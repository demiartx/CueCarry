# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
encoder.py - everything that talks to ffmpeg.

ffmpeg is a separate program (ffmpeg.exe). This file finds it, checks that it
can make MP3s, asks it what codec an .m4a file holds, and runs one ffmpeg
process per track, several at a time.

C++ reading notes
  - `subprocess.Popen` is like CreateProcess: it starts ffmpeg and gives back a
    handle; `.communicate()` waits for it and collects its output.
  - `ThreadPoolExecutor` is a fixed pool of worker threads, like a std::thread
    pool pulling tasks off a queue.
  - `threading.Event` is a thread-safe bool flag (here: "Stop was pressed").
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
# Imported from the submodule on purpose: on Python 3.15, once PySide6 is loaded,
# `concurrent.futures.ThreadPoolExecutor` comes back as an unresolved lazy import.
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .core import ENCODER_DELAY_SAMPLES, Job, Plan, Settings, mp3_sample_rate
from . import loudness, outputs

# On Windows, stop every ffmpeg run from flashing a console window.
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# =============================================================================
# 1. Finding ffmpeg
# What:  Look for ffmpeg next to the app first (the bundled copy), then on PATH.
# Why:   The packaged app ships its own ffmpeg, so users install nothing. When
#        run from source, a full ffmpeg on PATH works too.
# If it goes wrong: "ffmpeg has no MP3 encoder" means a stripped-down copy was
#        found (for example a Microsoft Store alias in ...\WindowsApps). Pick a
#        full build in Tools > Choose ffmpeg.
# =============================================================================

def bundled_ffmpeg() -> Optional[str]:
    """Path of the ffmpeg shipped inside the packaged app, if any."""
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    bases = []
    if getattr(sys, "frozen", False):                 # running as the packaged app
        bases.append(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
        bases.append(os.path.dirname(sys.executable))
    bases.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    for b in bases:
        p = os.path.join(b, "ffmpeg", exe)
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def has_mp3_encoder(ffmpeg: str) -> bool:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                           text=True, errors="replace", creationflags=NO_WINDOW, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "libmp3lame" in r.stdout


def find_ffmpeg(preferred: Optional[str] = None) -> tuple:
    """Returns (path or None, message). Tries: preferred, bundled, PATH."""
    tried = []
    for cand in [preferred, os.environ.get("CUECARRY_FFMPEG"), bundled_ffmpeg(), shutil.which("ffmpeg")]:
        if cand and cand not in tried:
            tried.append(cand)
            if has_mp3_encoder(cand):
                return cand, f"Using ffmpeg: {cand}"
    if tried:
        return None, ("No ffmpeg with an MP3 encoder (libmp3lame) was found. Checked:\n  "
                      + "\n  ".join(tried))
    return None, "ffmpeg was not found."


# =============================================================================
# 2. Asking ffmpeg about a file
# What:  Read the codec name of an .m4a file (ALAC or AAC).
# Why:   .m4a can hold lossless ALAC or lossy AAC. Converting AAC to MP3 would
#        stack two lossy encodes, so AAC files are kept as they are.
# If it goes wrong: keep only confirmed AAC; unreadable files are failures.
# =============================================================================

def audio_codec(ffmpeg: str, path: str) -> str:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True,
                           text=True, errors="replace", creationflags=NO_WINDOW, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"Audio:\s*([A-Za-z0-9_]+)", r.stderr)
    return m.group(1).lower() if m else ""


def is_alac(ffmpeg: str, path: str) -> bool:
    """True only if the file is readable and holds ALAC; never raises."""
    try:
        return audio_codec(ffmpeg, path) == "alac"
    except Exception:   # noqa: BLE001
        return False


def is_aac(ffmpeg: str, path: str) -> bool:
    """Unknown/unreadable audio must never be imported as confirmed AAC."""
    try:
        return audio_codec(ffmpeg, path) == "aac"
    except Exception:  # Match is_alac's per-track error isolation.
        return False


