"""Per-sensor synthetic physical frame (RESEARCH.md §3).

Replaces "pick one Euler channel" with a frame CONSTRUCTED from the data, so it
is independent of how the sensor was mounted on the oar:

    flip  = feather-flip axis: PC1 of the gyro content above `flip_hp_hz`, accepted when it
            carries >= `flip_min_deg_s` of fast rotation (sculling: 150-400 deg/s). Removed
            from the gyro before anything else; a plain PCA locks onto it otherwise.
    sweep = dominant remaining rotation axis: PC1 of the flip-free gyro low-passed at
            `axis_lp_hz`, per trailing window. NOT orthogonalised against gravity: an oar
            swings about the pin, a near-vertical axis, so on a scull the sweep axis lies
            within ~10-25 deg of `down`.
    down  = gravity direction (mean accelerometer); only used to complete the frame.
    third = down x sweep (any perpendicular if the two are parallel).

The gyro projected onto `sweep`, held rows interpolated, low-passed at `signal_lp_hz`, is a
signed 1-D oscillation, one cycle per stroke. Validated 10 Oct 2026 against the offline
signal-analysis pipeline: correlation 0.99-1.00 on every unit of three sessions; the two
units on one oar (3 Oct) agree to an IQR of 20 ms on clean strokes.

Caveats: `down` is the interval-mean accel and drifts under sustained turns (it only
affects `third`). The per-window PCA assumes a constant axis within `axis_window_s`;
residual off-axis energy (1 - variance_explained) is what makes per-stroke catch phase wander.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, filtfilt

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


def _uniform(times_s: np.ndarray, x: np.ndarray, grid_hz: float):
    """Resample irregular samples onto a uniform grid (linear). Returns (grid_t, x_u) or None if too short."""
    if times_s.size < 8 or times_s[-1] - times_s[0] < 1.0:
        return None
    grid = np.arange(times_s[0], times_s[-1], 1.0 / grid_hz)
    x = np.atleast_2d(x.T).T if x.ndim == 1 else x
    xu = np.column_stack([np.interp(grid, times_s, np.nan_to_num(x[:, c])) for c in range(x.shape[1])])
    return grid, xu


def _lowpass(x: np.ndarray, fc_hz: float, grid_hz: float) -> np.ndarray:
    """Zero-phase 2nd-order Butterworth low-pass along axis 0 (x on a uniform grid)."""
    if fc_hz <= 0 or fc_hz >= grid_hz / 2 or x.shape[0] < 16:
        return x
    b, a = butter(2, fc_hz / (grid_hz / 2))
    return filtfilt(b, a, x, axis=0)


def _flip_axis(times_s: np.ndarray, gyro_centered: np.ndarray, cfg: AnalysisConfig):
    """Feather-flip axis of one sensor over the WHOLE interval (fresh rows), or None.
    PC1 of the gyro content above `flip_hp_hz` on a uniform grid. It is accepted as the feather
    axis when the high-frequency rate along it is large in absolute terms (p99 of |rate| at least
    `flip_min_deg_s`): feathering puts 150-400 deg/s of fast content on the shaft axis, while a
    hull unit, or an oar that is not feathered, has under 10 deg/s there.
    Returns (axis, p99_deg_s) with axis None when no such axis exists."""
    if cfg.flip_hp_hz <= 0:
        return None, 0.0
    u = _uniform(times_s, gyro_centered, cfg.filter_grid_hz)
    if u is None:
        return None, 0.0
    _, gu = u
    lo = _lowpass(gu, cfg.flip_hp_hz, cfg.filter_grid_hz)
    hi = gu - lo
    ch = (hi.T @ hi) / max(len(hi), 1)
    ev, V = np.linalg.eigh(ch)
    fa = V[:, int(np.argmax(ev))]
    if ev.max() <= _EPS:
        return None, 0.0
    p99 = float(np.percentile(np.abs(hi @ fa), 99))
    return (fa if p99 >= cfg.flip_min_deg_s else None), p99


def _project_dom_hz(gyro_centered: np.ndarray, axis: np.ndarray,
                    times_s: np.ndarray) -> float:
    return _autocorr_hz(gyro_centered @ axis, times_s)


def _strip(g: np.ndarray, axis: np.ndarray | None) -> np.ndarray:
    """Remove the component of g along `axis` (no-op if None)."""
    return g if axis is None else g - np.outer(g @ axis, axis)


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
          prev_sweep: np.ndarray | None = None, cfg: AnalysisConfig = DEFAULT,
          flip_axis: np.ndarray | None = None) -> _Axis | None:
    """Synthetic frame axis over ONE window (RESEARCH.md §3): down = gravity (mean
    accel); sweep = dominant gyro rotation axis (PC1, feather-checked against PC2),
    orthogonalised against down, deterministic sign; third = down x sweep. Returns
    None if the rotation is degenerate. This is the phone's recomputeAxisIfDue body.
    """
    g_mean = np.nanmean(accel_w, axis=0)
    gn = np.linalg.norm(g_mean)
    down = g_mean / gn if gn > _EPS else np.array([0.0, 0.0, 1.0])

    G = _strip(np.nan_to_num(gyro_w - np.nanmean(gyro_w, axis=0)), flip_axis)
    # Sweep PCA on the stroke-band content of the flip-free gyro (the stroke band is < 1 Hz;
    # everything above it is catch shock and noise, which otherwise bias the covariance).
    u = _uniform(times_w, G, cfg.filter_grid_hz) if cfg.axis_lp_hz > 0 else None
    if u is not None:
        gl = _lowpass(u[1], cfg.axis_lp_hz, cfg.filter_grid_hz)
        gl = gl - gl.mean(axis=0)
        cov = (gl.T @ gl) / max(len(gl), 1)
    else:
        cov = (G.T @ G) / max(len(G), 1)
    evals, evecs = np.linalg.eigh(cov)               # ascending
    order = np.argsort(evals)[::-1]
    evals, evecs = evals[order], evecs[:, order]
    if evals[0] <= _EPS:
        return None
    variance_explained = float(evals[0] / (evals.sum() + _EPS))

    # With the feather axis removed explicitly (see _flip_axis), PC1 of the stroke-band
    # covariance IS the sweep axis. The old "PC1 out of band -> use PC2" fallback is gone: on an
    # oar every axis is periodic at the stroke rate, so that test could not tell sweep from
    # wobble and produced 90-degree axis jumps between windows. The band test now only flags
    # whether a stroke rhythm exists at all (cox / idle).
    sweep = evecs[:, 0]
    if prev_sweep is not None and float(sweep @ prev_sweep) < 0:
        sweep = -sweep
    dom = _project_dom_hz(G, sweep, times_w)
    stroke_axis_ok = bool(validity.in_stroke_band(dom))
    used_fallback = False

    # NOTE: the sweep axis is NOT orthogonalised against gravity. An oar swings about the pin,
    # a near-vertical axis, so on a scull the sweep axis is close to `down` itself and projecting
    # it out destroyed the signal (the earlier code did this; the residual was wobble).
    # `third` completes a right-handed frame from whatever part of `down` is perpendicular.
    sn = np.linalg.norm(sweep)
    if sn <= _EPS:
        return None
    sweep = sweep / sn
    third = np.cross(down, sweep)
    tn = np.linalg.norm(third)
    if tn <= _EPS:                                   # sweep parallel to gravity: any perpendicular
        third = np.cross(np.array([1.0, 0.0, 0.0]) if abs(sweep[0]) < 0.9 else np.array([0.0, 1.0, 0.0]), sweep)
        tn = np.linalg.norm(third)
    third = third / tn

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

    fresh = ~held
    if fresh.sum() < 16:
        return None
    global_mean = np.nanmean(gyro[fresh], axis=0)
    # Feather-flip axis, decided once over the whole interval on fresh rows (stable; a per-window
    # decision flickers and a flickering axis is worse than none).
    flip_axis, flip_ratio = _flip_axis(times_s[fresh], np.nan_to_num(gyro[fresh] - global_mean), cfg)
    span = float(times_s[-1] - times_s[0])
    win = cfg.axis_window_s

    if span <= win:
        # Short interval: the trailing window IS the whole interval -> single axis
        # (byte-identical to the pre-adaptive engine).
        ax = _axis(accel[fresh], gyro[fresh], times_s[fresh], cfg=cfg, flip_axis=flip_axis)
        if ax is None:
            return None
        proj = _strip(gyro - global_mean, flip_axis) @ ax.sweep
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
            sel = np.where(fresh[i0:i1])[0] + i0          # fresh rows of this window only
            is_fresh = sel.size >= 16
            ax = _axis(accel[sel], gyro[sel], times_s[sel], prev, cfg, flip_axis) if is_fresh else None
            if ax is None:
                if rep is None:
                    continue                  # no axis has locked yet
                ax, is_fresh = rep, False     # carry the last good axis over a degenerate window
            sweep = ax.sweep
            if prev is not None and float(sweep @ prev) < 0:
                sweep = -sweep                # keep the projected signal continuous
            prev = sweep
            atimes.append(float(ta))
            sweeps.append(sweep)
            means.append(gyro[sel].mean(axis=0) if sel.size else global_mean)
            if is_fresh:                      # only fresh windows inform the aggregates
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
        proj = np.einsum("ij,ij->i", _strip(gyro - means_a[idx], flip_axis), sweeps_a[idx])
        # dominant_hz = median of the per-window in-band locks (each window is ~stationary,
        # so this is robust where a single whole-interval autocorr smears if the rate
        # drifts). Falls back to the representative axis's own frequency if none locked.
        dominant_hz = float(np.median(doms)) if doms else rep.dominant_hz
        # representative diagnostics span the whole interval (median var / any-window
        # feather-fallback / any-window locked), not just the last anchor's.
        var_rep = float(np.median(var_list)) if var_list else rep.variance_explained
        fb_rep, ok_rep = any_fb, any_ok

    # Held (zero-order-hold) rows carry no new motion: replace their projection by the linear
    # interpolation between fresh neighbours so the signal is not a staircase.
    if held.any():
        proj[held] = np.interp(times_s[held], times_s[fresh], proj[fresh])
    # Low-pass the sweep signal (zero-phase, on a uniform grid, back to native times): the
    # catch is read from the stroke-band shape, not from flip or shock spikes.
    if cfg.signal_lp_hz > 0:
        u = _uniform(times_s, proj, cfg.filter_grid_hz)
        if u is not None:
            grid, pu = u
            proj = np.interp(times_s, grid, _lowpass(pu, cfg.signal_lp_hz, cfg.filter_grid_hz)[:, 0])

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
