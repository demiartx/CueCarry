# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
cli.py - the command-line front end (same engine as the window).

Run:  python -m app collection.xml --compensate
      python -m app --help

C++ reading notes
  - `argparse` turns command-line flags into an object with one field per
    flag, like a getopt loop written for you.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import core, encoder
from .version import APP_NAME, VERSION


# =============================================================================
# 1. Flags
# What:  Every option the window has, as a command-line flag.
# Why:   Scripting and troubleshooting; same flag names as the original script,
#        so earlier commands keep working.
# If it goes wrong: run with --dry-run to see the plan without encoding.
# =============================================================================

def parse_args(argv):
    ap = argparse.ArgumentParser(prog="cuecarry", description=f"{APP_NAME} {VERSION}: "
                                 "make an MP3 copy of a rekordbox library with cues, grids, "
                                 "ratings and playlists.")
    ap.add_argument("xml", help="rekordbox collection XML (File > Export Collection in xml format)")
    ap.add_argument("--src-root", help="top folder of the source library (default: detected)")
    ap.add_argument("--dst-root", help='top folder for the MP3 copy (default: "<src-root> MP3")')
    ap.add_argument("--out", help="output XML (default: <dst-root>/rekordbox_mp3.xml)")
    ap.add_argument("--playlist", action="append", default=[], help="only this playlist (repeatable)")
    ap.add_argument("--only-playlists", action="store_true", help="skip tracks in no playlist")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--compensate", action="store_true",
                      help="RECOMMENDED (default): keep all audio, shift cues by the encoder delay")
    mode.add_argument("--align", action="store_true", help="trim the encoder delay off the audio")
    mode.add_argument("--offset-ms", type=float, help="shift cues by a fixed number of ms")
    ap.add_argument("--trim-samples", type=int, default=core.ENCODER_DELAY_SAMPLES,
                    help="samples to trim with --align (default 1105)")
    ap.add_argument("--bitrate", default="320k", choices=["320k", "256k", "192k"])
    ap.add_argument("--jobs", type=int, default=core.Settings.jobs)
    ap.add_argument("--overwrite", action="store_true", help="re-encode MP3s that already exist")
    ap.add_argument("--xml-only", action="store_true", help="don't encode; only rewrite the XML")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    ap.add_argument("--ffmpeg", help="path to ffmpeg (default: bundled copy, then PATH)")
    return ap.parse_args(argv)


def settings_from(a) -> core.Settings:
    if a.align:
        mode = "align"
    elif a.offset_ms is not None:
        mode = "offset"
    else:
        mode = "compensate"
    return core.Settings(xml_path=a.xml, src_root=a.src_root, dst_root=a.dst_root, out_xml=a.out,
                         playlists=set(a.playlist), only_playlists=a.only_playlists, mode=mode,
                         offset_ms=a.offset_ms or 0.0, trim_samples=a.trim_samples,
                         bitrate=a.bitrate, jobs=a.jobs, overwrite=a.overwrite,
                         xml_only=a.xml_only)


# =============================================================================
# 2. Running
# What:  Load, plan, print the summary, encode, write the XML.
# Why:   Same order as the window, so both behave the same.
# If it goes wrong: failed tracks are listed in <out>_failures.txt; run the
#        same command again to retry them.
# =============================================================================

def main(argv=None) -> int:
    a = parse_args(argv if argv is not None else sys.argv[1:])
    s = settings_from(a)
    lib = core.load_library(s.xml_path)
    if s.playlists:
        missing = s.playlists - {p.name for p in lib.playlists()}
        if missing:
            print(f"Playlist not found: {sorted(missing)}", file=sys.stderr)
            return 2
    try:
        plan = core.make_plan(lib, s)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    todo = plan.todo(s.overwrite)
    print(f"Source folder : {plan.src_root}\nMP3 folder    : {plan.dst_root}\nOutput XML    : {plan.out_xml}")
    print(f"To convert    : {len(plan.jobs)} ({len(todo)} to encode now)")
    print(f"Kept as-is    : {len(plan.keep)} MP3")
    print(f"Timing mode   : {core.MODE_LABELS[s.mode]}")
    if plan.skipped:
        t, why = plan.skipped[0]
        print(f"Skipped       : {len(plan.skipped)}, e.g. {t.path} ({why})")
    if a.dry_run:
        return 0

    ffmpeg, msg = encoder.find_ffmpeg(a.ffmpeg)
    if not ffmpeg:
        print(msg, file=sys.stderr)
        return 3
    result = encoder.RunResult()
    if not s.xml_only and todo:
        def progress(done, total, name, status):
            print(f"[{done}/{total}] {status}  {name}", flush=True)
        runner = encoder.Runner(ffmpeg, plan, s, progress)
        try:
            result = runner.run()
        except KeyboardInterrupt:
            runner.stop()
            print("\nStopped. Run the same command again to continue.")
            return 130
    n_tracks, n_pl = core.write_xml(lib, plan, s,
                                    lambda p: encoder.is_alac(ffmpeg, p))
    print(f"\nWrote {plan.out_xml}: {n_tracks} tracks, {n_pl} playlists.")
    if result.failures:
        log = os.path.splitext(plan.out_xml)[0] + "_failures.txt"
        encoder.write_failures(log, result.failures)
        print(f"{len(result.failures)} failed - see {log}. Run again to retry them.")
        return 1
    return 0