# =============================================================================
# 3. Building one ffmpeg command
# What:  The exact ffmpeg arguments for one track.
# Why:   - 320 kbps constant bitrate, best encoder quality (-compression_level 0)
#        - ID3v2.3 tags (the version DJ software reads most reliably)
#        - cover art copied when present
#        - tags from the rekordbox XML override the file's own tags
#        - compensate/align modes leave out the LAME header (-write_xing 0) so
#          every player sees the same start delay
#        - align mode trims the delay off the start (atrim)
#        - rates above 48 kHz are resampled, since MP3 stops at 48 kHz
# If it goes wrong: the ffmpeg error is shown in the log and saved in the
#        failures file; the first attempt keeps cover art, the second drops it.
# =============================================================================

TAG_MAP = [  # (rekordbox XML attribute, ID3 key ffmpeg understands)
    ("Name", "title"), ("Artist", "artist"), ("Album", "album"),
    ("Genre", "genre"), ("Composer", "composer"), ("Comments", "comment"),
    ("Label", "publisher"), ("Remixer", "TPE4"), ("Grouping", "TIT1"),
    ("Tonality", "TKEY"),
]


def tag_args(attrs: Dict[str, str]) -> List[str]:
    args: List[str] = []
    for attr, key in TAG_MAP:
        v = attrs.get(attr, "")
        if v:
            args += ["-metadata", f"{key}={v}"]
    year = attrs.get("Year", "0")
    if year and year != "0":
        args += ["-metadata", f"date={year}"]
    try:
        bpm = float(attrs.get("AverageBpm", "0") or 0)
    except ValueError:
        bpm = 0
    if bpm > 0:
        args += ["-metadata", f"TBPM={round(bpm)}"]
    return args


def ffmpeg_commands(ffmpeg: str, job: Job, s: Settings, tmp: str,
                    gain_db: Optional[float] = None, sample_rate: Optional[int] = None) -> List[List[str]]:
    attrs = job.track.element.attrib
    base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", job.track.path]
    audio = ["-c:a", "libmp3lame", "-b:a", s.bitrate, "-compression_level", "0",
             "-map_metadata", "0", "-id3v2_version", "3", "-write_id3v1", "1"]
    rate = sample_rate if sample_rate is not None else int(float(attrs.get("SampleRate") or 0) or 0)
    if sample_rate is not None:
        audio += ["-ar", str(rate)]
    elif rate > 48000:
        audio += ["-ar", str(mp3_sample_rate(rate))]
    if gain_db is not None:
        filters = loudness.preprocessing(rate, s.mode, s.trim_samples)
        filters.append(f"volume={gain_db:.8f}dB:precision=double")
        audio += ["-af", ",".join(filters)]
    elif s.mode == "align":
        audio += ["-af", f"atrim=start_sample={s.trim_samples},asetpts=PTS-STARTPTS"]
    if s.mode in ("compensate", "align"):
        audio += ["-write_xing", "0"]
    gain_tags = []
    if gain_db is not None:
        # Gain tags describe the original master. Keeping them after applying
        # gain can make a player apply an old correction a second time.
        for key in ("REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK", "REPLAYGAIN_ALBUM_GAIN",
                    "REPLAYGAIN_ALBUM_PEAK", "REPLAYGAIN_REFERENCE_LOUDNESS",
                    "R128_TRACK_GAIN", "R128_ALBUM_GAIN", "iTunNORM",
                    "MP3GAIN_MINMAX", "MP3GAIN_ALBUM_MINMAX", "MP3GAIN_UNDO"):
            gain_tags += ["-metadata", key + "="]
    tail = tag_args(attrs) + gain_tags + ["-f", "mp3", tmp]
    with_art = ["-map", "0:a:0", "-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic"]
    no_art = ["-map", "0:a:0"]
    return [base + with_art + audio + tail, base + no_art + audio + tail]


