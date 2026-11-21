# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""Loudness policy and real-audio checks for optional DJ loudness matching."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

import pytest

from app import cli, core, encoder, loudness, outputs

FFMPEG = os.environ.get("CUECARRY_FFMPEG") or shutil.which("ffmpeg")
audio = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


@pytest.mark.parametrize("level,peak,expected", [
    (-12, -3, 0), (-10, -3, 0), (-8, -3, 0),  # leave the whole band alone
    (-18, -10, 6), (-30, -20, 6),              # boost/cap quiet tracks
    (-6, -1, -2), (-1, 0, -6),               # lower only loud outliers
    (-18, -4, 2.7),                           # preserve headroom
])
def test_range_policy(level, peak, expected):
    gain, _ = loudness.choose_gain(loudness.Measurement(level, peak, 1000),
                                   -12, -8, -1, 6, 6)
    assert gain == pytest.approx(expected)


def test_silence_never_gets_boosted():
    gain, reason = loudness.choose_gain(loudness.Measurement(None, None, 44100), -12, -8, -1, 6, 6)
    assert gain == 0
    assert "no boost" in reason


def test_peak_protection_does_not_exceed_cut_cap():
    with pytest.raises(ValueError, match="maximum cut"):
        loudness.choose_gain(loudness.Measurement(-10, 6, 44100), -12, -8, -1, 6, 6)


@pytest.mark.parametrize("value", ["nan", "inf", "not a number"])
def test_invalid_measurements_are_rejected(value):
    with pytest.raises(ValueError, match="safely"):
        loudness.parse_measurement('Number of samples: 100\n' + json.dumps({"input_i": value, "input_tp": -2}))


def test_missing_measurements_are_rejected():
    with pytest.raises(ValueError, match="safely"):
        loudness.parse_measurement("FFmpeg failed")


@pytest.mark.parametrize("field,value", [
    ("loudness_low", math.nan), ("loudness_high", -31), ("true_peak", 0),
    ("max_boost", math.inf), ("max_cut", -1), ("xml_only", True),
])
def test_invalid_loudness_settings(tmp_path, field, value):
    from tests.test_safety import make_library
    lib, settings = make_library(tmp_path, ["track.flac"])
    settings.normalize = True
    setattr(settings, field, value)
    with pytest.raises(ValueError):
        core.make_plan(lib, settings)


def test_default_is_off_and_cli_can_select_range():
    assert not cli.settings_from(cli.parse_args(["collection.xml"])).normalize
    s = cli.settings_from(cli.parse_args(["collection.xml", "--normalize", "--loudness-low", "-13",
                                         "--loudness-high", "-7", "--max-boost", "4"]))
    assert (s.normalize, s.loudness_low, s.loudness_high, s.max_boost) == (True, -13, -7, 4)


def test_matching_uses_a_separate_default_destination(tmp_path):
    from tests.test_safety import make_library
    lib, s = make_library(tmp_path, ["track.flac"])
    plain = core.make_plan(lib, s).dst_root
    s.normalize = True
    matched = core.make_plan(lib, s).dst_root
    assert plain.endswith(" MP3") and matched.endswith(" MP3 Matched")
    assert plain != matched


def test_mp3_only_library_keeps_originals_with_a_clear_plan(tmp_path):
    from tests.test_safety import make_library
    lib, s = make_library(tmp_path, ["track.mp3"])
    s.src_root = None
    s.normalize = True
    plan = core.make_plan(lib, s)
    assert not plan.jobs
    assert len(plan.keep) == 1
    assert plan.src_root == str(tmp_path / "Music").replace("\\", "/")
    assert plan.dst_root.endswith("Music MP3 Matched")


def test_stop_kills_an_active_analysis_process(tmp_path):
    from tests.test_safety import make_library
    lib, s = make_library(tmp_path, ["track.flac"])
    runner = encoder.Runner("unused", core.make_plan(lib, s), s, lambda *args: None)
    worker = threading.Thread(target=lambda: runner._run_cmd(
        [sys.executable, "-c", "import time; time.sleep(60)"]), daemon=True)
    worker.start()
    try:
        end = time.monotonic() + 5
        started = False
        while time.monotonic() < end:
            with runner.lock:
                started = bool(runner.procs)
            if started:
                break
            time.sleep(0.01)
        assert started, "The analysis process did not start"
        runner.stop()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert not runner.procs
    finally:
        runner.stop()
        worker.join(timeout=5)


