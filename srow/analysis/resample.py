"""Sample-rate estimation and uniform resampling (RESEARCH.md §6.1).

Phone-arrival timestamps are non-uniform (BLE jitter, gaps). PCA covariance and
median-crossing detection tolerate that, but any spectral / cross-correlation
step assumes uniform spacing — so resample onto a synthesized uniform grid first.
"""

from __future__ import annotations

import numpy as np


def estimate_rate(times_s: np.ndarray) -> float:
    """Effective sample rate (Hz) = 1 / median inter-sample interval.

    Per-sensor and per-interval — never assume a configured rate; BLE load makes
    the effective rate variable (RESEARCH.md §2).
    """
    if times_s is None or len(times_s) < 2:
        return 0.0
    dt = np.diff(np.asarray(times_s, dtype=float))
    dt = dt[dt > 0]
    if dt.size == 0:
        return 0.0
    med = float(np.median(dt))
    return 1.0 / med if med > 0 else 0.0


def to_uniform(
    times_s: np.ndarray,
    values: np.ndarray,
    target_hz: float | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Linear-interpolate a signal onto a uniform time grid.

    Args:
        times_s: monotonic sample times (epoch seconds), non-uniform.
        values: signal aligned to times_s (1-D, or 2-D with rows = samples).
        target_hz: grid rate; defaults to the estimated native rate (no upsample).

    Returns:
        (grid_times_s, resampled_values, fs) — fs is the grid rate actually used.
        Empty arrays + 0.0 if the input is too short.
    """
    times_s = np.asarray(times_s, dtype=float)
    values = np.asarray(values, dtype=float)
    if times_s.size < 2:
        return np.empty(0), np.empty((0,) + values.shape[1:]), 0.0

    fs = target_hz or estimate_rate(times_s)
    if fs <= 0:
        return np.empty(0), np.empty((0,) + values.shape[1:]), 0.0

    t0, t1 = times_s[0], times_s[-1]
    n = int(np.floor((t1 - t0) * fs)) + 1
    if n < 2:
        return np.empty(0), np.empty((0,) + values.shape[1:]), 0.0
    grid = t0 + np.arange(n) / fs

    if values.ndim == 1:
        out = np.interp(grid, times_s, values)
    else:
        out = np.column_stack([np.interp(grid, times_s, values[:, c]) for c in range(values.shape[1])])
    return grid, out, fs
