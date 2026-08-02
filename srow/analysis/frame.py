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
    sweep_energy: float        # std of the projected sweep signal (deg/s, pre-z-score)
    fs: float                  # estimated native rate
    used_feather_fallback: bool  # PC1 was out-of-band; used PC2 instead
    stroke_axis_ok: bool       # a stroke-band axis was actually found (else PC1 kept blindly)


def _dominant_hz(sig: np.ndarray, fs: float) -> float:
    if sig.size < 8 or fs <= 0:
        return 0.0
    x = sig - sig.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), d=1.0 / fs)
    if spec.size:
        spec[0] = 0.0  # drop DC
    return float(freqs[int(np.argmax(spec))]) if spec.sum() > 0 else 0.0


def _project_dom_hz(gyro_centered: np.ndarray, axis: np.ndarray,
                    times_s: np.ndarray) -> float:
    _, uproj, fs = to_uniform(times_s, gyro_centered @ axis)
    return _dominant_hz(uproj, fs)


def build_sensor_frame(times_s: np.ndarray, accel: np.ndarray, gyro: np.ndarray,
                       cfg: AnalysisConfig = DEFAULT) -> SensorFrame | None:
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

    g_mean = np.nanmean(accel, axis=0)
    gn = np.linalg.norm(g_mean)
    down = g_mean / gn if gn > _EPS else np.array([0.0, 0.0, 1.0])

    G = np.nan_to_num(gyro - np.nanmean(gyro, axis=0))
    cov = (G.T @ G) / max(len(G), 1)
    evals, evecs = np.linalg.eigh(cov)               # ascending
    order = np.argsort(evals)[::-1]
    evals, evecs = evals[order], evecs[:, order]
    if evals[0] <= _EPS:
        return None
    variance_explained = float(evals[0] / (evals.sum() + _EPS))

    # Feather rejection: prefer PC1 if in the stroke band, else PC2 if it is.
    pc1, pc2 = evecs[:, 0], evecs[:, 1]
    used_fallback = False
    stroke_axis_ok = True
    if validity.in_stroke_band(_project_dom_hz(G, pc1, times_s)):
        sweep = pc1
    elif validity.in_stroke_band(_project_dom_hz(G, pc2, times_s)):
        sweep, used_fallback = pc2, True
    else:
        sweep, stroke_axis_ok = pc1, False  # no clean stroke axis (e.g. cox/idle)

    sweep = sweep - (sweep @ down) * down
    sn = np.linalg.norm(sweep)
    if sn <= _EPS:
        return None
    sweep = sweep / sn
    third = np.cross(down, sweep)

    if sweep[int(np.argmax(np.abs(sweep)))] < 0:     # deterministic sign (§3.3)
        sweep = -sweep

    proj = G @ sweep
    sweep_energy = float(proj.std())                 # amplitude BEFORE z-scoring
    signal = (proj - proj.mean()) / sweep_energy if sweep_energy > _EPS else proj * 0.0

    return SensorFrame(
        times_s=times_s, signal=signal, down=down, sweep=sweep, third=third,
        variance_explained=variance_explained,
        dominant_hz=_project_dom_hz(G, sweep, times_s),
        sweep_energy=sweep_energy, fs=fs,
        used_feather_fallback=used_fallback, stroke_axis_ok=stroke_axis_ok,
    )
