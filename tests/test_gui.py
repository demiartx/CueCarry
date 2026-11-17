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
