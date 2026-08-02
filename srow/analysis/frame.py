"""Per-sensor synthetic physical frame (RESEARCH.md §3).

Replaces the old "pick one Euler channel" with a frame CONSTRUCTED from the data,
so it is independent of how the sensor was mounted on the oar:

    down  = gravity direction (low-frequency accelerometer)
    sweep = dominant gyro rotation axis (PCA), spectrally checked to be the
            stroke (not the feather roll), orthogonalized against down
    third = down x sweep

The gyro projected onto `sweep` is a signed 1-D oscillation, one cycle per stroke
— the signal all detection runs on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import validity
from .resample import estimate_rate, to_uniform

_EPS = 1e-9


@dataclass
class SensorFrame:
    times_s: np.ndarray        # native sample times (epoch seconds)
    signal: np.ndarray         # gyro projected onto sweep, z-scored (native grid)
    down: np.ndarray           # gravity unit vector (sensor frame)
    sweep: np.ndarray          # stroke rotation axis (unit, orthogonal to down)
    third: np.ndarray          # down x sweep
    variance_explained: float  # PC1 eigenvalue / sum — how 1-D the rotation is
    dominant_hz: float         # dominant frequency of the sweep signal
    fs: float                  # estimated native rate
    used_feather_fallback: bool  # PC1 was out-of-band; fell back to PC2/PC1


def _dominant_hz(sig: np.ndarray, fs: float) -> tuple[float, float]:
    """(dominant frequency, in-band power fraction) via rFFT on a uniform signal."""
    if sig.size < 8 or fs <= 0:
        return 0.0, 0.0
    x = sig - sig.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), d=1.0 / fs)
    spec[0] = 0.0  # drop DC
    total = spec.sum()
    if total <= 0:
        return 0.0, 0.0
    dom = float(freqs[int(np.argmax(spec))])
    lo, hi = validity.STROKE_BAND_HZ
    inband = float(spec[(freqs >= lo) & (freqs <= hi)].sum() / total)
    return dom, inband


def _project_dom_hz(gyro_centered: np.ndarray, axis: np.ndarray,
                    times_s: np.ndarray) -> tuple[float, float]:
    """Dominant freq + in-band fraction of the gyro projected onto `axis`."""
    proj = gyro_centered @ axis
    _, uproj, fs = to_uniform(times_s, proj)
    return _dominant_hz(uproj, fs)


def build_sensor_frame(times_s: np.ndarray, accel: np.ndarray,
                       gyro: np.ndarray) -> SensorFrame | None:
    """Build the synthetic frame for one sensor over one interval.

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

    # 1) down = gravity direction. Mean accel over the interval ~ gravity (linear
    #    accelerations average out over many strokes). (Causal tier: running LPF.)
    g_mean = np.nanmean(accel, axis=0)
    gn = np.linalg.norm(g_mean)
    down = g_mean / gn if gn > _EPS else np.array([0.0, 0.0, 1.0])

    # 2) gyro PCA -> candidate rotation axes
    G = gyro - np.nanmean(gyro, axis=0)
    G = np.nan_to_num(G)
    cov = (G.T @ G) / max(len(G), 1)
    evals, evecs = np.linalg.eigh(cov)           # ascending
    order = np.argsort(evals)[::-1]
    evals, evecs = evals[order], evecs[:, order]
    if evals[0] <= _EPS:
        return None
    variance_explained = float(evals[0] / (evals.sum() + _EPS))

    # 3) feather rejection: prefer PC1 if its projection is in the stroke band,
    #    else PC2 if it is, else fall back to PC1.
    pc1, pc2 = evecs[:, 0], evecs[:, 1]
    dom1, in1 = _project_dom_hz(G, pc1, times_s)
    lo, hi = validity.STROKE_BAND_HZ
    used_fallback = False
    if lo <= dom1 <= hi:
        sweep = pc1
    else:
        dom2, in2 = _project_dom_hz(G, pc2, times_s)
        if lo <= dom2 <= hi:
            sweep, used_fallback = pc2, True
        else:
            sweep = pc1  # nothing clean in-band; keep the strongest, flag downstream

    # 4) orthonormalize sweep against down; third = down x sweep
    sweep = sweep - (sweep @ down) * down
    sn = np.linalg.norm(sweep)
    if sn <= _EPS:
        return None
    sweep = sweep / sn
    third = np.cross(down, sweep)

    # 5) deterministic sign (largest-magnitude loading positive) — RESEARCH §3.3
    if sweep[int(np.argmax(np.abs(sweep)))] < 0:
        sweep = -sweep

    # 6) project native gyro onto sweep -> signed 1-D signal, then z-score
    signal = G @ sweep
    sd = signal.std()
    signal = (signal - signal.mean()) / sd if sd > _EPS else signal * 0.0

    dom, _ = _project_dom_hz(G, sweep, times_s)

    return SensorFrame(
        times_s=times_s, signal=signal, down=down, sweep=sweep, third=third,
        variance_explained=variance_explained, dominant_hz=dom, fs=fs,
        used_feather_fallback=used_fallback,
    )
