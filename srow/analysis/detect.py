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
    x_smooth = _moving_average(signal, smooth_win)
    # High-frequency residual removed by smoothing = the true noise floor for the
    # timing-uncertainty estimate (NOT the oscillation's own amplitude).
    resid = signal - x_smooth
    noise = 1.4826 * float(np.median(np.abs(resid - np.median(resid)))) or 1e-6

    # 2) detrend with a running median spanning ~2 strokes (drift immunity).
    med_win = max(int(fs * 2.0 * period_s), 5)
    if med_win % 2 == 0:
        med_win += 1
    x = x_smooth - pd.Series(x_smooth).rolling(med_win, center=True, min_periods=1).median().to_numpy()

    scale = 1.4826 * float(np.median(np.abs(x - np.median(x))))
    scale = scale or float(np.std(x)) or 1e-6
    h = 0.4 * scale                       # hysteresis at ~0.4 sigma
    refractory_s = 0.55 * period_s        # we KNOW the rate — reject double-detections

    catches: list[Catch] = []
    armed = False
    last_t: float | None = None
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
            if last_t is not None and (t - last_t) < refractory_s:
                continue  # within one stroke of the previous catch — skip
            slope_per_s = (b - a) / dt
            unc = noise / abs(slope_per_s) if abs(slope_per_s) > 1e-9 else dt
            amp = float(np.ptp(x[last_i:i])) if i > last_i else 0.0
            catches.append(Catch(time_s=t, uncertainty_s=float(unc), amplitude=amp))
            armed = False
            last_t, last_i = t, i

    return catches
