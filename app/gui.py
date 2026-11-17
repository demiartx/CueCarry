# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
gui.py - the window (Qt, through PySide6).

PySide6 is the official Python binding of Qt. Every class here (QMainWindow,
QTreeWidget, QAction, signals and slots) is the same Qt class you would use in
C++, with the same names; Qt's C++ documentation applies directly.

C++ reading notes
  - `self` is `this`, written out explicitly as each method's first parameter.
  - `widget.clicked.connect(self.start)` is connect(widget, &QPushButton::clicked,
    this, &MainWindow::start).
  - `Signal(int, str)` declares a Qt signal, like `signals: void x(int, QString);`.
  - Python has no `new`/`delete`: Qt parent-child ownership still applies, and
    Python frees everything else automatically.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import List, Optional

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import core, encoder
from .version import APP_NAME, VERSION

IMPORT_STEPS = """<b>Importing the MP3 copy into rekordbox</b>
<ol>
<li>If rekordbox already has these MP3s from an earlier run, remove them from the
Collection first, so rekordbox takes the new cue data.</li>
<li>Open <b>Preferences &gt; Advanced &gt; Database &gt; rekordbox xml</b> and set
<b>Imported Library</b> to the output XML (shown in this window's log).</li>
<li>Open <b>Preferences &gt; View &gt; Layout</b> and turn on <b>rekordbox xml</b>.</li>
<li>In the sidebar, open <b>rekordbox xml &gt; Playlists</b>, right-click the
<b>MP3</b> folder and choose <b>Import Playlist</b>.</li>
<li>To import MP3s that are in no playlist: <b>rekordbox xml &gt; All Tracks</b>,
select all, right-click, <b>Import To Collection</b>.</li>
</ol>"""


# =============================================================================
# 1. Talking from the worker thread to the window
# What:  A QObject whose signals carry progress from the encoding thread.
# Why:   Qt widgets may only be touched from the main (GUI) thread. Emitting a
#        signal from another thread queues the call onto the GUI thread, the
#        same as Qt::QueuedConnection in C++.
# If it goes wrong: a frozen window means work ran on the GUI thread; all
#        encoding must stay inside Worker.run.
# =============================================================================

class Bridge(QObject):
    progress = Signal(int, int, str, str)     # done, total, file name, status
    finished = Signal(object)                 # dict with the outcome
    message = Signal(str)


class Worker(threading.Thread):
    def __init__(self, ffmpeg: str, lib: core.Library, plan: core.Plan,
                 s: core.Settings, bridge: Bridge):
        super().__init__(daemon=True)
        self.ffmpeg, self.lib, self.plan, self.s, self.bridge = ffmpeg, lib, plan, s, bridge
        self.runner: Optional[encoder.Runner] = None

    def stop(self) -> None:
        if self.runner:
            self.runner.stop()

    def run(self) -> None:
        out = {"error": None, "result": encoder.RunResult(), "tracks": 0, "playlists": 0}
        try:
            if not self.s.xml_only:
                self.runner = encoder.Runner(self.ffmpeg, self.plan, self.s,
                                             self.bridge.progress.emit)
                out["result"] = self.runner.run()
            if not out["result"].cancelled:
                self.bridge.message.emit("Writing the XML…")
                n, p = core.write_xml(self.lib, self.plan, self.s,
                                      lambda f: encoder.is_alac(self.ffmpeg, f))
                out["tracks"], out["playlists"] = n, p
                if out["result"].failures:
                    log = os.path.splitext(self.plan.out_xml)[0] + "_failures.txt"
                    encoder.write_failures(log, out["result"].failures)
                    out["failures_file"] = log
        except Exception as e:   # shown to the user instead of silently dying
            out["error"] = f"{type(e).__name__}: {e}"
        self.bridge.finished.emit(out)


