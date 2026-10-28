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


def _hp(x: np.ndarray, fc_hz: float, fs: float, order: int = 2) -> np.ndarray:
    b, a = butter(order, fc_hz / (fs / 2.0), "high"); return filtfilt(b, a, x, axis=0)


def gps_windows(gps_t, gps_v, gps_acc=None, max_acc_m: float = 30.0, max_gap_s: float = 3.0,
                min_len_s: float = 15.0, min_fixes: int = 10) -> list[tuple[float, float]]:
    """Spans of steady GPS: good fixes (accuracy ≤ max_acc_m, speed present) with no gap over
    max_gap_s between consecutive fixes, at least min_len_s long and min_fixes fixes. Outside
    these spans there is no usable GPS, and nothing is fused there."""
    t = np.asarray(gps_t, float); v = np.asarray(gps_v, float)
    ok = np.isfinite(t) & np.isfinite(v) & (v >= 0)
    if gps_acc is not None:
        acc = np.asarray(gps_acc, float); ok &= ~np.isfinite(acc) | (acc <= max_acc_m)
    t = np.sort(t[ok])
    if t.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(t) > max_gap_s) + 1
    out = []
    for seg in np.split(t, cuts):
        if seg.size >= min_fixes and seg[-1] - seg[0] >= min_len_s:
            out.append((float(seg[0]), float(seg[-1])))
    return out


def gps_lag(grid, surge, gps_t, gps_v, windows, fs: float, max_lag_s: float = 1.2,
            band=(0.2, 0.8), min_corr: float = 0.1) -> float:
    """Seconds by which the phone's GPS speed trails the hull unit's clock, from the cross-correlation
    of the stroke-band GPS speed against the stroke-band integral of the surge over the steady spans.
    The lag is searched within ± half a stroke so the stroke period cannot alias it. Positive = GPS
    late. 0 when there is too little data or no clear peak (correlation under min_corr)."""
    v_acc = np.cumsum(surge) / fs
    lags = np.arange(-int(max_lag_s * fs), int(max_lag_s * fs) + 1)
    acc_sum = np.zeros(lags.size); n_tot = 0
    for w0, w1 in windows:
        m = (grid >= w0) & (grid <= w1)
        if m.sum() < int(30 * fs):
            continue
        g = grid[m]
        sel = (gps_t >= w0) & (gps_t <= w1)
        vg = np.interp(g, gps_t[sel], gps_v[sel])
        a = _bp(v_acc[m] - np.polyval(np.polyfit(g - g[0], v_acc[m], 1), g - g[0]), band[0], band[1], fs)
        b = _bp(vg - vg.mean(), band[0], band[1], fs)
        if a.std() == 0 or b.std() == 0:
            continue
        a = a / a.std(); b = b / b.std()
        for i, L in enumerate(lags):     # GPS shifted earlier by L samples lines up with the hull
            if L >= 0:
                x, y = a[: a.size - L], b[L:]
            else:
                x, y = a[-L:], b[: b.size + L]
            acc_sum[i] += float(np.dot(x, y))
        n_tot += a.size
    if n_tot == 0 or not np.isfinite(acc_sum).any() or acc_sum.max() / n_tot < min_corr:
        return 0.0
    return float(lags[int(np.argmax(acc_sum))] / fs)


def fused_speed(grid, surge, gps_t, gps_v, gps_acc=None, fs: float = 25.0, fc_hz: float = 0.15,
                windows=None, lag_s=None):
    """(grid, v_mps, windows, lag_s): boat speed on the surge grid, GPS plus hull accelerometer.

    A complementary filter per steady-GPS span: the GPS speed (shifted earlier by its lag) supplies
    everything below fc_hz, the integrated surge everything above it. Outside the steady spans the
    speed is NaN: the accelerometer only refines GPS that is there, it never replaces it.
    grid/surge: from hull_surge (uniform, fs). gps_t/gps_v/gps_acc: epoch s, m/s, m.
    """
    grid = np.asarray(grid, float); surge = np.asarray(surge, float)
    gps_t = np.asarray(gps_t, float); gps_v = np.asarray(gps_v, float)
    ok = np.isfinite(gps_t) & np.isfinite(gps_v)
    if gps_acc is not None:
        acc = np.asarray(gps_acc, float); ok &= ~np.isfinite(acc) | (acc <= 30.0)
    gps_t, gps_v = gps_t[ok], gps_v[ok]
    order = np.argsort(gps_t); gps_t, gps_v = gps_t[order], gps_v[order]
    if windows is None:
        windows = gps_windows(gps_t, gps_v)
    out = np.full(grid.size, np.nan)
    if not windows or grid.size == 0:
        return grid, out, windows, 0.0
    if lag_s is None:
        lag_s = gps_lag(grid, surge, gps_t, gps_v, windows, fs)
    tg = gps_t - lag_s
    v_acc = np.cumsum(surge) / fs
    for w0, w1 in windows:
        m = (grid >= w0 - lag_s) & (grid <= w1 - lag_s)
        if m.sum() < int(fs * 10):
            continue
        g = grid[m]; sel = (tg >= w0 - lag_s) & (tg <= w1 - lag_s)
        if sel.sum() < 4:
            continue
        vg = np.interp(g, tg[sel], gps_v[sel])
        va = v_acc[m]; va = va - np.polyval(np.polyfit(g - g[0], va, 1), g - g[0])
        out[m] = _lp(vg, fc_hz, fs) + _hp(va, fc_hz, fs)
    return grid, out, windows, float(lag_s)
