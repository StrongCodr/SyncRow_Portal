"""Per-sensor synthetic physical frame (RESEARCH.md §3).

Replaces "pick one Euler channel" with a frame CONSTRUCTED from the data, so it
is independent of how the sensor was mounted on the oar:

    down  = gravity direction (mean accelerometer; see caveat below)
    sweep = dominant gyro rotation axis (PCA), spectrally checked to be the
            stroke (not the feather roll), orthogonalized against down
    third = down x sweep

The gyro projected onto `sweep` is a signed 1-D oscillation, one cycle per stroke.

Caveats (honest): `down` is the interval-mean accel — valid only if linear
accelerations average out; under sustained turns it drifts. `down` is used only
to orthogonalize `sweep`, which PCA already dominates, so its influence is minor.
PCA assumes a single constant rotation axis over the interval; residual off-axis
energy (1 - variance_explained) is what makes per-stroke catch phase wander.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import validity
from .config import DEFAULT, AnalysisConfig
from .resample import estimate_rate

_EPS = 1e-9
_PERIOD_HZ = 25.0        # coarse uniform grid for the autocorrelation (phone PERIOD_HZ)
_AUTOCORR_MIN = 0.30     # min normalized autocorrelation to accept a period (phone AUTOCORR_MIN)


@dataclass
class SensorFrame:
    times_s: np.ndarray        # native sample times (epoch seconds)
    signal: np.ndarray         # gyro projected onto sweep, z-scored (native grid)
    down: np.ndarray           # gravity unit vector (sensor frame)
    sweep: np.ndarray          # stroke rotation axis (unit, orthogonal to down)
    third: np.ndarray          # down x sweep
    variance_explained: float  # PC1 eigenvalue / sum — how 1-D the rotation is
    dominant_hz: float         # dominant frequency of the sweep signal
    sweep_energy: float        # std of the projected sweep signal (deg/s, pre-z-score)
    fs: float                  # estimated native rate
    used_feather_fallback: bool  # PC1 was out-of-band; used PC2 instead
    stroke_axis_ok: bool       # a stroke-band axis was actually found (else PC1 kept blindly)
    held: np.ndarray           # bool per sample: gyro row identical to previous (zero-order-hold)


def _autocorr_hz(proj: np.ndarray, times_s: np.ndarray) -> float:
    """Fundamental stroke frequency of a projected signal by AUTOCORRELATION.

    SHARED SPEC — a numpy port of the phone's StrokeAnalyzer.periodOfAxis (same
    PERIOD_HZ grid, stroke band, AUTOCORR_MIN). Resample to a coarse uniform grid,
    then return the frequency of the strongest in-band autocorrelation peak. Robust
    to harmonics: a spiky stroke repeats once per stroke regardless of how much
    energy sits on the 2x/3x harmonic, so autocorrelation locks the fundamental —
    where FFT-argmax would return whichever harmonic bin happens to be tallest.
    Returns 0.0 if there is no clear in-band periodicity (the seat is then gated out
    by is_rowing, exactly as on the phone).

    DIVERGENCE (documented): the phone falls back to the median inter-catch interval
    when autocorrelation can't lock; this batch tier computes dominant_hz BEFORE catch
    detection, so it has no catches to fall back on and simply reports 0.0. Over a full
    trailing window real strokes lock reliably, so the fallback is rarely material.
    """
    times_s = np.asarray(times_s, dtype=float)
    proj = np.asarray(proj, dtype=float)
    if times_s.size < 16:
        return 0.0
    t0, t1 = float(times_s[0]), float(times_s[-1])
    if t1 - t0 < 3.0:                        # need a few seconds (phone: 3000 ms)
        return 0.0
    m = int((t1 - t0) * _PERIOD_HZ)
    if m < 32:
        return 0.0
    grid = t0 + np.arange(m) / _PERIOD_HZ
    x = np.interp(grid, times_s, proj)
    x = x - x.mean()
    zero = float(x @ x)
    if zero < _EPS:
        return 0.0
    lo, hi = validity.STROKE_BAND_HZ
    min_lag = int(_PERIOD_HZ / hi)
    max_lag = min(int(_PERIOD_HZ / lo), m - 1)
    best_lag, best = -1, 0.0
    for lag in range(min_lag, max_lag + 1):
        cc = float(x[: m - lag] @ x[lag:]) / zero
        if cc > best:
            best, best_lag = cc, lag
    if best_lag < 0 or best < _AUTOCORR_MIN:
        return 0.0
    return _PERIOD_HZ / best_lag


def _project_dom_hz(gyro_centered: np.ndarray, axis: np.ndarray,
                    times_s: np.ndarray) -> float:
    return _autocorr_hz(gyro_centered @ axis, times_s)


@dataclass
class _Axis:
    down: np.ndarray
    sweep: np.ndarray
    third: np.ndarray
    variance_explained: float
    used_fallback: bool
    stroke_axis_ok: bool
    dominant_hz: float          # autocorr period of the chosen axis OVER THIS WINDOW


def _axis(accel_w: np.ndarray, gyro_w: np.ndarray, times_w: np.ndarray,
          prev_sweep: np.ndarray | None = None) -> _Axis | None:
    """Synthetic frame axis over ONE window (RESEARCH.md §3): down = gravity (mean
    accel); sweep = dominant gyro rotation axis (PC1, feather-checked against PC2),
    orthogonalised against down, deterministic sign; third = down x sweep. Returns
    None if the rotation is degenerate. This is the phone's recomputeAxisIfDue body.
    """
    g_mean = np.nanmean(accel_w, axis=0)
    gn = np.linalg.norm(g_mean)
    down = g_mean / gn if gn > _EPS else np.array([0.0, 0.0, 1.0])

    G = np.nan_to_num(gyro_w - np.nanmean(gyro_w, axis=0))
    cov = (G.T @ G) / max(len(G), 1)
    evals, evecs = np.linalg.eigh(cov)               # ascending
    order = np.argsort(evals)[::-1]
    evals, evecs = evals[order], evecs[:, order]
    if evals[0] <= _EPS:
        return None
    variance_explained = float(evals[0] / (evals.sum() + _EPS))

    # Feather choice, with HYSTERESIS (mirrors the phone's recomputeAxisIfDue): once an
    # axis is locked, keep following whichever PC *continues* it (the one most aligned
    # with prev_sweep) as long as that axis stays in the stroke band — only re-decide
    # fresh when the continuation genuinely leaves the band (a real re-orientation / the
    # rower stopped). Without this, a window where PC1 is marginally out-of-band flips to
    # PC2 (a ~90° jump the sign-align can't fix), corrupting the projected signal on
    # ambiguous seats. Fresh decision: prefer PC1 if in-band, else PC2, else keep PC1.
    pc1, pc2 = evecs[:, 0], evecs[:, 1]
    used_fallback = False
    stroke_axis_ok = True
    sweep = None
    dom = 0.0
    if prev_sweep is not None:
        if abs(float(pc1 @ prev_sweep)) >= abs(float(pc2 @ prev_sweep)):
            cont, cont_fallback = pc1, False
        else:
            cont, cont_fallback = pc2, True
        dcont = _project_dom_hz(G, cont, times_w)
        if validity.in_stroke_band(dcont):
            sweep, dom, used_fallback = cont, dcont, cont_fallback
    if sweep is None:
        d1 = _project_dom_hz(G, pc1, times_w)
        if validity.in_stroke_band(d1):
            sweep, dom = pc1, d1
        else:
            d2 = _project_dom_hz(G, pc2, times_w)
            if validity.in_stroke_band(d2):
                sweep, dom, used_fallback = pc2, d2, True
            else:
                sweep, dom, stroke_axis_ok = pc1, d1, False  # no clean stroke axis (cox/idle)

    sweep = sweep - (sweep @ down) * down
    sn = np.linalg.norm(sweep)
    if sn <= _EPS:
        return None
    sweep = sweep / sn
    third = np.cross(down, sweep)

    if sweep[int(np.argmax(np.abs(sweep)))] < 0:     # deterministic sign (§3.3)
        sweep = -sweep
    return _Axis(down, sweep, third, variance_explained, used_fallback, stroke_axis_ok, dom)


def build_sensor_frame(times_s: np.ndarray, accel: np.ndarray, gyro: np.ndarray,
                       cfg: AnalysisConfig = DEFAULT) -> SensorFrame | None:
    """Build the synthetic frame for one sensor over one interval.

    ADAPTIVE AXIS (mirrors the phone's StrokeAnalyzer, which recomputes the axis over
    a trailing WINDOW_MS buffer every AXIS_REFRESH_MS): rather than one whole-interval
    PCA — which blends two mountings if the sensor is bumped mid-piece — the axis is
    recomputed over a TRAILING `axis_window_s` window at `axis_refresh_s` anchors, and
    each sample is projected with the axis of the anchor covering it. Consecutive
    anchors are sign-aligned (dot > 0) so the projected signal never flips mid-stream.
    A mid-session re-orientation therefore re-locks the axis on BOTH tiers.

    DIVERGENCE from the phone (documented): the phone is causal and warms up on a
    growing partial buffer; this batch tier uses the first FULL trailing window for
    the interval's head (forward-looking), and if the interval is shorter than one
    window it is a single whole-interval axis (identical to the pre-adaptive code).

    Args:
        times_s: (N,) epoch seconds, monotonic.
        accel:   (N,3) accelerometer (g).
        gyro:    (N,3) angular velocity (deg/s).
    Returns None if the data is too short or the rotation is degenerate.
    """
    times_s = np.asarray(times_s, dtype=float)
    accel = np.asarray(accel, dtype=float)
    gyro = np.asarray(gyro, dtype=float)
    if times_s.size < 16 or gyro.shape[0] != times_s.size:
        return None

    fs = estimate_rate(times_s)

    # zero-order-hold detection: a sample whose raw gyro row is bit-identical to
    # the previous one is a held/duplicated packet, not fresh motion (see
    # sensor-held-samples: bow units repeat values for up to ~1.3s). Detected on
    # RAW gyro, before any processing, so downstream can exclude degraded windows.
    held = np.zeros(times_s.size, dtype=bool)
    if gyro.shape[0] > 1:
        held[1:] = np.all(gyro[1:] == gyro[:-1], axis=1)

    global_mean = np.nanmean(gyro, axis=0)
    span = float(times_s[-1] - times_s[0])
    win = cfg.axis_window_s

    if span <= win:
        # Short interval: the trailing window IS the whole interval -> single axis
        # (byte-identical to the pre-adaptive engine).
        ax = _axis(accel, gyro, times_s)
        if ax is None:
            return None
        proj = (gyro - global_mean) @ ax.sweep
        rep = ax
        dominant_hz = ax.dominant_hz
        var_rep, fb_rep, ok_rep = ax.variance_explained, ax.used_fallback, ax.stroke_axis_ok
    else:
        # Adaptive: recompute the axis over a trailing `win` window at each anchor
        # (with feather hysteresis vs the previous axis), sign-align to the previous,
        # then project each sample with the axis AND the gyro mean of its anchor window
        # (window-centred projection, matching the phone — not one global mean).
        anchors = np.arange(times_s[0] + win, span + times_s[0] + cfg.axis_refresh_s,
                            cfg.axis_refresh_s)
        atimes: list[float] = []
        sweeps: list[np.ndarray] = []
        means: list[np.ndarray] = []          # gyro mean of each anchor's window
        doms: list[float] = []                # per-window in-band autocorr frequencies (fresh only)
        var_list: list[float] = []
        any_fb = any_ok = False
        rep = None
        prev = None
        for ta in anchors:
            i0 = int(np.searchsorted(times_s, ta - win))
            i1 = int(np.searchsorted(times_s, ta))
            fresh = i1 - i0 >= 16
            ax = _axis(accel[i0:i1], gyro[i0:i1], times_s[i0:i1], prev) if fresh else None
            if ax is None:
                if rep is None:
                    continue                  # no axis has locked yet
                ax, fresh = rep, False        # carry the last good axis over a degenerate window
            sweep = ax.sweep
            if prev is not None and float(sweep @ prev) < 0:
                sweep = -sweep                # keep the projected signal continuous
            prev = sweep
            atimes.append(float(ta))
            sweeps.append(sweep)
            means.append(gyro[i0:i1].mean(axis=0) if i1 > i0 else global_mean)
            if fresh:                         # only fresh windows inform the aggregates
                var_list.append(ax.variance_explained)
                any_fb = any_fb or ax.used_fallback
                any_ok = any_ok or ax.stroke_axis_ok
                if ax.stroke_axis_ok:
                    doms.append(ax.dominant_hz)
            rep = ax                          # representative = latest good axis
        if rep is None:
            return None
        # per-sample axis/mean = the most recent anchor at/before the sample (the first
        # anchor for the head, before any window is full).
        atimes_a = np.asarray(atimes)
        sweeps_a = np.asarray(sweeps)                       # (K, 3)
        means_a = np.asarray(means)                         # (K, 3)
        idx = np.clip(np.searchsorted(atimes_a, times_s, side="right") - 1,
                      0, len(atimes) - 1)
        proj = np.einsum("ij,ij->i", gyro - means_a[idx], sweeps_a[idx])
        # dominant_hz = median of the per-window in-band locks (each window is ~stationary,
        # so this is robust where a single whole-interval autocorr smears if the rate
        # drifts). Falls back to the representative axis's own frequency if none locked.
        dominant_hz = float(np.median(doms)) if doms else rep.dominant_hz
        # representative diagnostics span the whole interval (median var / any-window
        # feather-fallback / any-window locked), not just the last anchor's.
        var_rep = float(np.median(var_list)) if var_list else rep.variance_explained
        fb_rep, ok_rep = any_fb, any_ok

    sweep_energy = float(proj.std())                 # amplitude BEFORE z-scoring
    signal = (proj - proj.mean()) / sweep_energy if sweep_energy > _EPS else proj * 0.0

    return SensorFrame(
        times_s=times_s, signal=signal, down=rep.down, sweep=rep.sweep, third=rep.third,
        variance_explained=var_rep,
        dominant_hz=dominant_hz,                     # AUTOCORRELATION (harmonic-robust)
        sweep_energy=sweep_energy, fs=fs,
        used_feather_fallback=fb_rep, stroke_axis_ok=ok_rep,
        held=held,
    )
