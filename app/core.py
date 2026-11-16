# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
core.py - the engine: read a rekordbox XML, plan the conversion, write the new XML.

This file has no user interface code. Both the window (gui.py) and the command
line (cli.py) call into it, the way a C++ program might keep its logic in a
library that two front ends link against.

C++ reading notes
  - A `@dataclass` is a struct whose constructor is written for you.
  - `list`, `dict`, `set` are roughly std::vector, std::unordered_map, std::unordered_set.
  - `Optional[str]` means "a string or None" (None is Python's nullptr).
  - Indentation is the block structure: there are no braces.
"""

from __future__ import annotations

import os
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

# =============================================================================
# 1. Constants
# What:  File types the app understands, and the MP3 encoder's start delay.
# Why:   Every decision below keys off these, so they live in one place.
# If it goes wrong: a file type missing here is reported as "unsupported" in
#        the preview instead of being converted.
# =============================================================================

PREFIX = "file://localhost"
URL_SAFE = "/:,()!;+#="          # characters rekordbox leaves unencoded in Location

LOSSLESS_EXTS = {".flac", ".wav", ".aif", ".aiff"}   # always converted
PROBE_EXTS = {".m4a", ".mp4", ".alac"}               # ALAC is converted, AAC is kept
KEEP_EXTS = {".mp3"}                                 # already MP3: used where it is

# LAME (the MP3 encoder inside ffmpeg) puts 1105 samples of extra audio at the
# start of every MP3: 576 samples of encoder delay plus 529 of decoder delay.
# Measured in rekordbox 7.2: it does not skip them, so MP3 audio plays
# 25.1 ms (44.1 kHz) / 23.0 ms (48 kHz) later than the source.
ENCODER_DELAY_SAMPLES = 1105

MODES = ("compensate", "align", "offset")
MODE_LABELS = {
    "compensate": "Compensate (recommended): keep all audio, shift cues",
    "align": "Align: trim the delay off the audio",
    "offset": "Fixed offset: shift cues by a set number of ms",
}


# =============================================================================
# 2. Paths and rekordbox "Location" URLs
# What:  Convert between rekordbox's file://localhost/... URLs and normal paths.
# Why:   rekordbox stores every track as a URL; ffmpeg needs a file path.
# If it goes wrong: tracks show up as "source file missing". Check that the
#        XML was exported on the same computer that runs the conversion.
# =============================================================================

def loc_to_path(loc: str) -> str:
    """rekordbox Location URL -> file path with forward slashes.

    Windows:  file://localhost/J:/Music/a.flac  ->  J:/Music/a.flac
    macOS:    file://localhost/Users/me/a.flac  ->  /Users/me/a.flac
    """
    p = loc[len(PREFIX):] if loc.startswith(PREFIX) else loc.replace("file://", "", 1)
    p = urllib.parse.unquote(p)
    if re.match(r"^/[A-Za-z]:/", p):
        p = p[1:]
    return p


def path_to_loc(p: str) -> str:
    """File path -> rekordbox Location URL (the reverse of loc_to_path)."""
    p = p.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return PREFIX + urllib.parse.quote(p, safe=URL_SAFE)


def norm(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def is_under(path: str, root: str) -> bool:
    """True if `path` is inside folder `root`. Case-insensitive, like Windows."""
    a, b = norm(path).lower(), norm(root).lower()
    return a == b or a.startswith(b + "/")


def common_folder(paths: List[str]) -> str:
    """Deepest folder that contains every path (case-insensitive compare)."""
    if not paths:
        return ""
    split = [norm(posixpath.dirname(norm(p))).split("/") for p in paths]
    first = split[0]
    n = len(first)
    for parts in split[1:]:
        n = min(n, len(parts))
        for i in range(n):
            if parts[i].lower() != first[i].lower():
                n = i
                break
    return "/".join(first[:n]) or "/"


def _volume(path: str) -> str:
    """Which drive a path is on: 'j:' on Windows, '/volumes/usb' or '/users' on macOS."""
    parts = norm(path).lower().split("/")
    if re.match(r"^[a-z]:$", parts[0]):
        return parts[0]
    if path.startswith("//") and len(parts) > 2:
        return "//" + parts[2]                       # network share
    if len(parts) > 2 and parts[1] == "volumes":
        return "/volumes/" + parts[2]
    return "/" + (parts[1] if len(parts) > 1 else "")


def detect_src_root(paths: List[str]) -> str:
    """Common folder of the tracks on the drive that holds the most of them.

    Tracks on other drives (for example rekordbox's sampler sounds on C:) are
    then reported as "outside the source folder" instead of forcing the
    source folder up to nothing.
    """
    if not paths:
        return ""
    groups: Dict[str, List[str]] = {}
    for p in paths:
        groups.setdefault(_volume(p), []).append(p)
    biggest = max(groups.values(), key=len)
    return common_folder(biggest)


def mp3_path_for(src: str, src_root: str, dst_root: str) -> str:
    """Same subfolders under dst_root, extension changed to .mp3."""
    rel = norm(src)[len(norm(src_root)):].lstrip("/")
    stem, _ = posixpath.splitext(rel)
    return posixpath.join(norm(dst_root), stem + ".mp3")


# =============================================================================
# 3. Reading the collection
# What:  Load the XML into Track objects and a playlist tree.
# Why:   The window shows playlists as checkboxes and the plan needs per-track
#        file paths; parsing once and keeping the elements avoids re-reading.
# If it goes wrong: "Not a rekordbox collection XML" means the file has no
#        COLLECTION section. Export it again with File > Export Collection in
#        xml format.
# =============================================================================

@dataclass
class Track:
    track_id: str
    path: str                  # source file path
    ext: str                   # ".flac", ".wav", ...
    element: ET.Element        # the original <TRACK> element, kept for copying


@dataclass
class PlaylistNode:
    name: str
    is_folder: bool
    track_ids: List[str] = field(default_factory=list)
    children: List["PlaylistNode"] = field(default_factory=list)


@dataclass
class Library:
    xml_path: str
    tracks: Dict[str, Track]
    playlist_root: PlaylistNode

    def playlists(self) -> List[PlaylistNode]:
        """Every playlist (not folders), depth-first."""
        out: List[PlaylistNode] = []

        def walk(n: PlaylistNode) -> None:
            for c in n.children:
                if c.is_folder:
                    walk(c)
                else:
                    out.append(c)
        walk(self.playlist_root)
        return out


def load_library(xml_path: str) -> Library:
    root = ET.parse(xml_path).getroot()
    col = root.find("COLLECTION")
    if col is None:
        raise ValueError("Not a rekordbox collection XML (no COLLECTION section).")
    tracks: Dict[str, Track] = {}
    for el in col.findall("TRACK"):
        loc = el.get("Location", "")
        path = loc_to_path(loc)
        tracks[el.get("TrackID")] = Track(el.get("TrackID"), path,
                                          posixpath.splitext(path)[1].lower(), el)

    def build(node: ET.Element) -> PlaylistNode:
        if node.get("Type") == "0":
            pn = PlaylistNode(node.get("Name", ""), True)
            pn.children = [build(c) for c in node.findall("NODE")]
            return pn
        return PlaylistNode(node.get("Name", ""), False,
                            [t.get("Key") for t in node.findall("TRACK")])

    pl = root.find("PLAYLISTS")
    top = pl.find("NODE") if pl is not None else None
    proot = build(top) if top is not None else PlaylistNode("ROOT", True)
    return Library(xml_path, tracks, proot)


# =============================================================================
# 4. Settings and the plan
# What:  `Settings` holds every option; `make_plan` decides, per track, whether
#        it is converted, kept as-is, or skipped (with a reason).
# Why:   The preview, the conversion and the XML writer all work from the same
#        plan, so what you preview is what runs.
# If it goes wrong: tracks "outside the source folder" are skipped. Choose a
#        source folder that contains all of them (for example the drive root).
# =============================================================================

@dataclass
class Settings:
    xml_path: str
    src_root: Optional[str] = None       # None = detect automatically
    dst_root: Optional[str] = None       # None = "<src_root> MP3"
    out_xml: Optional[str] = None        # None = "<dst_root>/rekordbox_mp3.xml"
    playlists: Set[str] = field(default_factory=set)   # empty = whole collection
    only_playlists: bool = False
    mode: str = "compensate"
    offset_ms: float = 0.0
    trim_samples: int = ENCODER_DELAY_SAMPLES
    bitrate: str = "320k"
    jobs: int = max(1, (os.cpu_count() or 2) - 1)
    overwrite: bool = False
    xml_only: bool = False


@dataclass
class Job:
    track: Track
    dst: str                   # MP3 path
    probe: bool                # True for .m4a: convert only if the codec is ALAC


@dataclass
class Plan:
    src_root: str
    dst_root: str
    out_xml: str
    jobs: List[Job]
    keep: List[Track]          # used at their current location
    skipped: List[tuple]       # (Track, reason)

    def todo(self, overwrite: bool) -> List[Job]:
        return [j for j in self.jobs if overwrite or not os.path.exists(j.dst)]


def tracks_in_scope(lib: Library, s: Settings) -> Set[str]:
    if s.playlists:
        ids: Set[str] = set()
        for p in lib.playlists():
            if p.name in s.playlists:
                ids.update(p.track_ids)
        return ids
    if s.only_playlists:
        return {k for p in lib.playlists() for k in p.track_ids}
    return set(lib.tracks)


def make_plan(lib: Library, s: Settings) -> Plan:
    convertible = [t for t in lib.tracks.values() if t.ext in LOSSLESS_EXTS | PROBE_EXTS]
    # Detect the source folder from the WHOLE collection, not just the chosen
    # playlists, so a test run and the full run put files in the same place.
    src_root = norm(s.src_root) if s.src_root else detect_src_root([t.path for t in convertible])
    dst_root = norm(s.dst_root) if s.dst_root else src_root + " MP3"
    if is_under(dst_root, src_root):
        raise ValueError("The MP3 folder must not be inside the source folder.")
    out_xml = s.out_xml or posixpath.join(dst_root, "rekordbox_mp3.xml")

    jobs: List[Job] = []
    keep: List[Track] = []
    skipped: List[tuple] = []
    for tid in tracks_in_scope(lib, s):
        t = lib.tracks.get(tid)
        if t is None:
            continue
        if t.ext in KEEP_EXTS:
            keep.append(t)
        elif t.ext in LOSSLESS_EXTS | PROBE_EXTS:
            if is_under(t.path, src_root):
                jobs.append(Job(t, mp3_path_for(t.path, src_root, dst_root), t.ext in PROBE_EXTS))
            else:
                skipped.append((t, "outside the source folder"))
        else:
            skipped.append((t, f"unsupported file type ({t.ext or 'none'})"))
    jobs.sort(key=lambda j: j.track.path.lower())
    return Plan(src_root, dst_root, out_xml, jobs, keep, skipped)


# =============================================================================
# 5. Cue shifting
# What:  How far to move every cue, loop and grid point for one track.
# Why:   The MP3 starts later than the source by the encoder delay (section 1).
#        "compensate" removes the header so every player sees that same delay,
#        then shifts by exactly 1105 samples at the MP3's sample rate.
# If it goes wrong: cues land early or late by ~25 ms. Run Tools > Make
#        calibration tracks to measure what your rekordbox does.
# =============================================================================

def mp3_sample_rate(source_rate: int) -> int:
    """MP3 tops out at 48 kHz; higher rates are resampled (see encoder.py)."""
    if source_rate <= 48000:
        return source_rate
    return 44100 if source_rate % 44100 == 0 else 48000


def cue_shift_seconds(track: Track, s: Settings) -> float:
    if s.mode == "compensate":
        rate = mp3_sample_rate(int(float(track.element.get("SampleRate") or 44100)))
        return ENCODER_DELAY_SAMPLES / rate
    if s.mode == "offset":
        return s.offset_ms / 1000.0
    return 0.0  # align: the audio itself is moved, cues stay put


# =============================================================================
# 6. Writing the new XML
# What:  Build a rekordbox XML with only the MP3 tracks, every playlist copied
#        into a folder named "MP3", and cues shifted.
# Why:   rekordbox imports tracks with their cues and grid from an XML; this is
#        the only supported way to hand it that data.
# If it goes wrong: if rekordbox already has these MP3s, it can keep the old
#        cue data. Remove them from the Collection, then import again.
# =============================================================================

def _shift(el: ET.Element, attr: str, sec: float) -> None:
    v = el.get(attr)
    if v is not None:
        el.set(attr, f"{max(0.0, float(v) + sec):.3f}")


def mp3_track_element(t: Track, mp3_path: str, shift: float, bitrate_kbps: int) -> ET.Element:
    el = ET.Element("TRACK", dict(t.element.attrib))
    el.set("Kind", "MP3 File")
    el.set("Location", path_to_loc(mp3_path))
    el.set("BitRate", str(bitrate_kbps))
    el.set("Size", str(os.path.getsize(mp3_path)))
    rate = int(float(t.element.get("SampleRate") or 44100))
    el.set("SampleRate", str(mp3_sample_rate(rate)))
    for child in t.element:
        c = ET.SubElement(el, child.tag, dict(child.attrib))
        if shift:
            if child.tag == "TEMPO":
                _shift(c, "Inizio", shift)
            elif child.tag == "POSITION_MARK":
                _shift(c, "Start", shift)
                _shift(c, "End", shift)
    return el


def _copy_tree(node: PlaylistNode, keep_ids: Set[str], wanted: Set[str]) -> List[ET.Element]:
    out: List[ET.Element] = []
    for n in node.children:
        if n.is_folder:
            kids = _copy_tree(n, keep_ids, wanted)
            if kids:
                f = ET.Element("NODE", {"Type": "0", "Name": n.name, "Count": str(len(kids))})
                f.extend(kids)
                out.append(f)
        elif not wanted or n.name in wanted:
            keys = [k for k in n.track_ids if k in keep_ids]
            p = ET.Element("NODE", {"Name": n.name + " (MP3)", "Type": "1",
                                    "KeyType": "0", "Entries": str(len(keys))})
            for k in keys:
                ET.SubElement(p, "TRACK", {"Key": k})
            out.append(p)
    return out


def write_xml(lib: Library, plan: Plan, s: Settings,
              is_alac: Callable[[str], bool]) -> tuple:
    """Write plan.out_xml. Returns (number of tracks, number of playlists).

    is_alac(path) decides .m4a files that were never converted: ALAC files
    without an MP3 yet are left out; AAC files are kept as they are.
    """
    kbps = int(re.sub(r"\D", "", s.bitrate) or 320)
    tracks: List[ET.Element] = []
    ids: Set[str] = set()
    for j in plan.jobs:
        if os.path.exists(j.dst):
            tracks.append(mp3_track_element(j.track, j.dst, cue_shift_seconds(j.track, s), kbps))
            ids.add(j.track.track_id)
        elif j.probe and os.path.exists(j.track.path) and not is_alac(j.track.path):
            tracks.append(j.track.element)     # AAC: keep where it is
            ids.add(j.track.track_id)
    for t in plan.keep:
        tracks.append(t.element)
        ids.add(t.track_id)

    out = ET.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
    ET.SubElement(out, "PRODUCT", {"Name": "rekordbox", "Version": "7", "Company": "AlphaTheta"})
    col = ET.SubElement(out, "COLLECTION", {"Entries": str(len(tracks))})
    col.extend(tracks)
    pls = ET.SubElement(out, "PLAYLISTS")
    root = ET.SubElement(pls, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
    kids = _copy_tree(lib.playlist_root, ids, s.playlists)
    folder = ET.SubElement(root, "NODE", {"Type": "0", "Name": "MP3", "Count": str(len(kids))})
    folder.extend(kids)
    ET.indent(out)
    os.makedirs(os.path.dirname(plan.out_xml) or ".", exist_ok=True)
    ET.ElementTree(out).write(plan.out_xml, encoding="UTF-8", xml_declaration=True)
    return len(tracks), _count_playlists(kids)


def _count_playlists(nodes: List[ET.Element]) -> int:
    return sum(_count_playlists(list(n)) if n.get("Type") == "0" else 1 for n in nodes)


# =============================================================================
# 7. Reading calibration results
# What:  Find the four calibration tracks in an exported collection XML and
#        report how far each one's beat grid sits from the FLAC reference.
# Why:   rekordbox's grid start (TEMPO Inizio) is precise to 1 ms, which beats
#        judging waveforms by eye.
# If it goes wrong: "not found" means rekordbox hasn't analyzed the tracks yet,
#        or the collection was exported before they were imported.
# =============================================================================

def read_calibration(xml_path: str) -> List[tuple]:
    """Returns [(title, grid start in s or None, shift vs reference in ms or None)]."""
    lib = load_library(xml_path)
    found: Dict[str, Optional[float]] = {}
    for t in lib.tracks.values():
        title = t.element.get("Name", "")
        if title.startswith("CAL ") and t.element.get("Artist") == "Calibration":
            tempo = t.element.find("TEMPO")
            found[title] = float(tempo.get("Inizio")) if tempo is not None else None
    ref = next((v for k, v in found.items() if k.startswith("CAL A")), None)
    rows = []
    for title in sorted(found):
        v = found[title]
        shift = None
        if v is not None and ref is not None:
            d = (v - ref) % 0.5          # beats are 0.5 s apart at 120 BPM
            if d > 0.25:
                d -= 0.5
            shift = round(d * 1000, 1)
        rows.append((title, v, shift))
    return rows
