"""Catch detection on the projected sweep signal (RESEARCH.md §5, §6.2, §6.4).

Upward crossings of a RUNNING median (not a global one — baseline drifts as the
boat turns/settles), interpolated for sub-sample timing, each with a timing
uncertainty derived from local slope and noise.
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


def detect_catches(times_s: np.ndarray, signal: np.ndarray,
                   fs: float, dominant_hz: float = 0.0) -> list[Catch]:
    """Detect catches as upward running-median crossings of `signal`.

    Args:
        times_s: (N,) epoch seconds.
        signal:  (N,) projected sweep signal (z-scored).
        fs:      native sample rate (Hz).
        dominant_hz: stroke frequency, if known, to size the detrend window.
    """
    times_s = np.asarray(times_s, dtype=float)
    signal = np.asarray(signal, dtype=float)
    n = signal.size
    if n < 8 or fs <= 0:
        return []

    # Detrend with a running median spanning ~2 strokes so baseline drift doesn't
    # shift or drop crossings.
    period_s = (1.0 / dominant_hz) if dominant_hz > 0 else 2.0
    win = max(int(fs * 2.0 * period_s), 5)
    if win % 2 == 0:
        win += 1
    baseline = pd.Series(signal).rolling(win, center=True, min_periods=1).median().to_numpy()
    x = signal - baseline

    # Local noise scale (MAD) for the uncertainty estimate.
    noise = 1.4826 * float(np.median(np.abs(x - np.median(x)))) or float(np.std(x)) or 1e-6

    raw: list[tuple[float, float]] = []  # (time, uncertainty)
    for i in range(1, n):
        a, b = x[i - 1], x[i]
        if np.isnan(a) or np.isnan(b):
            continue
        if a < 0.0 <= b:  # upward crossing through the (detrended) midpoint
            dt = times_s[i] - times_s[i - 1]
            if dt <= 0:
                continue
            frac = -a / (b - a) if (b - a) != 0 else 0.0
            t = times_s[i - 1] + frac * dt
            slope_per_s = (b - a) / dt              # signal units / s
            unc = noise / abs(slope_per_s) if abs(slope_per_s) > 1e-9 else dt
            raw.append((t, float(unc)))

    if len(raw) < 2:
        return [Catch(t, u, 0.0) for t, u in raw]

    # Debounce: drop crossings closer than half the median stroke gap.
    ts = np.array([t for t, _ in raw])
    min_gap = 0.5 * float(np.median(np.diff(ts)))
    kept: list[tuple[float, float]] = [raw[0]]
    for t, u in raw[1:]:
        if t - kept[-1][0] >= min_gap:
            kept.append((t, u))

    # Per-stroke amplitude (peak-to-peak of the detrended signal between catches)
    # for the drill flag downstream.
    catches: list[Catch] = []
    for k, (t, u) in enumerate(kept):
        t_next = kept[k + 1][0] if k + 1 < len(kept) else times_s[-1]
        seg = x[(times_s >= t) & (times_s < t_next)]
        amp = float(np.ptp(seg)) if seg.size else 0.0
        catches.append(Catch(time_s=t, uncertainty_s=u, amplitude=amp))
    return catches