# =============================================================================
# 2. The main window: layout
# What:  Builds every widget: file pickers, playlist tree, options, progress, log.
# Why:   One screen holds the whole workflow, top to bottom: choose the XML,
#        choose what to convert, choose how, preview, start.
# If it goes wrong: layout problems are cosmetic; the engine does not depend on
#        any widget.
# =============================================================================

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.resize(1100, 760)
        # Saved options live in the registry; tests point CUECARRY_SETTINGS at a
        # temporary .ini file instead so they never touch the user's settings.
        ini = os.environ.get("CUECARRY_SETTINGS")
        self.cfg = QSettings(ini, QSettings.IniFormat) if ini else QSettings("CueCarry", APP_NAME)
        self.lib: Optional[core.Library] = None
        self.worker: Optional[Worker] = None
        self.bridge = Bridge()
        self.bridge.progress.connect(self.on_progress)
        self.bridge.finished.connect(self.on_finished)
        self.bridge.message.connect(self.log)
        self.ffmpeg: Optional[str] = None
        self._build_widgets()
        self._build_menus()
        self._restore()
        self.find_ffmpeg()

    def _build_widgets(self) -> None:
        central = QWidget()
        v = QVBoxLayout(central)

        # Files ---------------------------------------------------------------
        files = QFormLayout()
        self.xml_edit = QLineEdit(readOnly=True, placeholderText="File > Open collection XML…")
        b = QPushButton("Open…")
        b.clicked.connect(self.open_xml)
        files.addRow("Collection XML", self._row(self.xml_edit, b))
        self.src_edit = QLineEdit(placeholderText="Detected automatically")
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: self.pick_folder(self.src_edit))
        files.addRow("Source folder", self._row(self.src_edit, b))
        self.dst_edit = QLineEdit(placeholderText='Default: source folder + " MP3"')
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: self.pick_folder(self.dst_edit))
        files.addRow("MP3 folder", self._row(self.dst_edit, b))
        self.out_edit = QLineEdit(placeholderText="Default: rekordbox_mp3.xml in the MP3 folder")
        self.out_edit.editingFinished.connect(self.update_summary)
        b = QPushButton("Browse…")
        b.clicked.connect(self.pick_out_xml)
        files.addRow("Output XML", self._row(self.out_edit, b))
        v.addLayout(files)

        split = QSplitter(Qt.Horizontal)

        # Playlists ---------------------------------------------------------------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("<b>Playlists</b> — check some to convert only those; "
                            "leave all unchecked for the whole collection"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Playlist", "Tracks"])
        self.tree.setColumnWidth(0, 380)
        self.tree.itemChanged.connect(lambda *_: self.update_summary())
        lv.addWidget(self.tree)
        split.addWidget(left)

        # Options -------------------------------------------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        timing = QGroupBox("Cue timing")
        tv = QVBoxLayout(timing)
        self.mode_radios = {}
        for m in core.MODES:
            r = QRadioButton(core.MODE_LABELS[m])
            r.toggled.connect(lambda on, m=m: on and self.set_mode(m))
            self.mode_radios[m] = r
            tv.addWidget(r)
        self.offset_spin = QDoubleSpinBox(suffix=" ms", minimum=-200, maximum=200, decimals=1)
        self.offset_spin.valueChanged.connect(lambda *_: self.update_summary())
        tv.addWidget(self._row(QLabel("Fixed offset:"), self.offset_spin))
        rv.addWidget(timing)

        enc = QGroupBox("Encoding")
        ef = QFormLayout(enc)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["320k", "256k", "192k"])
        self.bitrate_combo.currentTextChanged.connect(self.set_bitrate)
        ef.addRow("MP3 bitrate", self.bitrate_combo)
        self.jobs_spin = QSpinBox(minimum=1, maximum=64, value=core.Settings.jobs)
        ef.addRow("Tracks at once", self.jobs_spin)
        self.flag_boxes = {}
        for key, label in [("overwrite", "Re-encode MP3s that already exist"),
                           ("xml_only", "XML only (don't encode)"),
                           ("only_playlists", "Skip tracks that are in no playlist")]:
            c = QCheckBox(label)
            c.toggled.connect(lambda on, k=key: self.set_flag(k, on))
            self.flag_boxes[key] = c
            ef.addRow(c)
        rv.addWidget(enc)

        self.summary = QLabel("Open a collection XML to begin.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        rv.addWidget(self.summary)
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([620, 480])
        v.addWidget(split, 1)

        # Run controls ------------------------------------------------------------------
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.clicked.connect(self.preview)
        self.start_btn = QPushButton("Start")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop", enabled=False)
        self.stop_btn.clicked.connect(self.stop)
        self.progress = QProgressBar()
        v.addWidget(self._row(self.preview_btn, self.start_btn, self.stop_btn, self.progress))
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(20000)
        v.addWidget(self.log_view, 1)
        self.setCentralWidget(central)

    @staticmethod
    def _row(*widgets) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            h.addWidget(x, 1 if isinstance(x, (QLineEdit, QProgressBar)) else 0)
        return w

    # =========================================================================
    # 3. Menus
    # What:  File, Mode, Options, Tools and Help menus.
    # Why:   Every option on the right-hand panel is also in a menu; both stay
    #        in step through set_mode / set_flag / set_bitrate (section 4).
    # If it goes wrong: a menu tick that disagrees with the panel means a
    #        setter was bypassed; always change options through the setters.
    # =========================================================================

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        a = m.addAction("&Open collection XML…", self.open_xml)
        a.setShortcut(QKeySequence.Open)
        m.addAction("Open &MP3 folder", self.open_dst)
        m.addSeparator()
        m.addAction("E&xit", self.close)

        m = mb.addMenu("&Mode")
        self.mode_actions = {}
        group = QActionGroup(self)
        for mode in core.MODES:
            act = QAction(core.MODE_LABELS[mode], self, checkable=True)
            act.triggered.connect(lambda _=False, mo=mode: self.set_mode(mo))
            group.addAction(act)
            m.addAction(act)
            self.mode_actions[mode] = act

        m = mb.addMenu("&Options")
        self.flag_actions = {}
        for key in ("overwrite", "xml_only", "only_playlists"):
            act = QAction(self.flag_boxes[key].text(), self, checkable=True)
            act.triggered.connect(lambda on, k=key: self.set_flag(k, on))
            m.addAction(act)
            self.flag_actions[key] = act
        sub = m.addMenu("MP3 &bitrate")
        self.bitrate_actions = {}
        bgroup = QActionGroup(self)
        for br in ("320k", "256k", "192k"):
            act = QAction(br, self, checkable=True)
            act.triggered.connect(lambda _=False, b=br: self.set_bitrate(b))
            bgroup.addAction(act)
            sub.addAction(act)
            self.bitrate_actions[br] = act

        m = mb.addMenu("&Tools")
        m.addAction("Make &calibration tracks…", self.make_calibration)
        m.addAction("Check calibration &results…", self.check_calibration)
        m.addSeparator()
        m.addAction("Choose &ffmpeg…", self.choose_ffmpeg)

        m = mb.addMenu("&Help")
        m.addAction("How to &import into rekordbox", self.show_import_steps)
        m.addAction("&About and licenses", self.show_about)

    # =========================================================================
    # 4. Keeping options in step
    # What:  One setter per option updates the panel, the menu and the summary.
    # Why:   The same option appears in two places; one setter avoids drift.
    #        blockSignals stops the update from re-triggering itself (like
    #        QSignalBlocker in C++).
    # If it goes wrong: see section 3.
    # =========================================================================

    def set_mode(self, mode: str) -> None:
        for m, r in self.mode_radios.items():
            r.blockSignals(True)
            r.setChecked(m == mode)
            r.blockSignals(False)
            self.mode_actions[m].setChecked(m == mode) if hasattr(self, "mode_actions") else None
        self.offset_spin.setEnabled(mode == "offset")
        self.update_summary()

    def mode(self) -> str:
        return next((m for m, r in self.mode_radios.items() if r.isChecked()), "compensate")

    def set_flag(self, key: str, on: bool) -> None:
        box = self.flag_boxes[key]
        box.blockSignals(True)
        box.setChecked(on)
        box.blockSignals(False)
        if hasattr(self, "flag_actions"):
            self.flag_actions[key].setChecked(on)
        self.update_summary()

    def set_bitrate(self, br: str) -> None:
        self.bitrate_combo.blockSignals(True)
        self.bitrate_combo.setCurrentText(br)
        self.bitrate_combo.blockSignals(False)
        if hasattr(self, "bitrate_actions"):
            self.bitrate_actions[br].setChecked(True)

    # =========================================================================
    # 5. Loading the collection and building the plan
    # What:  Open the XML, fill the playlist tree, and turn the window's
    #        current options into core.Settings and a core.Plan.
    # Why:   Preview and Start both call make_plan, so the preview always
    #        matches what Start will do.
    # If it goes wrong: errors from the XML or the plan appear in a message box
    #        and the log, and nothing is written.
    # =========================================================================

    def open_xml(self, path: Optional[str] = None) -> None:
        if not path:
            start = os.path.dirname(self.xml_edit.text()) or os.path.expanduser("~")
            path, _ = QFileDialog.getOpenFileName(self, "Open rekordbox collection XML", start,
                                                  "rekordbox XML (*.xml);;All files (*)")
            if not path:
                return
        try:
            self.lib = core.load_library(path)
        except Exception as e:
            self.notify(APP_NAME, f"Could not read that XML:\n{e}")
            return
        self.xml_edit.setText(path)
        self.fill_tree()
        self.log(f"Loaded {path}: {len(self.lib.tracks)} tracks, "
                 f"{len(self.lib.playlists())} playlists.")
        self.update_summary()

    def fill_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()

        def add(parent, node: core.PlaylistNode) -> None:
            for c in node.children:
                it = QTreeWidgetItem(parent, [c.name, "" if c.is_folder else str(len(c.track_ids))])
                flags = it.flags() | Qt.ItemIsUserCheckable
                if c.is_folder:
                    flags |= Qt.ItemIsAutoTristate
                it.setFlags(flags)
                it.setCheckState(0, Qt.Unchecked)
                it.setData(0, Qt.UserRole, None if c.is_folder else c.name)
                if c.is_folder:
                    add(it, c)
        add(self.tree.invisibleRootItem(), self.lib.playlist_root)
        self.tree.expandAll()
        self.tree.blockSignals(False)

    def checked_playlists(self) -> set:
        names = set()

        def walk(it: QTreeWidgetItem) -> None:
            for i in range(it.childCount()):
                c = it.child(i)
                name = c.data(0, Qt.UserRole)
                if name is not None and c.checkState(0) == Qt.Checked:
                    names.add(name)
                walk(c)
        walk(self.tree.invisibleRootItem())
        return names

    def settings(self) -> core.Settings:
        return core.Settings(
            xml_path=self.xml_edit.text(),
            src_root=self.src_edit.text().strip() or None,
            dst_root=self.dst_edit.text().strip() or None,
            out_xml=self.out_edit.text().strip() or None,
            playlists=self.checked_playlists(),
            only_playlists=self.flag_boxes["only_playlists"].isChecked(),
            mode=self.mode(), offset_ms=self.offset_spin.value(),
            bitrate=self.bitrate_combo.currentText(), jobs=self.jobs_spin.value(),
            overwrite=self.flag_boxes["overwrite"].isChecked(),
            xml_only=self.flag_boxes["xml_only"].isChecked())

    def current_plan(self) -> Optional[core.Plan]:
        if not self.lib:
            return None
        try:
            return core.make_plan(self.lib, self.settings())
        except ValueError as e:
            self.summary.setText(f"<span style='color:#d33'>{e}</span>")
            return None

    def update_summary(self) -> None:
        if not hasattr(self, "summary") or not self.lib:
            return
        s = self.settings()
        plan = self.current_plan()
        if not plan:
            return
        todo = 0 if s.xml_only else len(plan.todo(s.overwrite))
        scope = (f"{len(s.playlists)} checked playlist(s)" if s.playlists
                 else "tracks in any playlist" if s.only_playlists else "the whole collection")
        self.summary.setText(
            f"<b>Converting {scope}</b><br>"
            f"Source: {plan.src_root}<br>MP3 folder: {plan.dst_root}<br>"
            f"Output XML: {plan.out_xml}<br>"
            f"Tracks to convert: <b>{len(plan.jobs)}</b> ({todo} to encode now)<br>"
            f"Already MP3, kept: {len(plan.keep)}<br>"
            f"Skipped: {len(plan.skipped)}<br>"
            f"Timing: {core.MODE_LABELS[s.mode]}")

    # =========================================================================
    # 6. Preview, Start, Stop
    # What:  Preview lists the plan in the log; Start runs it on a Worker
    #        thread; Stop asks the worker to finish its current tracks and quit.
    # Why:   The window stays responsive while encoding takes hours.
    # If it goes wrong: Stop then Start again picks up where it left off,
    #        because finished MP3s are skipped.
    # =========================================================================

    def preview(self) -> None:
        plan = self.current_plan()
        if not plan:
            return
        s = self.settings()
        self.log(f"--- Preview ---\nOutput XML: {plan.out_xml}")
        for j in plan.todo(s.overwrite)[:15]:
            self.log(f"  convert  {j.track.path}  →  {j.dst}")
        more = len(plan.todo(s.overwrite)) - 15
        if more > 0:
            self.log(f"  … and {more} more")
        for t, why in plan.skipped[:15]:
            self.log(f"  skip     {t.path}  ({why})")
        if len(plan.skipped) > 15:
            self.log(f"  … and {len(plan.skipped) - 15} more skipped")

    def start(self) -> None:
        if not self.lib:
            self.open_xml()
            if not self.lib:
                return
        if not self.ffmpeg:
            self.notify(APP_NAME, "No usable ffmpeg was found. Use Tools > Choose ffmpeg.")
            return
        s = self.settings()
        plan = self.current_plan()
        if not plan:
            return
        todo = 0 if s.xml_only else len(plan.todo(s.overwrite))
        if s.overwrite and todo > 50:
            if self.ask(APP_NAME, f"Re-encode {todo} tracks, including ones "
                                    "already converted?") != QMessageBox.Yes:
                return
        self._save()
        self.progress.setRange(0, max(1, todo))
        self.progress.setValue(0)
        self.set_running(True)
        self.log(f"--- Start: {todo} to encode → {plan.dst_root}")
        self.worker = Worker(self.ffmpeg, self.lib, plan, s, self.bridge)
        self.worker.start()

    def stop(self) -> None:
        if self.worker:
            self.log("Stopping… (finished tracks are kept)")
            self.worker.stop()

    def set_running(self, on: bool) -> None:
        for w in (self.preview_btn, self.start_btn, self.tree, self.menuBar()):
            w.setEnabled(not on)
        self.stop_btn.setEnabled(on)

    def on_progress(self, done: int, total: int, name: str, status: str) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.log(f"[{done}/{total}] {status}  {name}")

    def on_finished(self, out: dict) -> None:
        self.set_running(False)
        r: encoder.RunResult = out["result"]
        if out["error"]:
            self.log("ERROR: " + out["error"])
            self.notify(APP_NAME, out["error"])
        elif r.cancelled:
            self.log("Stopped. Press Start to continue where it left off.")
        else:
            msg = (f"Done. {r.converted} converted, {r.kept_aac} AAC kept as-is, "
                   f"{len(r.failures)} failed.\nXML: {out['tracks']} tracks, "
                   f"{out['playlists']} playlists → {self.worker.plan.out_xml}")
            self.log(msg)
            if r.failures:
                self.log(f"Failed tracks are listed in {out.get('failures_file')}")
            self.notify(APP_NAME, msg + "\n\nNext: import the XML into rekordbox "
                        "(Help > How to import).")
        self.update_summary()

    # =========================================================================
    # 7. Tools: calibration and ffmpeg
    # What:  Make the four calibration tracks, read the results back, and let
    #        the user pick a different ffmpeg.
    # Why:   Lets any DJ verify the cue timing on their own rekordbox version,
    #        the same test that found rekordbox ignores the MP3 header.
    # If it goes wrong: see encoder.py section 5.
    # =========================================================================

    def make_calibration(self) -> None:
        if not self.ffmpeg:
            return self.choose_ffmpeg()
        folder = QFileDialog.getExistingDirectory(self, "Folder for the calibration tracks")
        if not folder:
            return
        try:
            encoder.make_calibration_tracks(self.ffmpeg, os.path.join(folder, "calibration"))
        except Exception as e:
            self.notify(APP_NAME, str(e))
            return
        self.notify(APP_NAME, (
            "Made 4 calibration tracks in:\n" + os.path.join(folder, "calibration") + "\n\n"
            "1. Drag them into rekordbox and let it analyze them.\n"
            "2. File > Export Collection in xml format.\n"
            "3. Tools > Check calibration results, and pick that XML."))

    def check_calibration(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Exported collection XML", "", "XML (*.xml)")
        if not path:
            return
        try:
            rows = core.read_calibration(path)
        except Exception as e:
            self.notify(APP_NAME, str(e))
            return
        if not rows:
            self.notify(APP_NAME, "No calibration tracks found in that XML.")
            return
        lines = [f"{t}: grid {g if g is not None else 'not analyzed'}"
                 + (f", {sh:+.1f} ms vs FLAC" if sh is not None else "") for t, g, sh in rows]
        shift = {t[:5]: sh for t, _, sh in rows}
        verdict = ""
        if shift.get("CAL C") is not None and shift.get("CAL D") is not None:
            if abs(shift["CAL D"]) <= 2 and abs(shift["CAL C"] - 25) <= 3:
                verdict = "\n\nResult: Compensate and Align are both exact on this rekordbox."
            else:
                verdict = (f"\n\nResult: this rekordbox differs from the tested one. "
                           f"Try Fixed offset {shift['CAL C']:.0f} ms, and please report it.")
        self.notify(APP_NAME, "\n".join(lines) + verdict)

    def find_ffmpeg(self) -> None:
        self.ffmpeg, msg = encoder.find_ffmpeg(self.cfg.value("ffmpeg") or None)
        self.statusBar().showMessage(msg if self.ffmpeg else "⚠ " + msg.splitlines()[0])
        self.log(msg)

    def choose_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose ffmpeg", "",
                                              "ffmpeg (ffmpeg.exe ffmpeg);;All files (*)")
        if path:
            if encoder.has_mp3_encoder(path):
                self.cfg.setValue("ffmpeg", path)
                self.find_ffmpeg()
            else:
                self.notify(APP_NAME, "That ffmpeg has no MP3 encoder (libmp3lame).")

    # =========================================================================
    # 8. Small helpers: folders, log, help, saved settings
    # What:  Folder and file pickers, the log, Help dialogs, and remembering options
    #        between runs (QSettings: the registry on Windows).
    # Why:   Convenience; nothing here affects the conversion itself.
    # =========================================================================

    def pick_folder(self, edit: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text() or "")
        if d:
            edit.setText(d.replace("\\", "/"))
            self.update_summary()

    def pick_out_xml(self) -> None:
        plan = self.current_plan()
        start = self.out_edit.text() or (plan.out_xml if plan else "")
        # No overwrite prompt: re-writing the previous output is the normal case,
        # and make_plan already refuses the collection XML itself.
        path, _ = QFileDialog.getSaveFileName(self, "Where to write the MP3 XML", start,
                                              "rekordbox XML (*.xml)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.out_edit.setText(path.replace("\\", "/"))
            self.update_summary()

    def open_dst(self) -> None:
        plan = self.current_plan()
        if plan and os.path.isdir(plan.dst_root):
            QDesktopServices.openUrl(QUrl.fromLocalFile(plan.dst_root))

    def log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    # Every dialog goes through notify/ask. With CUECARRY_NO_DIALOGS set (the
    # tests set it), they only write to the log, so a test can never hang on
    # a dialog nobody can click.
    def notify(self, title: str, text: str) -> None:
        if os.environ.get("CUECARRY_NO_DIALOGS"):
            self.log(f"[dialog] {title}: {text}")
            return
        QMessageBox.information(self, title, text)

    def ask(self, title: str, text: str) -> QMessageBox.StandardButton:
        if os.environ.get("CUECARRY_NO_DIALOGS"):
            self.log(f"[question, answered Yes] {title}: {text}")
            return QMessageBox.Yes
        return QMessageBox.question(self, title, text)

    def show_import_steps(self) -> None:
        self.notify("Import into rekordbox", IMPORT_STEPS)

    def show_about(self) -> None:
        self.notify(f"About {APP_NAME}", (
            f"<b>{APP_NAME} {VERSION}</b><br>Makes an MP3 copy of a rekordbox library "
            "with cues, grids, ratings and playlists.<br><br>"
            "License: GNU GPL v3.0 or later (see LICENSE).<br>"
            "Uses FFmpeg (LGPL build) and Qt for Python (LGPL). Their licenses are in "
            "the LICENSES folder.<br>"
            "rekordbox is a trademark of AlphaTheta Corporation. This app is not "
            "affiliated with AlphaTheta."))

    def _restore(self) -> None:
        self.set_mode(self.cfg.value("mode", "compensate"))
        self.set_bitrate(self.cfg.value("bitrate", "320k"))
        self.jobs_spin.setValue(int(self.cfg.value("jobs", core.Settings.jobs)))
        self.offset_spin.setValue(float(self.cfg.value("offset_ms", 0.0)))
        self.src_edit.setText(self.cfg.value("src", ""))
        self.dst_edit.setText(self.cfg.value("dst", ""))
        self.out_edit.setText(self.cfg.value("out", ""))
        last =self.cfg.value("xml", "")
        if last and os.path.exists(last):
            self.open_xml(last)

    def _save(self) -> None:
        for k, v in [("mode", self.mode()), ("bitrate", self.bitrate_combo.currentText()),
                     ("jobs", self.jobs_spin.value()), ("offset_ms", self.offset_spin.value()),
                     ("src", self.src_edit.text()), ("dst", self.dst_edit.text()),
                     ("out", self.out_edit.text()), ("xml", self.xml_edit.text())]:
            self.cfg.setValue(k, v)

    def closeEvent(self, ev) -> None:
        if self.worker and self.worker.is_alive():
            if self.ask(APP_NAME, "A conversion is running. Stop it and quit?") \
                    != QMessageBox.Yes:
                ev.ignore()
                return
            self.worker.stop()
            self.worker.join(timeout=10)
        self._save()
        ev.accept()


def main(argv: Optional[List[str]] = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    w.show()
    return app.exec()
