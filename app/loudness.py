# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""Loudness decisions: move outliers to a useful range using constant gain.

FFmpeg measures the audio; this module never compresses or limits the signal.
Missing loudness on silent/ungated material means no loudness boost, not a
request for infinite gain. Peak protection still applies where measurable.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Measurement:
    integrated: float | None
    peak: float | None
    samples: int


def parse_measurement(stderr: str) -> Measurement:
    """Read loudnorm input measurements and astats' decoded sample count."""
    try:
        start = stderr.rfind('{')
        if start < 0:
            raise ValueError("missing loudness measurements")
        values, _ = json.JSONDecoder().raw_decode(stderr[start:])

        def number(key):
            value = float(values[key])
            if value == -math.inf:
                return None
            if not math.isfinite(value):
                raise ValueError("non-finite measurement")
            return value

        counts = re.findall(r"Number of samples:\s*(\d+)", stderr)
        samples = int(counts[-1]) if counts else 0
        if samples <= 0:
            raise ValueError("no decoded samples")
        result = Measurement(number("input_i"), number("input_tp"), samples)
        if result.integrated is not None and result.peak is None:
            raise ValueError("missing true peak")
        return result
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Could not measure audio safely: {e}") from e


def choose_gain(measured: Measurement, low: float, high: float, peak_ceiling: float,
                max_boost: float, max_cut: float) -> tuple[float, str]:
    level = measured.integrated
    if level is None:
        wanted, reason = 0.0, "unmeasurable loudness; no boost"
    elif level < low:
        wanted, reason = low - level, "raised toward range"
    elif level > high:
        wanted, reason = high - level, "lowered toward range"
    else:
        wanted, reason = 0.0, "already in range"
    gain = max(-max_cut, min(max_boost, wanted))
    if gain != wanted:
        reason += "; adjustment capped"
    if measured.peak is not None:
        # Reserve a little room for peaks introduced by MP3 encoding. The
        # decoded MP3 is measured again; this is not a clipping guarantee.
        safe = peak_ceiling - 0.3 - measured.peak
        if safe < -max_cut:
            raise ValueError("Peak protection needs more attenuation than the maximum cut; audio left untouched.")
        if gain > safe:
            gain, reason = safe, reason + "; peak headroom limited"
    return gain, reason


def preprocessing(rate: int, mode: str, trim_samples: int) -> list[str]:
    """Measure and encode exactly the same resampling and timing changes."""
    filters = [f"aresample={rate}"]
    if mode == "align":
        filters += [f"atrim=start_sample={trim_samples}", "asetpts=PTS-STARTPTS"]
    return filters


def measurement_filter(filters: list[str]) -> str:
    return ','.join(filters + ["astats=metadata=0:reset=0",
                               "loudnorm=I=-10:TP=-1:LRA=50:print_format=json"])