def audio_library(tmp_path, expression, rate=44100, mode="compensate", codec="flac", channels=2):
    music = tmp_path / "Music"
    music.mkdir()
    ext = ".m4a" if codec in ("alac", "aac") else ".flac"
    src = music / ("track" + ext)
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc='{expression}':s={rate}:d=5:c={'stereo' if channels == 2 else 'mono'}",
                    "-c:a", codec, str(src)], check=True)
    root = ET.Element("DJ_PLAYLISTS")
    collection = ET.SubElement(root, "COLLECTION")
    track = ET.SubElement(collection, "TRACK", TrackID="1", Name="Test", Artist="DJ",
                           SampleRate="12345", Location=core.path_to_loc(str(src)), Rating="204")
    ET.SubElement(track, "POSITION_MARK", Start="1.000", Num="0", Type="0", Red="40")
    xml = tmp_path / "collection.xml"
    ET.ElementTree(root).write(xml, encoding="utf-8")
    lib = core.load_library(str(xml))
    s = core.Settings(xml_path=str(xml), src_root=str(music), normalize=True, mode=mode, jobs=1)
    plan = core.make_plan(lib, s)
    runner = encoder.Runner(FFMPEG, plan, s, lambda *args: None)
    return src, lib, s, plan, runner


def record_export(lib, s, plan):
    return core.write_xml(lib, plan, s, lambda p: encoder.is_alac(FFMPEG, p),
                          lambda p: encoder.is_aac(FFMPEG, p))


@audio
@pytest.mark.parametrize("amplitude", [0.08, 0.4, 0.95])
def test_quiet_in_band_and_loud_audio_are_verified(tmp_path, amplitude):
    src, lib, s, plan, runner = audio_library(tmp_path, f"{amplitude}*sin(2*PI*440*t)")
    original = src.read_bytes()
    statuses = []
    runner.progress = lambda *args: statuses.append(args[-1])
    result = runner.run()
    assert not result.failures, result.failures
    assert result.converted == 1
    job = plan.jobs[0]
    record = outputs.read_record(job.dst)
    assert record["output_peak_dbtp"] <= s.true_peak
    if amplitude == 0.08:
        assert 0 < record["gain_db"] <= 6
    elif amplitude == 0.4:
        assert record["gain_db"] == 0
    else:
        assert -6 <= record["gain_db"] < 0
    assert src.read_bytes() == original
    assert "dB" in statuses[0] and "LUFS" in statuses[0]
    assert record_export(lib, s, plan) == (1, 0)
    track = ET.parse(plan.out_xml).find("COLLECTION/TRACK")
    assert track.get("Rating") == "204"
    assert track.get("SampleRate") == "44100"  # actual stream, not stale XML
    assert track.find("POSITION_MARK").get("Red") == "40"
    assert not list(Path(job.dst).parent.glob("*.part"))


@audio
@pytest.mark.parametrize("mode,rate", [("compensate", 44100), ("align", 44100),
                                       ("align", 96000), ("offset", 48000),
                                       ("compensate", 22050), ("compensate", 32000),
                                       ("compensate", 88200)])
def test_normalization_preserves_cue_timing(tmp_path, mode, rate):
    from tests.test_engine import first_onset_ms
    src, lib, s, plan, runner = audio_library(tmp_path, encoder.CAL_KICK + "*0.4", rate, mode)
    assert not runner.run().failures
    record_export(lib, s, plan)
    track = ET.parse(plan.out_xml).find("COLLECTION/TRACK")
    assert int(track.get("SampleRate")) >= 32000
    cue_ms = float(track.find("POSITION_MARK").get("Start")) * 1000
    onset = first_onset_ms(plan.jobs[0].dst)
    assert abs(cue_ms - onset) < 4, (mode, rate, cue_ms, onset)