# =============================================================================
# 4. Running the whole plan
# What:  Encode every job on a pool of threads, report progress after each
#        track, and stop cleanly when asked.
# Why:   Encoding is the slow part; one ffmpeg per CPU core keeps the machine
#        busy. Each MP3 is written as "name.mp3.part" and renamed when done,
#        so a stopped run never leaves a half-written MP3 behind.
# If it goes wrong: failures are collected with their ffmpeg error; running
#        again retries only what is missing.
# =============================================================================

@dataclass
class RunResult:
    done: int = 0
    converted: int = 0
    kept_aac: int = 0
    failures: List[tuple] = field(default_factory=list)    # (source path, error)
    cancelled: bool = False


ProgressFn = Callable[[int, int, str, str], None]   # (done, total, file name, status)


class Runner:
    def __init__(self, ffmpeg: str, plan: Plan, s: Settings,
                 progress: ProgressFn, cancel: Optional[threading.Event] = None):
        self.ffmpeg, self.plan, self.s, self.progress = ffmpeg, plan, s, progress
        self.cancel = cancel or threading.Event()
        self.lock = threading.Lock()
        self.procs: set = set()
        self.result = RunResult()

    def stop(self) -> None:
        """Called from the window's Stop button (another thread)."""
        self.cancel.set()
        with self.lock:
            for p in list(self.procs):
                try:
                    p.kill()
                except OSError:
                    pass

    def _run_cmd(self, cmd: List[str]) -> tuple:
        with self.lock:
            if self.cancel.is_set():
                return -1, "cancelled"
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 creationflags=NO_WINDOW)
            self.procs.add(p)
        try:
            _, err = p.communicate()
        finally:
            with self.lock:
                self.procs.discard(p)
        return p.returncode, err.decode("utf-8", "replace").strip()

    def _one(self, job: Job) -> str:
        """Encode one track. Any error becomes a "FAIL ..." status for that
        track instead of stopping the whole run."""
        try:
            return self._encode(job)
        except InterruptedError:
            if self.cancel.is_set():
                return "cancelled"
            return "FAIL audio processing was interrupted"
        except Exception as e:   # noqa: BLE001 - reported per track
            return f"FAIL {type(e).__name__}: {e}"

    def _encode(self, job: Job) -> str:
        if self.cancel.is_set():
            return "cancelled"
        if not os.path.exists(job.track.path):
            return "FAIL source file missing"
        if self.s.normalize:
            return self._encode_matched(job)
        if job.probe:
            codec = audio_codec(self.ffmpeg, job.track.path)
            if codec == "aac":
                return "kept (AAC, already lossy)"
            if codec != "alac":
                return "FAIL could not confirm ALAC or AAC audio"
        os.makedirs(os.path.dirname(job.dst), exist_ok=True)
        tmp = job.dst + ".part"
        err = ""
        for cmd in ffmpeg_commands(self.ffmpeg, job, self.s, tmp):
            code, err = self._run_cmd(cmd)
            if self.cancel.is_set():
                break
            if code == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                _retry(os.replace, tmp, job.dst)
                if os.path.exists(outputs.record_path(job.dst)):
                    _retry(os.remove, outputs.record_path(job.dst))
                return "ok"
        if os.path.exists(tmp):
            _retry(os.remove, tmp)
        if self.cancel.is_set():
            return "cancelled"
        return "FAIL " + (err.splitlines()[-1] if err else "ffmpeg failed")

    def _probe_audio(self, path: str) -> tuple[str, int]:
        _, text = self._run_cmd([self.ffmpeg, "-nostdin", "-hide_banner", "-i", path])
        if self.cancel.is_set():
            raise InterruptedError("cancelled")
        match = re.search(r"Audio:\s*([A-Za-z0-9_]+)([^\r\n]*)", text)
        properties = re.search(r",\s*(\d+) Hz,\s*(mono|stereo)(?:,|$)", match.group(2)) if match else None
        if not properties:
            raise ValueError("Could not read a mono or stereo audio stream safely.")
        return match.group(1).lower(), int(properties.group(1))

    def _measure(self, path: str, filters: List[str]) -> loudness.Measurement:
        code, text = self._run_cmd([
            self.ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
            "-xerror", "-err_detect", "explode", "-i", path, "-map", "0:a:0", "-vn",
            "-af", loudness.measurement_filter(filters), "-f", "null", "-"])
        if self.cancel.is_set():
            raise InterruptedError("cancelled")
        if code != 0:
            raise ValueError("Audio could not be decoded completely for loudness verification.")
        return loudness.parse_measurement(text)

    def _encode_matched(self, job: Job) -> str:
        """Analyze, adjust only outliers, then decode-check before publishing."""
        s = self.s
        source = outputs.fingerprint(job.track.path)
        if os.path.exists(job.dst) and not s.overwrite:
            previous = outputs.read_record(job.dst)
            if (not previous or previous.get("version") != 1 or
                    not isinstance(previous.get("source"), dict) or
                    previous["source"].get("path") != source["path"]):
                raise ValueError("Existing MP3 has no matching loudness record; choose another folder "
                                 "or enable re-encoding existing MP3s.")
        codec, source_rate = self._probe_audio(job.track.path)
        if job.probe:
            if codec == "aac":
                return "kept (AAC, already lossy; loudness unchanged)"
            if codec != "alac":
                raise ValueError("Could not confirm ALAC or AAC audio.")
        rate = mp3_sample_rate(source_rate)
        # MPEG-1 rates support all offered bitrates. Lower-rate MPEG-2 MP3
        # would silently clamp 192/256/320k to a lower bitrate.
        if rate not in (32000, 44100, 48000):
            rate = 44100 if source_rate % 11025 == 0 else 48000
        before = self._measure(job.track.path, loudness.preprocessing(rate, s.mode, s.trim_samples))
        gain, reason = loudness.choose_gain(before, s.loudness_low, s.loudness_high,
                                            s.true_peak, s.max_boost, s.max_cut)
        os.makedirs(os.path.dirname(job.dst) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cuecarry-", suffix=".mp3.part",
                                   dir=os.path.dirname(job.dst) or ".")
        os.close(fd)
        try:
            for attempt in range(3):
                error = "ffmpeg failed"
                for cmd in ffmpeg_commands(self.ffmpeg, job, s, tmp, gain, rate):
                    code, error = self._run_cmd(cmd)
                    if self.cancel.is_set():
                        return "cancelled"
                    if code == 0:
                        break
                else:
                    raise ValueError(error.splitlines()[-1] if error else "ffmpeg failed")
                after = self._measure(tmp, [f"aresample={rate}"])
                # MP3 padding/delay can add up to two frames. Catch truncated
                # or empty output without moving cues or trimming the music.
                if abs(after.samples - before.samples) > 2304:
                    raise ValueError("Decoded MP3 length did not match the source; output not published.")
                if ((before.integrated is not None and after.integrated is None) or
                        (before.peak is not None and after.peak is None)):
                    raise ValueError("Encoded audio lost its measurable signal; output not published.")
                if after.peak is None or after.peak <= s.true_peak:
                    if self.cancel.is_set():
                        return "cancelled"
                    if outputs.fingerprint(job.track.path) != source:
                        raise ValueError("Source changed during encoding; please retry.")
                    record = {"version": 1, "source": source, "profile": outputs.profile(s),
                              "tags": dict(job.track.element.attrib),
                              "sample_rate": rate, "gain_db": gain, "reason": reason,
                              "input_lufs": before.integrated, "output_lufs": after.integrated,
                              "output_peak_dbtp": after.peak, "decoded_samples": after.samples}
                    _retry(os.replace, tmp, job.dst)
                    record["output"] = outputs.fingerprint(job.dst)
                    outputs.publish_record(job.dst, record)
                    level = (f"{after.integrated:.1f} LUFS" if after.integrated is not None
                             else "silent/ungated audio")
                    outside = (after.integrated is not None and
                               not s.loudness_low - 0.2 <= after.integrated <= s.loudness_high + 0.2)
                    return f"ok ({gain:+.1f} dB; {level}; {reason}" + (
                        "; outside range, manual trim may help)" if outside else ")")
                gain -= after.peak - s.true_peak + 0.15
                reason += "; encoded peak protected"
                if gain < -s.max_cut:
                    break
            raise ValueError("Could not meet the peak ceiling within the adjustment cap; output not published.")
        finally:
            if os.path.exists(tmp):
                _retry(os.remove, tmp)

    def run(self) -> RunResult:
        todo = self.plan.todo(self.s.overwrite, self.s)
        total = len(todo)

        def work(job: Job) -> None:
            status = self._one(job)
            with self.lock:
                if status == "cancelled":
                    return
                self.result.done += 1
                if status.startswith("ok"):
                    self.result.converted += 1
                elif status.startswith("kept"):
                    self.result.kept_aac += 1
                else:
                    self.result.failures.append((job.track.path, status[5:]))
                done = self.result.done
            self.progress(done, total, os.path.basename(job.dst), status)

        with ThreadPoolExecutor(max(1, self.s.jobs)) as ex:
            list(ex.map(work, todo))
        self.result.cancelled = self.cancel.is_set()
        return self.result


