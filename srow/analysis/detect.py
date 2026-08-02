"""Catch detection on the projected sweep signal (RESEARCH.md §5, §6.2, §6.4).

Upward crossings of a running-median baseline, but hysteresis-gated (Schmitt
trigger) so we get exactly ONE catch per stroke — feather/noise bleed otherwise
produces several zero-crossings per stroke. The reported time is still the
sub-sample-interpolated midpoint crossing (sharpest timing), each with an
uncertainty from local slope and noise.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Catch:
    time_s: float          # interpolated catch time (epoch seconds)
    uncertainty_s: float   # 1-sigma timing uncertainty
    amplitude: float       # peak-to-peak of the following stroke (for drill flag)


def _moving_average(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1:
        return x
    return pd.Series(x).rolling(win, center=True, min_periods=1).mean().to_numpy()


def detect_catches(times_s: np.ndarray, signal: np.ndarray,
                   fs: float, dominant_hz: float = 0.0) -> list[Catch]:
    """Detect catches as hysteresis-gated upward crossings of `signal`.

    Args:
        times_s: (N,) epoch seconds.
        signal:  (N,) projected sweep signal (z-scored).
        fs:      native sample rate (Hz).
        dominant_hz: stroke frequency (Hz); sizes the smoothing/detrend windows.
    """
    times_s = np.asarray(times_s, dtype=float)
    signal = np.asarray(signal, dtype=float)
    n = signal.size
    if n < 8 or fs <= 0 or dominant_hz <= 0:
        return []

    period_s = 1.0 / dominant_hz
    # 1) smooth to the stroke band (~1/6 period) to kill feather/noise wiggles.
    smooth_win = max(int(fs * period_s / 6.0), 1)
    x = _moving_average(signal, smooth_win)
    # 2) detrend with a running median spanning ~2 strokes (drift immunity).
    med_win = max(int(fs * 2.0 * period_s), 5)
    if med_win % 2 == 0:
        med_win += 1
    x = x - pd.Series(x).rolling(med_win, center=True, min_periods=1).median().to_numpy()

    # Robust amplitude scale; hysteresis at ~0.4 sigma.
    scale = 1.4826 * float(np.median(np.abs(x - np.median(x))))
    scale = scale or float(np.std(x)) or 1e-6
    h = 0.4 * scale
    noise = scale

    catches: list[Catch] = []
    armed = False               # only accept an up-crossing after a dip below -h
    last_zero: float | None = None
    last_i = 0
    for i in range(1, n):
        a, b = x[i - 1], x[i]
        if np.isnan(a) or np.isnan(b):
            continue
        if b < -h:
            armed = True
        if armed and a < 0.0 <= b:
            dt = times_s[i] - times_s[i - 1]
            if dt <= 0:
                continue
            frac = -a / (b - a) if (b - a) != 0 else 0.0
            t = times_s[i - 1] + frac * dt
            slope_per_s = (b - a) / dt
            unc = noise / abs(slope_per_s) if abs(slope_per_s) > 1e-9 else dt
            amp = float(np.ptp(x[last_i:i])) if i > last_i else 0.0
            catches.append(Catch(time_s=t, uncertainty_s=float(unc), amplitude=amp))
            armed = False
            last_zero, last_i = t, i

    return catches
