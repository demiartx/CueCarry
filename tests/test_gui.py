# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
Window test: opens the real window off-screen, loads a library, converts one
playlist through the Start button path, and saves screenshots to docs/.
Run:  QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_gui.py
"""
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, Qt          # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

from app import gui                                      # noqa: E402
from tests.test_engine import library                    # noqa: E402,F401  (fixture)

DOCS = os.path.join(os.path.dirname(__file__), "..", "docs")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_window_converts_a_playlist(qapp, library, monkeypatch, tmp_path):
    tmp, xml, music = library
    monkeypatch.setenv("CUECARRY_NO_DIALOGS", "1")                 # no modal popups
    monkeypatch.setenv("CUECARRY_SETTINGS", str(tmp_path / "settings.ini"))
    w = gui.MainWindow()
    w.resize(1200, 780)
    w.show()
    w.open_xml(xml)
    w.src_edit.setText(music)
    # menu and panel stay in step
    w.mode_actions["align"].trigger()
    assert w.mode_radios["align"].isChecked()
    w.mode_radios["compensate"].setChecked(True)
    assert w.mode_actions["compensate"].isChecked()
    w.flag_actions["only_playlists"].trigger()
    assert w.flag_boxes["only_playlists"].isChecked()
    w.set_flag("only_playlists", False)
    # check playlist "Set"
    root = w.tree.invisibleRootItem()
    item = next(root.child(i) for i in range(root.childCount()) if root.child(i).text(0) == "Set")
    item.setCheckState(0, Qt.Checked)
    assert w.checked_playlists() == {"Set"}
    w.preview()
    w.start()
    log = w.log_view.toPlainText
    assert wait_until(lambda: not w.worker.is_alive() and w.start_btn.isEnabled()), log()
    assert os.path.exists(os.path.join(music + " MP3", "A", "flac track.mp3")), log()
    assert "Done." in w.log_view.toPlainText()
    os.makedirs(DOCS, exist_ok=True)
    w.grab().save(os.path.join(DOCS, "screenshot-test-library.png"))
    w.close()


def test_window_output_xml_location(qapp, library, monkeypatch, tmp_path):
    tmp, xml, music = library
    monkeypatch.setenv("CUECARRY_NO_DIALOGS", "1")
    monkeypatch.setenv("CUECARRY_SETTINGS", str(tmp_path / "settings.ini"))
    out = os.path.join(str(tmp_path), "exports", "mp3 playlists.xml")
    w = gui.MainWindow()
    w.open_xml(xml)
    w.src_edit.setText(music)
    assert w.current_plan().out_xml.endswith(" MP3/rekordbox_mp3.xml")      # empty = default
    w.out_edit.setText(out)
    w.set_flag("xml_only", True)
    w.start()
    log = w.log_view.toPlainText
    assert wait_until(lambda: not w.worker.is_alive() and w.start_btn.isEnabled()), log()
    assert os.path.exists(out), log()
    assert not os.path.exists(os.path.join(music + " MP3", "rekordbox_mp3.xml")), log()
    w.close()                                                               # saves settings
    w2 = gui.MainWindow()
    assert w2.out_edit.text() == out                                        # remembered
    w2.close()


def test_stop_before_worker_start_preserves_previous_xml(qapp, tmp_path):
    """Stop can arrive before Worker.run has created its Runner."""
    from tests.test_safety import make_library

    lib, settings = make_library(tmp_path, ["track.flac"])
    plan = gui.core.make_plan(lib, settings)
    out = tmp_path / "Music MP3" / gui.core.DEFAULT_OUT_NAME
    out.parent.mkdir()
    out.write_bytes(b"previous export")
    worker = gui.Worker("unused-ffmpeg", lib, plan, settings, gui.Bridge())
    worker.stop()
    worker.run()
    assert worker.runner.result.cancelled
    assert out.read_bytes() == b"previous export"
    assert not (out.parent / "track.mp3").exists()


def test_window_matches_loudness_and_remembers_range(qapp, library, monkeypatch, tmp_path):
    from app import outputs

    _, xml, music = library
    monkeypatch.setenv("CUECARRY_NO_DIALOGS", "1")
    monkeypatch.setenv("CUECARRY_SETTINGS", str(tmp_path / "matching.ini"))
    w = gui.MainWindow()
    w.open_xml(xml)
    w.src_edit.setText(music)
    assert not w.settings().normalize
    assert not w.loudness_low_spin.isEnabled()
    w.flag_actions["normalize"].trigger()
    assert w.flag_boxes["normalize"].isChecked()
    assert w.loudness_low_spin.isEnabled()
    w.loudness_low_spin.setValue(-13)
    w.loudness_high_spin.setValue(-7)
    w.peak_spin.setValue(-1.5)
    root = w.tree.invisibleRootItem()
    item = next(root.child(i) for i in range(root.childCount()) if root.child(i).text(0) == "Set")
    item.setCheckState(0, Qt.Checked)
    plan = w.current_plan()
    assert plan.dst_root.endswith(" MP3 Matched")
    w.preview()
    assert "Gain is measured during Start" in w.log_view.toPlainText()
    w.start()
    assert wait_until(lambda: not w.worker.is_alive() and w.start_btn.isEnabled()), w.log_view.toPlainText()
    assert "Done." in w.log_view.toPlainText()
    assert "0 failed" in w.log_view.toPlainText()
    assert all(outputs.matches(job, w.settings()) for job in plan.jobs)
    assert not os.path.exists(music + " MP3")
    w.close()
    w2 = gui.MainWindow()
    assert w2.settings().normalize
    assert w2.loudness_low_spin.value() == -13
    assert w2.loudness_high_spin.value() == -7
    assert w2.peak_spin.value() == -1.5
    w2.set_flag("xml_only", True)
    assert w2.current_plan() is None
    assert "Turn off XML only" in w2.summary.text()
    w2.close()
