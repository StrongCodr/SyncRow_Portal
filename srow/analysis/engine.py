"""Interval orchestration (RESEARCH.md §3–§7).

The single entry point the CLI, worker, and backfill all call: build per-sensor
synthetic frames -> sign-align sensors to the stroke reference -> detect catches
-> cross-sensor signed offsets. Input is the streaming loader's per-source dict.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DEFAULT, AnalysisConfig
from .crosssensor import (
    CrossResult,
    align_signs,
    analyze_cross,
    is_rowing,
    seat_index,
)
from .detect import Catch, detect_catches
from .frame import SensorFrame, build_sensor_frame


@dataclass
class IntervalAnalysis:
    frames: dict[str, SensorFrame]
    catches: dict[str, list[Catch]]
    cross: CrossResult
    flipped: list[str]


def analyze_interval(by_source: dict[str, dict],
                     cfg: AnalysisConfig = DEFAULT) -> IntervalAnalysis:
    """by_source: {source: {"times_s","accel","gyro"}} from load_raw_by_source."""
    frames: dict[str, SensorFrame] = {}
    for src, d in by_source.items():
        f = build_sensor_frame(d["times_s"], d["accel"], d["gyro"], cfg)
        if f is not None:
            frames[src] = f

    rowers = [s for s, f in frames.items() if is_rowing(f)]
    flipped: list[str] = []
    if len(rowers) >= 2:
        ref = max(rowers, key=lambda s: seat_index(s) if seat_index(s) is not None else -1)
        flipped = align_signs(frames, ref, rowers, cfg)  # phase-align BEFORE detection

    catches = {
        s: detect_catches(f.times_s, f.signal, f.fs, f.dominant_hz, cfg)
        for s, f in frames.items()
    }
    cross = analyze_cross(frames, catches, cfg)
    return IntervalAnalysis(frames=frames, catches=catches, cross=cross, flipped=flipped)
