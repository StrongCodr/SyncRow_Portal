"""Cross-sensor synchronicity (RESEARCH.md §7).

Reference = the stroke seat (highest seat index). Per stroke, the signed offset
of each other rowing seat vs stroke is estimated by WINDOWED cross-correlation of
the projected sweep signals on a shared grid (robust to per-stroke catch-phase
wander; catch-to-catch matching gives the right average but is noisy per stroke).
Confidence and uncertainty come from the SAME estimator that produces the offset:
the normalized cross-correlation peak rho. A stroke is `low_confidence` when the
two waveforms simply don't match well at any lag (rho < min_corr), and the offset
1-sigma follows from rho and the window length via a CRB-style relation. (Deriving
uncertainty from catch-crossing slope — a different estimator than the x-corr that
sets the offset — was the earlier mistake: it flagged good strokes.) Non-rowing
sensors (cox / no in-band rhythm / too little sweep energy) are excluded. Sensors
whose synthetic axis is anti-phase are sign-flipped first (`align_signs`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from . import validity
from .config import DEFAULT, AnalysisConfig
from .detect import Catch
from .frame import SensorFrame


def seat_index(source: str) -> int | None:
    """Parse a seat number from a source label ('Seat 4 (...)' -> 4). Cox -> None."""
    m = re.search(r"seat\s*(\d+)", source, re.IGNORECASE)
    return int(m.group(1)) if m else None


def is_rowing(frame: SensorFrame) -> bool:
    """Rowing if there is an in-band stroke rhythm AND enough sweep amplitude."""
    return (
        validity.in_stroke_band(frame.dominant_hz)
        and frame.sweep_energy >= validity.MIN_SWEEP_ENERGY_DEG_S
    )


@dataclass
class StrokeSync:
    stroke_time_s: float                 # reference (stroke seat) catch time
    spm: float                           # 60 / interval to previous stroke catch
    offsets_ms: dict[str, float]         # per seat, signed vs stroke (stroke = 0)
    uncertainty_ms: dict[str, float]     # per-seat offset uncertainty (1-sigma, from x-corr)
    corr: dict[str, float]               # per-seat x-corr peak rho (match confidence)
    spread_ms: float                     # max-min across matched seats
    n_matched: int
    flags: list[str] = field(default_factory=list)


@dataclass
class CrossResult:
    reference: str | None
    rowers: list[str]
    excluded: list[str]
    strokes: list[StrokeSync]


def _common_grid(frames: dict[str, SensorFrame], sources: list[str], fs: float):
    """Interpolate the given sensors' signals onto one shared uniform grid."""
    t0 = max(frames[s].times_s[0] for s in sources)
    t1 = min(frames[s].times_s[-1] for s in sources)
    if t1 <= t0:
        return None
    grid = np.arange(t0, t1, 1.0 / fs)
    sigs = {s: np.interp(grid, frames[s].times_s, frames[s].signal) for s in sources}
    return grid, sigs


def _lag_uncertainty_s(rho: float, n: int, f_dom: float) -> float:
    """1-sigma of a cross-correlation time-delay estimate, CRB-style.

    sigma = 1/(2*pi*f_dom) * sqrt((1-rho^2)/rho^2) / sqrt(N)

    Same estimator that yields the offset, so confidence is self-consistent. High
    rho (waveforms match) => small sigma; rho -> 0 => sigma blows up. f_dom sets
    the effective bandwidth (a stroke-band sinusoid); N is the window sample count.
    """
    rho = float(min(abs(rho), 0.999))
    if rho <= 0.0 or n < 2 or f_dom <= 0.0:
        return float("inf")
    return (1.0 / (2.0 * np.pi * f_dom)) * np.sqrt((1.0 - rho * rho) / (rho * rho)) / np.sqrt(n)


