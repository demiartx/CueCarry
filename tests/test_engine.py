# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
End-to-end tests: build a small fake library with every supported file type,
run the converter, and check the files, the XML and the actual audio timing.

Run:  python -m pytest -q
"""
import array
import os
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

import pytest

from app import cli, core, encoder

FFMPEG = os.environ.get("CUECARRY_FFMPEG") or shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")

KICK = encoder.CAL_KICK  # 120 BPM kick, first kick at exactly 1.000 s


def ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_audio(path, rate=44100, codec=None, cover=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    src = ["-f", "lavfi", "-i", f"aevalsrc='{KICK}':s={rate}:d=4:c=stereo"]
    if cover:
        png = path + ".png"
        ff("-f", "lavfi", "-i", "color=red:s=32x32", "-frames:v", "1", png)
        ff(*src, "-i", png, "-map", "0:a", "-map", "1", "-c:v", "png",
           "-disposition:v", "attached_pic", "-sample_fmt", "s16", path)
        os.remove(png)
    else:
        extra = ["-c:a", codec] if codec else []
        if path.endswith((".flac", ".wav", ".aiff")):
            extra += ["-sample_fmt", "s16"] if not codec else []
        ff(*src, *extra, path)


def loc(p):
    return core.path_to_loc(p)


@pytest.fixture()
def library(tmp_path):
    music = tmp_path / "Music"
    files = {
        "1": ("A/flac track.flac", dict(cover=True)),
        "2": ("B/wav 48k.wav", dict(rate=48000)),
        "3": ("B/aiff track.aiff", {}),
        "4": ("C/alac track.m4a", dict(codec="alac")),
        "5": ("C/aac track.m4a", dict(codec="aac")),
        "6": ("D/hires 96k.flac", dict(rate=96000)),
        "7": ("D/already.mp3", dict(codec="libmp3lame")),
    }
    rates = {"2": 48000, "6": 96000}
    root = ET.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
    col = ET.SubElement(root, "COLLECTION")
    for tid, (rel, kw) in files.items():
        p = str(music / rel)
        make_audio(p, **kw)
        t = ET.SubElement(col, "TRACK", {
            "TrackID": tid, "Name": f"Title {tid}", "Artist": "Artist", "Rating": "204",
            "SampleRate": str(rates.get(tid, 44100)), "AverageBpm": "120.00",
            "Location": loc(p)})
        ET.SubElement(t, "TEMPO", {"Inizio": "0.500", "Bpm": "120.00", "Metro": "4/4", "Battito": "1"})
        ET.SubElement(t, "POSITION_MARK", {"Name": "", "Type": "0", "Start": "1.000", "Num": "0",
                                           "Red": "40", "Green": "226", "Blue": "20"})
        ET.SubElement(t, "POSITION_MARK", {"Name": "", "Type": "4", "Start": "2.000",
                                           "End": "3.000", "Num": "1"})
    # Track outside the source folder, and one whose file is missing.
    other = tmp_path / "Elsewhere" / "outside.flac"
    make_audio(str(other))
    ET.SubElement(col, "TRACK", {"TrackID": "8", "Name": "Outside", "SampleRate": "44100",
                                 "Location": loc(str(other))})
    ET.SubElement(col, "TRACK", {"TrackID": "9", "Name": "Missing", "SampleRate": "44100",
                                 "Location": loc(str(music / "A" / "gone.flac"))})
    col.set("Entries", str(len(col)))
    pls = ET.SubElement(root, "PLAYLISTS")
    top = ET.SubElement(pls, "NODE", {"Type": "0", "Name": "ROOT", "Count": "2"})
    p1 = ET.SubElement(top, "NODE", {"Name": "Set", "Type": "1", "KeyType": "0", "Entries": "3"})
    for k in ("1", "4", "7"):
        ET.SubElement(p1, "TRACK", {"Key": k})
    folder = ET.SubElement(top, "NODE", {"Type": "0", "Name": "Folder", "Count": "1"})
    p2 = ET.SubElement(folder, "NODE", {"Name": "Inner", "Type": "1", "KeyType": "0", "Entries": "1"})
    ET.SubElement(p2, "TRACK", {"Key": "2"})
    xml = tmp_path / "collection.xml"
    ET.ElementTree(root).write(xml, encoding="UTF-8", xml_declaration=True)
    return tmp_path, str(xml), str(music)


def first_onset_ms(path):
    """Where the first kick is, in ms, as a decoder hears it. The MP3s are made
    without a LAME header, so every decoder hears the same thing."""
    args = [FFMPEG, "-loglevel", "error"]
    raw = subprocess.run(args + ["-i", path, "-f", "s16le", "-ac", "1", "-ar", "44100", "-"],
                         capture_output=True).stdout
    a = array.array("h", raw)
    i = next(i for i, v in enumerate(a) if abs(v) > 2000)
    return i / 44.1


def tracks_by_name(xml_path):
    col = ET.parse(xml_path).getroot().find("COLLECTION")
    return {t.get("Name"): t for t in col.findall("TRACK")}


# --- paths --------------------------------------------------------------------

def test_location_round_trip():
    for p in ["J:/Engine Library/Music/Ä & B's/#1 (3).flac", "/Users/me/Music/x y.flac"]:
        assert core.loc_to_path(core.path_to_loc(p)) == p


def test_source_folder_ignores_other_drives():
    paths = ["J:/Music/a/1.flac", "J:/Music/b/2.flac", "C:/Users/x/Sampler/k.wav"]
    assert core.detect_src_root(paths) == "J:/Music"


# --- full run -------------------------------------------------------------------

def test_compensate_run(library):
    tmp, xml, music = library
    assert cli.main([xml, "--src-root", music, "--compensate", "--jobs", "2"]) == 1   # 1 = some failures (missing file)
    out = os.path.join(music + " MP3", "rekordbox_mp3.xml")
    t = tracks_by_name(out)
    # converted: FLAC, WAV, AIFF, ALAC, 96k FLAC; kept: AAC, MP3; skipped: outside; failed: missing
    assert {n for n, e in t.items() if e.get("Kind") == "MP3 File"} >= {
        "Title 1", "Title 2", "Title 3", "Title 4", "Title 6"}
    assert "Title 5" in t and t["Title 5"].get("Location").endswith(".m4a")   # AAC kept
    assert "Title 7" in t                                                    # MP3 kept
    assert "Outside" not in t and "Missing" not in t
    # rating and cue color survive
    assert t["Title 1"].get("Rating") == "204"
    assert t["Title 1"].find("POSITION_MARK").get("Red") == "40"
    # cue shift = 1105 samples at the MP3's rate
    assert t["Title 1"].find("POSITION_MARK").get("Start") == "1.025"      # 44.1 kHz
    assert t["Title 2"].find("POSITION_MARK").get("Start") == "1.023"      # 48 kHz
    assert t["Title 6"].get("SampleRate") == "48000"                        # 96k resampled
    assert t["Title 6"].find("POSITION_MARK").get("Start") == "1.023"
    loop = [m for m in t["Title 1"].findall("POSITION_MARK") if m.get("Type") == "4"][0]
    assert (loop.get("Start"), loop.get("End")) == ("2.025", "3.025")
    # untouched: AAC/MP3 kept as-is have no shift
    assert t["Title 7"].find("POSITION_MARK").get("Start") == "1.000"
    # playlists copied with suffix, inside folder "MP3"
    names = [n.get("Name") for n in ET.parse(out).getroot().iter("NODE")]
    assert "MP3" in names and "Set (MP3)" in names and "Inner (MP3)" in names
    # the shifted cue matches where the kick really is (no header to skip)
    mp3 = core.loc_to_path(t["Title 1"].get("Location"))
    assert abs(first_onset_ms(mp3) - 1025.1) < 1.0
    # cover art carried over
    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", mp3], capture_output=True, text=True).stderr
    assert "Video: png" in probe or "Video: mjpeg" in probe
    # failures file lists the missing track
    with open(os.path.splitext(out)[0] + "_failures.txt", encoding="utf-8") as f:
        assert "gone.flac" in f.read()


def test_align_run(library):
    tmp, xml, music = library
    cli.main([xml, "--src-root", music, "--align", "--playlist", "Set"])
    t = tracks_by_name(os.path.join(music + " MP3", "rekordbox_mp3.xml"))
    assert t["Title 1"].find("POSITION_MARK").get("Start") == "1.000"
    mp3 = core.loc_to_path(t["Title 1"].get("Location"))
    assert abs(first_onset_ms(mp3) - 1000.0) < 1.0


def test_rerun_skips_and_xml_only(library):
    tmp, xml, music = library
    cli.main([xml, "--src-root", music, "--playlist", "Set"])
    mp3 = os.path.join(music + " MP3", "A", "flac track.mp3")
    before = os.path.getmtime(mp3)
    cli.main([xml, "--src-root", music, "--playlist", "Set", "--offset-ms", "10", "--xml-only"])
    assert os.path.getmtime(mp3) == before
    t = tracks_by_name(os.path.join(music + " MP3", "rekordbox_mp3.xml"))
    assert t["Title 1"].find("POSITION_MARK").get("Start") == "1.010"


def test_output_xml_location(library):
    tmp, xml, music = library
    lib = core.load_library(xml)
    base = dict(xml_path=xml, src_root=music)
    default = core.make_plan(lib, core.Settings(**base)).out_xml
    assert default == core.norm(music) + " MP3/rekordbox_mp3.xml"            # empty = as before
    custom = str(tmp / "exports" / "mp3 playlists.xml")
    assert core.make_plan(lib, core.Settings(**base, out_xml=custom)).out_xml == core.norm(custom)
    (tmp / "exports").mkdir()
    in_folder = core.make_plan(lib, core.Settings(**base, out_xml=str(tmp / "exports"))).out_xml
    assert in_folder == core.norm(str(tmp / "exports")) + "/rekordbox_mp3.xml"
    with pytest.raises(ValueError, match="collection XML"):                  # never overwrite the input
        core.make_plan(lib, core.Settings(**base, out_xml=xml))
    assert cli.main([xml, "--src-root", music, "--out", custom, "--xml-only"]) == 0
    assert "Title 7" in tracks_by_name(custom)
    assert not os.path.exists(default)


def test_calibration_round_trip(tmp_path):
    files = encoder.make_calibration_tracks(FFMPEG, str(tmp_path / "cal"))
    assert len(files) == 4
    # Fake what rekordbox exports after analysis (values measured in rekordbox 7.2)
    root = ET.Element("DJ_PLAYLISTS")
    col = ET.SubElement(root, "COLLECTION")
    for (name, title, _), inizio in zip(encoder.CAL_FILES, ["0.499", "0.024", "0.024", "0.499"]):
        t = ET.SubElement(col, "TRACK", {"TrackID": title[4], "Name": title, "Artist": "Calibration",
                                         "Location": loc(str(tmp_path / "cal" / name))})
        ET.SubElement(t, "TEMPO", {"Inizio": inizio, "Bpm": "120.00"})
    x = tmp_path / "exported.xml"
    ET.ElementTree(root).write(x)
    shifts = {r[0][:5]: r[2] for r in core.read_calibration(str(x))}
    assert shifts == {"CAL A": 0.0, "CAL B": 25.0, "CAL C": 25.0, "CAL D": 0.0}


def test_locked_file_and_errors_do_not_stop_the_run(library, monkeypatch):
    """Windows can lock a just-written file for a moment; a crash in one track
    must become that track's failure, not stop the whole run."""
    tmp, xml, music = library
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("[WinError 32] file in use")
        return real_replace(a, b)
    monkeypatch.setattr(encoder.time, "sleep", lambda s: None)
    monkeypatch.setattr(encoder.os, "replace", flaky_replace)
    real_codec = encoder.audio_codec

    def broken_probe(ff, path):
        if "alac" in path:
            raise RuntimeError("probe exploded")
        return real_codec(ff, path)
    monkeypatch.setattr(encoder, "audio_codec", broken_probe)
    rc = cli.main([xml, "--src-root", music, "--playlist", "Set"])
    assert rc == 1                                   # one failure reported, run finished
    assert os.path.exists(os.path.join(music + " MP3", "A", "flac track.mp3"))
    with open(os.path.join(music + " MP3", "rekordbox_mp3_failures.txt"), encoding="utf-8") as f:
        assert "RuntimeError: probe exploded" in f.read()
