# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""Resume identity for loudness-matched copies; never trust file existence alone."""
from __future__ import annotations

import json
import os
import tempfile


def record_path(dst: str) -> str:
    return dst + ".cuecarry.json"


def fingerprint(path: str) -> dict:
    stat = os.stat(path)
    return {"path": os.path.normcase(os.path.realpath(path)),
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev, "inode": stat.st_ino}


def profile(settings) -> dict:
    return {"mode": settings.mode, "offset_ms": settings.offset_ms,
            "trim_samples": settings.trim_samples, "bitrate": settings.bitrate,
            "low": settings.loudness_low, "high": settings.loudness_high,
            "peak": settings.true_peak, "max_boost": settings.max_boost,
            "max_cut": settings.max_cut}


def read_record(dst: str) -> dict | None:
    try:
        with open(record_path(dst), encoding="utf-8") as stream:
            record = json.load(stream)
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


def matches(job, settings) -> bool:
    if not os.path.isfile(job.dst):
        return False
    if not settings.normalize:
        # A matched copy must be rebuilt from the original when switching off.
        return not os.path.exists(record_path(job.dst))
    record = read_record(job.dst)
    if not record or record.get("version") != 1:
        return False
    try:
        output = fingerprint(job.dst)
        return (record.get("profile") == profile(settings)
                and record.get("tags") == dict(job.track.element.attrib)
                and record.get("source") == fingerprint(job.track.path)
                and record.get("output") == output
                and isinstance(record.get("sample_rate"), int)
                and record["sample_rate"] in (8000, 11025, 12000, 16000, 22050,
                                               24000, 32000, 44100, 48000))
    except OSError:
        return False


def publish_record(dst: str, record: dict) -> None:
    """The audio was verified before publication; a missing record forces retry."""
    fd, tmp = tempfile.mkstemp(prefix=".cuecarry-", suffix=".json",
                               dir=os.path.dirname(dst) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, allow_nan=False, indent=2)
            stream.write("\n")
        os.replace(tmp, record_path(dst))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
