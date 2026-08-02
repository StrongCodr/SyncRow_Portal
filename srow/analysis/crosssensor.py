"""Cross-sensor synchronicity (RESEARCH.md §7).

Reference = the stroke seat (highest seat index). For each reference catch, match
the nearest catch on every other rowing sensor within tolerance, and report each
seat's SIGNED offset vs stroke (stroke = 0) plus the crew spread. Non-rowing
sensors (e.g. the cox, or anything with no in-band stroke rhythm) are excluded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from . import validity
from .detect import Catch
from .frame import SensorFrame


def seat_index(source: str) -> int | None:
    """Parse a seat number from a source label ('Seat 4 (...)' -> 4). Cox -> None."""
    m = re.search(r"seat\s*(\d+)", source, re.IGNORECASE)
    return int(m.group(1)) if m else None


def is_rowing(frame: SensorFrame) -> bool:
    """A sensor is 'rowing' if its dominant frequency sits in the stroke band."""
    lo, hi = validity.STROKE_BAND_HZ
    return lo <= frame.dominant_hz <= hi


@dataclass
class StrokeSync:
    stroke_time_s: float                 # reference (stroke seat) catch time
    spm: float                           # 60 / interval to previous stroke catch
    offsets_ms: dict[str, float]         # per seat, signed vs stroke (stroke ~ 0)
    uncertainty_ms: dict[str, float]     # per-seat catch timing uncertainty
    spread_ms: float                     # max-min across matched seats
    n_matched: int
    flags: list[str] = field(default_factory=list)


@dataclass
class CrossResult:
    reference: str | None
    rowers: list[str]
    excluded: list[str]
    strokes: list[StrokeSync]


def analyze_cross(frames: dict[str, SensorFrame],
                  catches: dict[str, list[Catch]]) -> CrossResult:
    rowers = [s for s, f in frames.items() if is_rowing(f)]
    excluded = [s for s in frames if s not in rowers]
    if len(rowers) < 2:
        return CrossResult(reference=None, rowers=rowers, excluded=excluded, strokes=[])

    # Reference = highest seat index among rowing sensors (stroke seat).
    ref = max(rowers, key=lambda s: (seat_index(s) if seat_index(s) is not None else -1))
    others = [s for s in rowers if s != ref]

    ref_catches = np.array([c.time_s for c in catches.get(ref, [])])
    ref_unc = {c.time_s: c.uncertainty_s for c in catches.get(ref, [])}
    if ref_catches.size < 3:
        return CrossResult(reference=ref, rowers=rowers, excluded=excluded, strokes=[])

    # Pre-sort each other sensor's catches for nearest lookup.
    other_t = {s: np.array([c.time_s for c in catches.get(s, [])]) for s in others}
    other_unc = {s: {c.time_s: c.uncertainty_s for c in catches.get(s, [])} for s in others}

    period_s = 1.0 / frames[ref].dominant_hz if frames[ref].dominant_hz > 0 else 2.0

    # Coarse per-seat lag from whole-waveform x-corr — centers the catch search so
    # nearest-match can't alias to an adjacent stroke (the second estimator, §6.3).
    coarse: dict[str, float] = {}
    pl = pairwise_lags(frames, ref, others)
    for s in others:
        lag_ms = pl.get(s, (0.0, None))[1]
        coarse[s] = (lag_ms / 1000.0) if lag_ms is not None else 0.0
    tol = 0.30 * period_s  # tight window around the coarse-aligned expectation

    strokes: list[StrokeSync] = []
    for i in range(1, len(ref_catches)):
        t_ref = float(ref_catches[i])
        stroke_period = t_ref - float(ref_catches[i - 1])
        spm = 60.0 / stroke_period if stroke_period > 0 else 0.0

        offsets = {ref: 0.0}
        uncs = {ref: 1000.0 * ref_unc.get(t_ref, 0.0)}
        for s in others:
            ts = other_t[s]
            if ts.size == 0:
                continue
            expected = t_ref + coarse[s]
            j = int(np.argmin(np.abs(ts - expected)))
            if abs(float(ts[j]) - expected) <= tol:
                offsets[s] = float(ts[j] - t_ref) * 1000.0  # signed (+ = later)
                uncs[s] = 1000.0 * other_unc[s].get(float(ts[j]), 0.0)

        vals = list(offsets.values())
        spread = (max(vals) - min(vals)) if len(vals) >= 2 else 0.0
        flags: list[str] = []
        if (f := validity.spm_flag(spm)):
            flags.append(f)
        strokes.append(StrokeSync(
            stroke_time_s=t_ref, spm=spm, offsets_ms=offsets, uncertainty_ms=uncs,
            spread_ms=spread, n_matched=len(offsets), flags=flags,
        ))

    return CrossResult(reference=ref, rowers=rowers, excluded=excluded, strokes=strokes)


def _common_grid(frames: dict[str, SensorFrame], sources: list[str], fs: float):
    """Interpolate the given sensors' signals onto one shared uniform grid."""
    t0 = max(frames[s].times_s[0] for s in sources)
    t1 = min(frames[s].times_s[-1] for s in sources)
    if t1 <= t0:
        return None
    grid = np.arange(t0, t1, 1.0 / fs)
    sigs = {s: np.interp(grid, frames[s].times_s, frames[s].signal) for s in sources}
    return grid, sigs


