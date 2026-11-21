# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""File safety regressions that run without Qt or ffmpeg."""
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from app import cli, core, encoder


def make_library(tmp_path, names):
    music = tmp_path / "Music"
    music.mkdir()
    tracks = {}
    for i, name in enumerate(names):
        path = music / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original audio")
        el = ET.Element("TRACK", TrackID=str(i), Location=core.path_to_loc(str(path)))
        tracks[str(i)] = core.Track(str(i), str(path), path.suffix.lower(), el)
    xml = tmp_path / "collection.xml"
    xml.write_text('<DJ_PLAYLISTS><COLLECTION /></DJ_PLAYLISTS>', encoding="utf-8")
    lib = core.Library(str(xml), tracks, core.PlaylistNode("ROOT", True))
    settings = core.Settings(xml_path=str(xml), src_root=str(music))
    return lib, settings


@pytest.mark.parametrize("names", [("track.flac", "track.wav"), ("track.flac", "TRACK.wav")])
def test_colliding_mp3_paths_rejected_before_encoding(tmp_path, names):
    lib, settings = make_library(tmp_path, names)
    with pytest.raises(ValueError, match="same path"):
        core.make_plan(lib, settings)
    assert not (tmp_path / "Music MP3").exists()
    assert all(Path(t.path).read_bytes() == b"original audio" for t in lib.tracks.values())


@pytest.mark.parametrize("target", ["source", "mp3", "part", "failures"])
def test_custom_xml_cannot_replace_audio_or_encoder_files(tmp_path, target):
    lib, settings = make_library(tmp_path, ["track.flac"])
    mp3 = str(tmp_path / "Music MP3" / "track.mp3")
    if target == "source":
        settings.out_xml = next(iter(lib.tracks.values())).path
    elif target == "failures":
        # The failure report would overwrite an original file, even though
        # that original isn't a format CueCarry converts.
        path = tmp_path / "Music" / "export_failures.txt"
        path.write_bytes(b"original audio")
        lib.tracks["extra"] = core.Track("extra", str(path), ".txt", ET.Element("TRACK"))
        settings.out_xml = str(path.with_name("export.xml"))
    else:
        settings.out_xml = mp3 + (".part" if target == "part" else "")
    with pytest.raises(ValueError, match="source file|same path"):
        core.make_plan(lib, settings)


def test_dot_segments_cannot_hide_output_inside_source(tmp_path):
    lib, settings = make_library(tmp_path, ["track.flac"])
    settings.dst_root = str(tmp_path / "other" / ".." / "Music" / "copy")
    with pytest.raises(ValueError, match="inside the source"):
        core.make_plan(lib, settings)


def test_dot_segments_cannot_escape_source_scope(tmp_path):
    lib, settings = make_library(tmp_path, ["track.flac"])
    track = next(iter(lib.tracks.values()))
    track.path = str(tmp_path / "Music" / ".." / "elsewhere.flac")
    plan = core.make_plan(lib, settings)
    assert not plan.jobs
    assert plan.skipped == [(track, "outside the source folder")]


def test_collection_alias_is_protected(tmp_path):
    lib, settings = make_library(tmp_path, ["track.flac"])
    settings.out_xml = str(tmp_path / "Music" / ".." / "collection.xml")
    with pytest.raises(ValueError, match="collection XML|source file"):
        core.make_plan(lib, settings)


@pytest.mark.parametrize("field,value", [("jobs", 0), ("trim_samples", -1), ("mode", "unknown")])
def test_invalid_settings_are_rejected_during_planning(tmp_path, field, value):
    lib, settings = make_library(tmp_path, ["track.flac"])
    setattr(settings, field, value)
    with pytest.raises(ValueError):
        core.make_plan(lib, settings)


@pytest.mark.parametrize("fail_at", ["serialize", "replace"])
def test_failed_xml_export_preserves_previous_export(tmp_path, monkeypatch, fail_at):
    lib, settings = make_library(tmp_path, ["track.flac"])
    plan = core.make_plan(lib, settings)
    out = tmp_path / "Music MP3" / core.DEFAULT_OUT_NAME
    out.parent.mkdir()
    out.write_bytes(b"previous complete XML")

    def fail(*args, **kwargs):
        raise OSError("simulated write failure")

    if fail_at == "serialize":
        monkeypatch.setattr(ET.ElementTree, "write", fail)
    else:
        monkeypatch.setattr(core.os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        core.write_xml(lib, plan, settings, lambda path: False)
    assert out.read_bytes() == b"previous complete XML"
    assert list(out.parent.iterdir()) == [out]


def test_successful_xml_export_is_parseable(tmp_path):
    lib, settings = make_library(tmp_path, ["track.mp3"])
    plan = core.make_plan(lib, settings)
    assert core.write_xml(lib, plan, settings, lambda path: False) == (1, 0)
    assert ET.parse(plan.out_xml).find("COLLECTION").get("Entries") == "1"


def test_stopped_runner_does_not_launch_another_process(tmp_path, monkeypatch):
    lib, settings = make_library(tmp_path, ["track.flac"])
    runner = encoder.Runner("ffmpeg", core.make_plan(lib, settings), settings, lambda *args: None)
    runner.stop()

    def unexpected(*args, **kwargs):
        pytest.fail("A stopped runner must not start ffmpeg")

    monkeypatch.setattr(encoder.subprocess, "Popen", unexpected)
    assert runner._run_cmd(["ffmpeg"])[0] != 0
    assert runner.run().cancelled


@pytest.mark.parametrize("content", [None, "<broken", "<OTHER />"])
def test_cli_reports_unreadable_collection_without_traceback(tmp_path, capsys, content):
    xml = tmp_path / "collection.xml"
    if content is not None:
        xml.write_text(content, encoding="utf-8")
    assert cli.main([str(xml), "--dry-run"]) == 2
    assert "Could not open the collection XML" in capsys.readouterr().err