def analyze_cross(frames: dict[str, SensorFrame], catches: dict[str, list[Catch]],
                  cfg: AnalysisConfig = DEFAULT) -> CrossResult:
    rowers = [s for s, f in frames.items() if is_rowing(f)]
    excluded = [s for s in frames if s not in rowers]
    if len(rowers) < 2:
        return CrossResult(reference=None, rowers=rowers, excluded=excluded, strokes=[])

    ref = max(rowers, key=lambda s: (seat_index(s) if seat_index(s) is not None else -1))
    others = [s for s in rowers if s != ref]

    ref_catch_list = catches.get(ref, [])
    ref_catches = np.array([c.time_s for c in ref_catch_list])
    ref_amp = {c.time_s: c.amplitude for c in ref_catch_list}
    if ref_catches.size < 3:
        return CrossResult(reference=ref, rowers=rowers, excluded=excluded, strokes=[])
    median_amp = float(np.median([c.amplitude for c in ref_catch_list])) or 1e-9

    f_dom = frames[ref].dominant_hz
    global_period = 1.0 / f_dom if f_dom > 0 else 2.0
    cg = _common_grid(frames, [ref] + others, cfg.xcorr_grid_hz)
    if cg is None:
        return CrossResult(reference=ref, rowers=rowers, excluded=excluded, strokes=[])
    grid, sigs = cg

    strokes: list[StrokeSync] = []
    for i in range(1, len(ref_catches)):
        t_ref = float(ref_catches[i])
        # LOCAL stroke period (clamped) so the window tracks rate changes.
        local_period = float(np.clip(t_ref - float(ref_catches[i - 1]),
                                     0.6 * global_period, 1.6 * global_period))
        spm = 60.0 / (t_ref - float(ref_catches[i - 1])) if t_ref > ref_catches[i - 1] else 0.0
        half = cfg.xcorr_window_frac * local_period
        max_lag = cfg.xcorr_max_lag_frac * local_period

        i0 = int(np.searchsorted(grid, t_ref - half))
        i1 = int(np.searchsorted(grid, t_ref + half))
        if i1 - i0 < 8:
            continue
        ref_win = sigs[ref][i0:i1]

        offsets = {ref: 0.0}
        uncs = {ref: 0.0}
        corr = {ref: 1.0}
        for s in others:
            est = gaussian_lag(ref_win, sigs[s][i0:i1], cfg.xcorr_grid_hz, max_lag)
            if est is None:
                continue
            offsets[s] = est.lag_s * 1000.0  # signed ms (+ = seat later than stroke)
            corr[s] = est.rho
            # uncertainty from the SAME x-corr peak that set the offset
            uncs[s] = 1000.0 * _lag_uncertainty_s(est.rho, est.n, f_dom)

        vals = list(offsets.values())
        spread = (max(vals) - min(vals)) if len(vals) >= 2 else 0.0

        flags: list[str] = []
        if (f := validity.spm_flag(spm)):
            flags.append(f)
        if ref_amp.get(t_ref, median_amp) < cfg.drill_amp_frac * median_amp:
            flags.append("drill")
        seat_rhos = [r for k, r in corr.items() if k != ref]
        if seat_rhos and min(seat_rhos) < cfg.min_corr:
            flags.append("low_confidence")

        strokes.append(StrokeSync(
            stroke_time_s=t_ref, spm=spm, offsets_ms=offsets, uncertainty_ms=uncs,
            corr=corr, spread_ms=spread, n_matched=len(offsets), flags=flags,
        ))

    return CrossResult(reference=ref, rowers=rowers, excluded=excluded, strokes=strokes)


def align_signs(frames: dict[str, SensorFrame], ref: str, sources: list[str],
                cfg: AnalysisConfig = DEFAULT) -> list[str]:
    """Flip any sensor whose synthetic axis is anti-phase to the reference
    (RESEARCH.md §3.3 step 2), so every sensor's up-crossing marks the same
    physical instant. Mutates frames[s].signal / .sweep. Returns flipped sources.
    """
    cg = _common_grid(frames, sources, cfg.align_grid_hz)
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
                  cfg: AnalysisConfig = DEFAULT,
                  max_lag_s: float = 1.0) -> dict[str, tuple[float, float | None]]:
    """Per seat: (zero-lag correlation sign, whole-waveform x-corr lag in ms) vs
    the reference. Correlation sign < 0 => anti-phase. Used for diagnostics and as
    the second (whole-interval) estimator.
    """
    cg = _common_grid(frames, [ref] + others, cfg.align_grid_hz)
    if cg is None:
        return {}
    _, sigs = cg
    a = sigs[ref] - sigs[ref].mean()
    out: dict[str, tuple[float, float | None]] = {}
    for s in others:
        b = sigs[s] - sigs[s].mean()
        c0 = float(np.dot(a, b))
        est = gaussian_lag(sigs[ref], sigs[s], cfg.align_grid_hz, max_lag_s)
        out[s] = (c0, (est.lag_s * 1000.0) if est is not None else None)
    return out


@dataclass
class LagEst:
    lag_s: float   # sub-sample lag; POSITIVE => sig_other lags (later than) sig_ref
    rho: float     # normalized peak correlation (Pearson at best lag), ~[-1, 1]
    n: int         # window length in samples (for uncertainty scaling)


def gaussian_lag(sig_ref: np.ndarray, sig_other: np.ndarray, fs: float,
                 max_lag_s: float) -> LagEst | None:
    """Sub-sample lag of `sig_other` vs `sig_ref` by cross-correlation with
    GAUSSIAN peak interpolation, plus the normalized peak correlation (the match
    confidence). RESEARCH.md §6.3.

    Convention: POSITIVE lag => sig_other lags (is later than) sig_ref. Verified
    by test_gaussian_lag_recovers_known_delay.
    """
    a = np.asarray(sig_ref, float)
    b = np.asarray(sig_other, float)
    n = min(a.size, b.size)
    if n < 8 or fs <= 0:
        return None
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na <= 0 or nb <= 0:
        return None
    corr = np.correlate(b, a, mode="full")
    lags = np.arange(-n + 1, n)
    max_lag = int(max_lag_s * fs)
    mask = np.abs(lags) <= max_lag
    corr, lags = corr[mask], lags[mask]
    if corr.size < 3:
        return None
    k = int(np.argmax(corr))
    rho = float(corr[k] / (na * nb))          # match confidence at the peak lag
    if k == 0 or k == corr.size - 1 or corr[k] <= 0:
        return LagEst(float(lags[k]) / fs, rho, n)
    ym1, y0, yp1 = corr[k - 1], corr[k], corr[k + 1]
    if ym1 <= 0 or y0 <= 0 or yp1 <= 0:
        return LagEst(float(lags[k]) / fs, rho, n)
    lm1, l0, lp1 = np.log(ym1), np.log(y0), np.log(yp1)
    denom = lm1 - 2 * l0 + lp1
    delta = 0.5 * (lm1 - lp1) / denom if denom != 0 else 0.0
    return LagEst(float(lags[k] + delta) / fs, rho, n)
