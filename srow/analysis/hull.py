"""Hull surge: the boat's fore-aft acceleration from a hull-mounted unit.

The unit's mounting is unknown, so the forward axis is found from the data: the principal axis of
the accelerometer in the stroke band (0.2 to 1.5 Hz), where the boat's surge dominates. Its sign is
set so that the drive peak is positive: with the catches known, each cycle's peak plus trough must be
positive (the drive push is the largest excursion, the catch check the smaller, opposite one); without
catches, the skew of the signal decides (a short strong push and a long weak pull are right-skewed). The projected
signal is low-passed at 4 Hz. Same processing as the essay's Figure 11 and the per-stroke Δv.
"""
import numpy as np
from scipy.signal import butter, filtfilt

G0 = 9.80665


def _lp(x: np.ndarray, fc_hz: float, fs: float, order: int = 2) -> np.ndarray:
    b, a = butter(order, fc_hz / (fs / 2.0)); return filtfilt(b, a, x, axis=0)


def _bp(x: np.ndarray, lo_hz: float, hi_hz: float, fs: float) -> np.ndarray:
    b, a = butter(2, [lo_hz / (fs / 2.0), hi_hz / (fs / 2.0)], "band"); return filtfilt(b, a, x, axis=0)


def hull_surge(times_s, accel_g, catches_s=None, fs_out: float = 25.0, lp_hz: float = 4.0):
    """(grid_s, surge_mps2): the hull's fore-aft acceleration on a uniform grid, drive positive.

    times_s: (N,) epoch seconds; accel_g: (N,3) accelerometer in g (held rows, identical to the
    previous row, are dropped before interpolation); catches_s: catch times for the sign test.
    Returns (None, None) if there is too little data for a stroke-band axis.
    """
    t = np.asarray(times_s, float); a = np.asarray(accel_g, float)
    if t.size < 64 or a.shape[0] != t.size:
        return None, None
    fresh = np.r_[True, np.any(a[1:] != a[:-1], axis=1)]
    t, a = t[fresh], a[fresh]
    if t.size < 64 or t[-1] - t[0] < 20.0:
        return None, None
    grid = np.arange(t[0], t[-1], 1.0 / fs_out)
    u = np.column_stack([np.interp(grid, t, a[:, i]) for i in range(3)]) * G0
    u = u - u.mean(axis=0)
    band = _bp(u, 0.2, 1.5, fs_out)
    w, v = np.linalg.eigh(np.cov(band.T))
    axis = v[:, int(np.argmax(w))]
    s = _lp(u @ axis, lp_hz, fs_out)
    sign = 0.0
    if catches_s is not None and len(catches_s) >= 3:
        # per cycle the drive push is the biggest excursion (about +3.5 m/s² on a double), the catch
        # check the smaller one (about −1.5): peak plus trough is positive when the sign is right
        c = np.sort(np.asarray(catches_s, float)); pt = []
        for a0, a1 in zip(c[:-1], c[1:]):
            if 1.0 < a1 - a0 < 5.0:
                m = (grid >= a0) & (grid < a1)
                if m.any():
                    pt.append(s[m].max() + s[m].min())
        if pt:
            sign = float(np.median(pt))
    if sign == 0.0 or not np.isfinite(sign):
        d = s - s.mean(); sign = float(np.mean(d ** 3))          # right-skewed when the drive is positive
    if sign < 0:
        s = -s
    return grid, s