@audio
def test_silent_audio_is_not_amplified_or_corrupted(tmp_path):
    src, lib, s, plan, runner = audio_library(tmp_path, "0")
    assert not runner.run().failures
    record = outputs.read_record(plan.jobs[0].dst)
    assert record["gain_db"] == 0
    assert record["output_lufs"] is None
    assert record["output_peak_dbtp"] is None
    assert record["decoded_samples"] >= 5 * 44100


@audio
@pytest.mark.parametrize("codec", ["alac", "aac"])
def test_alac_is_matched_and_aac_is_left_original(tmp_path, codec):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)", codec=codec)
    original = src.read_bytes()
    result = runner.run()
    assert not result.failures, result.failures
    assert result.converted == (codec == "alac")
    assert result.kept_aac == (codec == "aac")
    assert src.read_bytes() == original
    record_export(lib, s, plan)
    track = ET.parse(plan.out_xml).find("COLLECTION/TRACK")
    assert track.get("Location").endswith(".mp3" if codec == "alac" else ".m4a")


@audio
def test_resume_rejects_changed_settings_sources_and_outputs(tmp_path):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    assert not runner.run().failures
    job = plan.jobs[0]
    assert plan.todo(False, s) == []
    assert encoder.Runner(FFMPEG, plan, s, lambda *args: None).run().converted == 0
    s.loudness_low -= 1
    assert plan.todo(False, s) == [job]
    assert record_export(lib, s, plan) == (0, 0)
    s.loudness_low += 1
    assert outputs.matches(job, s)
    lib.tracks["1"].element.set("Name", "Updated title")
    assert not outputs.matches(job, s)
    lib.tracks["1"].element.set("Name", "Test")
    assert outputs.matches(job, s)
    before = src.stat()
    os.utime(src, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
    assert not outputs.matches(job, s)
    os.utime(src, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert outputs.matches(job, s)
    with open(job.dst, "ab") as stream:
        stream.write(b"damaged")
    assert not outputs.matches(job, s)
    assert record_export(lib, s, plan) == (0, 0)


@audio
def test_untracked_existing_output_is_not_trusted(tmp_path):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    job = plan.jobs[0]
    Path(job.dst).parent.mkdir()
    Path(job.dst).write_bytes(b"not audio")
    assert plan.todo(False, s) == [job]
    assert record_export(lib, s, plan) == (0, 0)
    assert runner.run().failures
    assert Path(job.dst).read_bytes() == b"not audio"
    s.overwrite = True
    runner = encoder.Runner(FFMPEG, plan, s, lambda *args: None)
    assert not runner.run().failures
    assert outputs.matches(job, s)


@audio
@pytest.mark.parametrize("failure", ["decode", "length", "peak", "source_change"])
def test_failed_verification_preserves_previous_audio(tmp_path, monkeypatch, failure):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    job = plan.jobs[0]
    Path(job.dst).parent.mkdir()
    Path(job.dst).write_bytes(b"previous audio")
    s.overwrite = True
    measure = runner._measure

    def fail(path, filters):
        result = measure(path, filters)
        if Path(path) != src:
            if failure == "decode":
                raise ValueError("simulated decoder failure")
            if failure == "length":
                return loudness.Measurement(-10, -2, 1)
            if failure == "peak":
                return loudness.Measurement(-10, 20, result.samples)
            if failure == "source_change":
                before = src.stat()
                os.utime(src, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        return result

    monkeypatch.setattr(runner, "_measure", fail)
    result = runner.run()
    assert result.failures
    assert Path(job.dst).read_bytes() == b"previous audio"
    assert not Path(outputs.record_path(job.dst)).exists()
    assert not list(Path(job.dst).parent.glob("*.part"))


@audio
def test_cancel_during_analysis_does_not_publish_audio(tmp_path, monkeypatch):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    measure = runner._measure

    def cancel(path, filters):
        result = measure(path, filters)
        runner.stop()
        return result

    monkeypatch.setattr(runner, "_measure", cancel)
    result = runner.run()
    assert result.cancelled
    assert not result.failures
    assert not Path(plan.jobs[0].dst).exists()


@audio
def test_bad_alac_is_not_mislabeled_as_aac(tmp_path):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)", codec="alac")
    src.write_bytes(b"broken")
    assert runner.run().failures
    assert record_export(lib, s, plan) == (0, 0)


@audio
def test_mono_and_gain_tags_survive(tmp_path):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)", channels=1)
    # Add replay gain to the original; a matched output must not carry stale
    # instructions that could cause the player to apply gain twice.
    tagged = src.with_name("tagged.flac")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-c", "copy",
                    "-metadata", "REPLAYGAIN_TRACK_GAIN=+9 dB", str(tagged)], check=True)
    os.replace(tagged, src)
    assert not runner.run().failures
    _, probe = runner._run_cmd([FFMPEG, "-hide_banner", "-i", plan.jobs[0].dst])
    assert "mono" in probe and "DJ" in probe
    assert "replaygain_track_gain" not in probe.lower()


