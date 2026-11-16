# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
Makes docs/screenshot.png from a made-up demo library (no real files needed).
Run:  QT_QPA_PLATFORM=offscreen python tools/make_screenshot.py
"""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtCore import Qt                    # noqa: E402
from PySide6.QtWidgets import QApplication       # noqa: E402

from app import core, gui                        # noqa: E402

PLAYLISTS = {"Warm-up": 18, "Peak time": 24, ("Gigs", "Friday 11-14"): 31,
             ("Gigs", "Saturday 11-15"): 27, "Afterhours": 15}

root = ET.Element("DJ_PLAYLISTS")
col = ET.SubElement(root, "COLLECTION")
tid = 0
ids = {}
for key, n in PLAYLISTS.items():
    ids[key] = []
    for i in range(n):
        tid += 1
        ext = [".flac", ".flac", ".flac", ".aiff", ".wav", ".m4a", ".mp3"][tid % 7]
        ET.SubElement(col, "TRACK", {"TrackID": str(tid), "Name": f"Track {tid}",
                                     "SampleRate": "44100",
                                     "Location": core.path_to_loc(f"D:/DJ Music/Artist {tid % 40}/Track {tid}{ext}")})
        ids[key].append(str(tid))
pl = ET.SubElement(ET.SubElement(root, "PLAYLISTS"), "NODE", {"Type": "0", "Name": "ROOT"})
folders = {}
for key, tracks in ids.items():
    parent = pl
    name = key
    if isinstance(key, tuple):
        if key[0] not in folders:
            folders[key[0]] = ET.SubElement(pl, "NODE", {"Type": "0", "Name": key[0]})
        parent = folders[key[0]]
        name = key[1]
    node = ET.SubElement(parent, "NODE", {"Type": "1", "Name": name, "KeyType": "0"})
    for t in tracks:
        ET.SubElement(node, "TRACK", {"Key": t})
xml = os.path.join(tempfile.mkdtemp(), "collection.xml")
ET.ElementTree(root).write(xml, encoding="UTF-8", xml_declaration=True)

os.environ["CUECARRY_SETTINGS"] = os.path.join(tempfile.mkdtemp(), "settings.ini")
app = QApplication([])
w = gui.MainWindow()
w.log_view.clear()
w.resize(1200, 800)
w.open_xml(xml)
w.set_mode("compensate")
top = w.tree.invisibleRootItem()
for i in range(top.childCount()):
    if top.child(i).text(0) == "Peak time":
        top.child(i).setCheckState(0, Qt.Checked)
w.src_edit.setText("D:/DJ Music")
w.update_summary()
w.preview()
w.xml_edit.setText("D:/DJ Music/collection.xml")
w.log_view.moveCursor(w.log_view.textCursor().MoveOperation.Start)
w.statusBar().showMessage("Using ffmpeg: bundled with the app")
w.show()
app.processEvents()
out = os.path.join(os.path.dirname(__file__), "..", "docs", "screenshot.png")
os.makedirs(os.path.dirname(out), exist_ok=True)
w.grab().save(out)
print("saved", os.path.abspath(out))