def align_signs(frames: dict[str, SensorFrame], ref: str, sources: list[str],
                fs: float = 50.0) -> list[str]:
    """Flip any sensor whose synthetic axis is anti-phase to the reference
    (RESEARCH.md §3.3 step 2), so every sensor's up-crossing marks the same
    physical instant. Mutates frames[s].signal. Returns the flipped sources.
    """
    cg = _common_grid(frames, sources, fs)
    if cg is None:
        return []
    _, sigs = cg
    a = sigs[ref] - sigs[ref].mean()
    flipped: list[str] = []
    for s in sources:
        if s == ref:
            continue
        b = sigs[s] - sigs[s].mean()
        if float(np.dot(a, b)) < 0:
            frames[s].signal = -frames[s].signal
            frames[s].sweep = -frames[s].sweep
            flipped.append(s)
    return flipped


def pairwise_lags(frames: dict[str, SensorFrame], ref: str, others: list[str],
                  fs: float = 50.0, max_lag_s: float = 1.0) -> dict[str, tuple[float, float | None]]:
    """Per seat: (zero-lag correlation sign, x-corr lag in ms) vs the reference.

    Correlation sign < 0 means the sensor's synthetic axis is anti-phase and its
    signal must be flipped before catch matching. Lag is the whole-waveform offset
    (independent of catch phase) — the second estimator and the alignment key.
    """
    cg = _common_grid(frames, [ref] + others, fs)
    if cg is None:
        return {}
    _, sigs = cg
    a = sigs[ref] - sigs[ref].mean()
    out: dict[str, tuple[float, float | None]] = {}
    for s in others:
        b = sigs[s] - sigs[s].mean()
        c0 = float(np.dot(a, b))
        lag = gaussian_lag(sigs[ref], sigs[s], fs, max_lag_s)
        out[s] = (c0, (lag * 1000.0) if lag is not None else None)
    return out


def gaussian_lag(sig_ref: np.ndarray, sig_other: np.ndarray, fs: float,
                 max_lag_s: float) -> float | None:
    """Second estimator: sub-sample lag of `sig_other` vs `sig_ref` by cross-
    correlation with GAUSSIAN peak interpolation (RESEARCH.md §6.3).

    Returns lag in seconds (+ = other lags reference), or None.
    """
    a = np.asarray(sig_ref, float)
    b = np.asarray(sig_other, float)
    n = min(a.size, b.size)
    if n < 8 or fs <= 0:
        return None
    a = (a[:n] - a[:n].mean())
    b = (b[:n] - b[:n].mean())
    corr = np.correlate(b, a, mode="full")
    lags = np.arange(-n + 1, n)
    max_lag = int(max_lag_s * fs)
    mask = np.abs(lags) <= max_lag
    corr, lags = corr[mask], lags[mask]
    if corr.size < 3:
        return None
    k = int(np.argmax(corr))
    if k == 0 or k == corr.size - 1 or corr[k] <= 0:
        return float(lags[k]) / fs
    # Gaussian interpolation: fit a parabola to the logs of the 3 points.
    ym1, y0, yp1 = corr[k - 1], corr[k], corr[k + 1]
    if ym1 <= 0 or y0 <= 0 or yp1 <= 0:
        return float(lags[k]) / fs
    lm1, l0, lp1 = np.log(ym1), np.log(y0), np.log(yp1)
    denom = (lm1 - 2 * l0 + lp1)
    delta = 0.5 * (lm1 - lp1) / denom if denom != 0 else 0.0
    return float(lags[k] + delta) / fs