@audio
def test_cover_art_survives_matching(tmp_path):
    from tests.test_engine import make_audio
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    make_audio(str(src), cover=True)
    original = src.read_bytes()
    assert not runner.run().failures
    _, probe = runner._run_cmd([FFMPEG, "-hide_banner", "-i", plan.jobs[0].dst])
    assert "Video: png" in probe or "Video: mjpeg" in probe
    assert src.read_bytes() == original


@audio
def test_switching_off_rebuilds_from_original(tmp_path):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    assert not runner.run().failures
    job = plan.jobs[0]
    assert outputs.matches(job, s)
    s.normalize = False
    assert plan.todo(False, s) == [job]
    assert record_export(lib, s, plan) == (0, 0)
    assert not encoder.Runner(FFMPEG, plan, s, lambda *args: None).run().failures
    assert not Path(outputs.record_path(job.dst)).exists()
    assert outputs.matches(job, s)


@audio
def test_cli_can_normalize_and_resume(tmp_path, capsys):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    argv = [s.xml_path, "--src-root", s.src_root, "--normalize", "--jobs", "1", "--ffmpeg", FFMPEG]
    assert cli.main(argv) == 0
    first = capsys.readouterr().out
    assert "-12 to -8 LUFS" in first and "+6.0 dB" in first
    assert cli.main(argv) == 0
    assert "0 to encode now" in capsys.readouterr().out


@audio
def test_encoded_peak_retry_always_uses_original(tmp_path, monkeypatch):
    src, lib, s, plan, runner = audio_library(tmp_path, "0.08*sin(2*PI*440*t)")
    measure = runner._measure
    commands = encoder.ffmpeg_commands
    tries = []

    def peak_once(path, filters):
        result = measure(path, filters)
        if Path(path) != src and len(tries) == 1:
            return loudness.Measurement(result.integrated, s.true_peak + 0.5, result.samples)
        return result

    def capture(ffmpeg, job, settings, tmp, gain, rate):
        assert Path(job.track.path) == src
        tries.append(gain)
        return commands(ffmpeg, job, settings, tmp, gain, rate)

    monkeypatch.setattr(runner, "_measure", peak_once)
    monkeypatch.setattr(encoder, "ffmpeg_commands", capture)
    assert not runner.run().failures
    assert len(tries) == 2 and tries[1] < tries[0]
    record = outputs.read_record(plan.jobs[0].dst)
    assert record["output_peak_dbtp"] <= s.true_peak


@audio
def test_transient_heavy_quiet_audio_respects_headroom(tmp_path):
    expression = "if(lt(mod(t,0.5),0.005),0.95*sin(2*PI*5000*t),0)"
    src, lib, s, plan, runner = audio_library(tmp_path, expression)
    original = src.read_bytes()
    result = runner.run()
    assert not result.failures, result.failures
    record = outputs.read_record(plan.jobs[0].dst)
    assert record["input_lufs"] < s.loudness_low
    assert record["gain_db"] < 1
    assert record["output_peak_dbtp"] <= s.true_peak
    assert "headroom limited" in record["reason"]
    assert src.read_bytes() == original