def _retry(fn, *args, tries: int = 20, wait: float = 0.25):
    """Windows can briefly lock a file that was just written (antivirus
    scanning, the search indexer). Retry a rename or delete for ~5 seconds,
    like a C++ loop around MoveFileEx that sleeps on ERROR_SHARING_VIOLATION."""
    for i in range(tries):
        try:
            return fn(*args)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


def write_failures(path: str, failures: List[tuple]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for src, err in failures:
            f.write(f"{src}\n    {err}\n")


# =============================================================================
# 5. Calibration tracks
# What:  Make four test tracks (a FLAC reference and three MP3 variants) with a
#        kick exactly on every beat at 120 BPM, first kick at 1.000 s.
# Why:   Import them into rekordbox, let it analyze, export the collection,
#        and core.read_calibration compares the grid starts to the millisecond.
#        This is how the 25 ms MP3 delay was measured.
# If it goes wrong: if rekordbox can't find a grid on a test track, re-analyze
#        it (right-click > Analyze Track).
# =============================================================================

CAL_KICK = ("if(gte(t,1), exp(-mod(t-1,0.5)*40)*sin(2*PI*(45*mod(t-1,0.5)+"
            "(110/40)*(1-exp(-40*mod(t-1,0.5))))), 0)*0.9")
CAL_FILES = [  # (file name, title, extra ffmpeg args or None for the FLAC)
    ("A - reference.flac", "CAL A reference FLAC", None),
    ("B - standard.mp3", "CAL B standard MP3", []),
    ("C - no header.mp3", "CAL C no LAME header", ["-write_xing", "0"]),
    ("D - pretrim no header.mp3", "CAL D pretrim no header",
     ["-af", f"atrim=start_sample={ENCODER_DELAY_SAMPLES},asetpts=PTS-STARTPTS", "-write_xing", "0"]),
]


def make_calibration_tracks(ffmpeg: str, folder: str) -> List[str]:
    os.makedirs(folder, exist_ok=True)
    ref = os.path.join(folder, CAL_FILES[0][0])
    out = []
    for name, title, extra in CAL_FILES:
        dst = os.path.join(folder, name)
        if extra is None:
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                   "-i", f"aevalsrc='{CAL_KICK}':s=44100:d=64:c=stereo", "-sample_fmt", "s16"]
        else:
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", ref,
                   "-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0",
                   "-id3v2_version", "3"] + extra
        cmd += ["-metadata", f"title={title}", "-metadata", "artist=Calibration", dst]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"ffmpeg failed on {name}")
        out.append(dst)
    return out
