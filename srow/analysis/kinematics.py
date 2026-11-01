"""Oar and hull kinematics for the loop charts and the replay (design: SyncRow Replay and Loops).

The engine's sweep signal (frame.py) is one fixed sensor axis, built for timing the catch where the
blade is square. It is the wrong thing to draw loops from: during the feathered recovery the oar has
rolled 90°, so part of the sweep leaks onto the other sensor axes. Here each oar unit's own fused
orientation (roll, pitch) gives gravity's direction in the sensor frame at every sample, and:

    sweep rate   = gyro · gravity direction          rotation about the true vertical, feather or not
    blade height = angle between the shaft axis (the feather axis the engine finds) and the horizontal,
                   read from the orientation, so it does not drift; scaled to centimetres above the
                   water with the run's rigging (outboard, pin height above water)
    feather      = integrated rotation about the shaft, zeroed where the blade is square (the drive)
    sweep angle  = integrated sweep rate, closed stroke by stroke (gyro bias cannot accumulate)

Which way is the drive: the engine's frame has a deterministic but physically arbitrary sign (on the
October piece it came out one way over the whole piece and the other way over a two-minute window),
so its "catches" are catches or finishes depending on the window. Here the drive is the half-cycle
in which the shaft tilts more steeply down toward the blade (the blade is in the water), from the
unit's orientation; the stroke boundaries used for the loops are the reversals at the stern end of
the arc (rising zero crossings of the drive-positive rate). Without an orientation the engine's
sign is kept and flagged as assumed.

The sweep angle's zero is the middle of the mean arc ("midpoint"). An absolute zero (the oar
perpendicular to the hull) was planned from the units' yaw, but these units run a 6-axis fusion whose
yaw is a gyro integral about the sensor's own z axis: on the October piece it swings 1-2° where the
sweep swings 65°, so it carries no heading. An absolute reference needs a calibration gesture at the
start of a run (oar held square and perpendicular for a moment); `perpendicular_offset_deg` in the
output is the hook for it and is 0 until then.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import butter, filtfilt

from .frame import SensorFrame

FS_OUT = 25.0            # output grid (Hz); the units deliver fresh values at 20-25 Hz
SWEEP_LP_HZ = 3.0
HEIGHT_LP_HZ = 4.0
FEATHER_LP_HZ = 8.0
FEATHER_SQUARE_DEG = 45.0   # |feather| below this = square
_EPS = 1e-9


@dataclass
class StrokeLoop:
    t0: float                 # this oar's catch (epoch s)
    t1: float                 # next catch
    i0: int                   # grid indices [i0, i1)
    i1: int
    catch_deg: float          # sweep angle at the catch (minimum)
    finish_deg: float         # sweep angle at the finish (maximum)
    release_s: float | None   # when the sweep rate crosses from drive to recovery
    square_before_ms: float | None   # blade square this long before the catch
    feather_after_ms: float | None   # blade feathered this long after the release
    feather_range_deg: float
    min_elev_deg: float
    max_elev_deg: float

    @property
    def arc_deg(self) -> float:
        return self.finish_deg - self.catch_deg

    @property
    def period_s(self) -> float:
        return self.t1 - self.t0


@dataclass
class OarKinematics:
    source: str
    grid: np.ndarray
    sweep_rate: np.ndarray        # deg/s, drive positive
    sweep_angle: np.ndarray       # deg
    elev_deg: np.ndarray          # shaft elevation toward the blade (+ = blade end up); NaN without orientation
    feather_deg: np.ndarray       # 0 = square
    strokes: list[StrokeLoop]
    angle_reference: str          # "midpoint" (an absolute reference needs a calibration gesture)
    perpendicular_offset_deg: float = 0.0
    quality: dict = field(default_factory=dict)


@dataclass
class HullAttitude:
    source: str
    grid: np.ndarray
    roll_deg: np.ndarray          # + = rolls to one side; the mounting offset (median) removed
    pitch_deg: np.ndarray         # + = bow up, as mounted; median removed
    yaw_deg: np.ndarray           # unwrapped heading


def _lp(x: np.ndarray, fc: float, fs: float) -> np.ndarray:
    if x.shape[0] < 16 or fc <= 0 or fc >= fs / 2:
        return x
    b, a = butter(2, fc / (fs / 2))
    return filtfilt(b, a, x, axis=0)


def _grid(times_s: np.ndarray, fs: float) -> np.ndarray:
    return np.arange(times_s[0], times_s[-1], 1.0 / fs)


def _interp_cols(grid: np.ndarray, t: np.ndarray, x: np.ndarray) -> np.ndarray:
    x = np.atleast_2d(x.T).T
    return np.column_stack([np.interp(grid, t, np.nan_to_num(x[:, i])) for i in range(x.shape[1])])


def _fresh(times_s, accel, gyro, euler):
    """Drop held rows (a gyro row identical to the previous one is a repeated packet, not motion)."""
    keep = np.r_[True, np.any(gyro[1:] != gyro[:-1], axis=1)]
    return (times_s[keep], accel[keep], gyro[keep], None if euler is None else euler[keep])


def gravity_in_body(roll_deg: np.ndarray, pitch_deg: np.ndarray) -> np.ndarray:
    """Unit vector of gravity in the sensor frame from the unit's fused roll (about x) and pitch
    (about y), rotation order Z-Y-X as the WitMotion units report it."""
    r = np.radians(roll_deg); p = np.radians(pitch_deg)
    g = np.column_stack([-np.sin(p), np.sin(r) * np.cos(p), np.cos(r) * np.cos(p)])
    return g / np.maximum(np.linalg.norm(g, axis=1), _EPS)[:, None]


def _integrate(x: np.ndarray, fs: float) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum((x[1:] + x[:-1]) / 2.0) / fs])


def _close_per_stroke(angle: np.ndarray, grid: np.ndarray, catches: np.ndarray, period_ok=(1.2, 5.0)) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Remove gyro drift stroke by stroke: within each catch-to-catch span subtract the linear ramp
    that makes the angle return to its start; outside the strokes subtract a running mean. Returns the
    corrected angle and the strokes' [i0, i1) index spans."""
    out = angle.copy()
    spans = []
    fs = 1.0 / (grid[1] - grid[0])
    T = float(np.median(np.diff(catches))) if catches.size >= 2 else 2.5
    win = max(int(3 * T * fs), 8)
    run = np.convolve(angle, np.ones(win) / win, mode="same")
    out = angle - run
    for a, b in zip(catches[:-1], catches[1:]):
        if not (period_ok[0] < b - a < period_ok[1]):
            continue
        i0, i1 = int(np.searchsorted(grid, a)), int(np.searchsorted(grid, b))
        if i1 - i0 < 8:
            continue
        seg = angle[i0:i1]
        ramp = (seg[-1] - seg[0]) * (np.arange(i1 - i0) / max(i1 - i0 - 1, 1))
        closed = seg - ramp
        # place the closed stroke on the running-mean-free angle at its start (continuity)
        out[i0:i1] = closed - closed[0] + out[i0]
        spans.append((i0, i1))
    return out, spans


def _spans_mask(grid: np.ndarray, catches: np.ndarray, period_ok=(1.2, 5.0)) -> np.ndarray:
    """True on the samples inside catch-to-catch spans of a rowing-like length; all True if none."""
    m = np.zeros(grid.size, bool)
    for a, b in zip(catches[:-1], catches[1:]):
        if period_ok[0] < b - a < period_ok[1]:
            m[int(np.searchsorted(grid, a)):int(np.searchsorted(grid, b))] = True
    if not m.any():
        m[:] = True
    return m


def _rising_crossings(rate: np.ndarray, grid: np.ndarray, refractory_s: float, min_peak: float) -> np.ndarray:
    """Times where the drive-positive rate crosses zero upwards (the reversal at the stern end of the
    arc), at least `refractory_s` apart, each followed by a drive peak of at least `min_peak` deg/s."""
    idx = np.where((rate[:-1] <= 0) & (rate[1:] > 0))[0] + 1
    out: list[float] = []
    last = -np.inf
    fs = 1.0 / (grid[1] - grid[0])
    look = max(int(refractory_s * fs), 2)
    for i in idx:
        t = float(grid[i] - (rate[i] / (rate[i] - rate[i - 1])) / fs)   # linear interpolation of the crossing
        if t - last < refractory_s:
            continue
        if rate[i:i + look].max() < min_peak:
            continue
        out.append(t); last = t
    return np.asarray(out, float)


def _crossings_down(x: np.ndarray, level: float) -> np.ndarray:
    """Indices where x goes from above `level` to at or below it."""
    return np.where((x[:-1] > level) & (x[1:] <= level))[0] + 1


def blade_height_cm(elev_deg, rig) -> np.ndarray:
    """Blade height above the water from the shaft's elevation and the rigging (pin height above the
    water, outboard). The water line is computed, not measured: see the design's caveat."""
    e = np.asarray(elev_deg, float)
    return 100.0 * (rig.pin_height_m + rig.outboard_m * np.sin(np.radians(e)))


def oar_kinematics(source: str, times_s, accel, gyro, euler, frame: SensorFrame, catches_s,
                   fs: float = FS_OUT) -> OarKinematics | None:
    """Per-sample sweep rate, sweep angle, shaft elevation and feather for one oar unit on a uniform
    grid, plus the per-stroke loops. `frame` is the engine's SensorFrame for this unit (sign-aligned to
    the stroke seat), `catches_s` this unit's catches. Centimetres come from blade_height_cm."""
    times_s = np.asarray(times_s, float); accel = np.asarray(accel, float); gyro = np.asarray(gyro, float)
    euler = None if euler is None else np.asarray(euler, float)
    if euler is not None and not np.isfinite(euler[:, :2]).any():
        euler = None
    times_s, accel, gyro, euler = _fresh(times_s, accel, gyro, euler)
    if times_s.size < 64 or times_s[-1] - times_s[0] < 5.0:
        return None
    grid = _grid(times_s, fs)
    if grid.size < 64:
        return None
    # no mean subtraction: in the sensor frame the sweep lands on different axes when the blade is
    # square and when it is feathered, so the raw mean is real motion; gyro bias is removed stroke by
    # stroke by the closure in _close_per_stroke instead
    W = _interp_cols(grid, times_s, gyro)
    sig = np.interp(grid, frame.times_s, frame.signal)
    quality: dict = {"orientation": euler is not None, "flip_axis": frame.flip is not None}

    # --- sweep rate about the vertical -------------------------------------------------------
    if euler is not None:
        gb = gravity_in_body(np.interp(grid, times_s, euler[:, 0]), np.interp(grid, times_s, euler[:, 1]))
        an = _interp_cols(grid, times_s, accel); an /= np.maximum(np.linalg.norm(an, axis=1), _EPS)[:, None]
        if np.median(np.einsum("ij,ij->i", gb, an)) < 0:
            gb = -gb
        rate = np.einsum("ij,ij->i", W, gb)
    else:
        rate = W @ frame.sweep
    rate = _lp(rate, SWEEP_LP_HZ, fs)
    eng = np.sort(np.asarray([x for x in (list(catches_s) if catches_s is not None else []) if grid[0] <= x <= grid[-1]], float))
    T = float(np.median(np.diff(eng))) if eng.size >= 3 else 2.5
    # the checks below look at the strokes only: idle stretches have no drive and no recovery
    in_stroke = _spans_mask(grid, eng)
    c = np.corrcoef(rate[in_stroke], sig[in_stroke])[0, 1] if np.std(rate[in_stroke]) > _EPS and np.std(sig[in_stroke]) > _EPS else 1.0
    if c < 0:
        rate = -rate
    quality["rate_vs_engine_corr"] = float(abs(c))

    # --- which half is the drive -----------------------------------------------------------------
    flip = frame.flip
    elev_or = None
    if euler is not None and flip is not None:
        elev_or = _lp(np.degrees(np.arcsin(np.clip(gb @ flip, -1.0, 1.0))), HEIGHT_LP_HZ, fs)
        if np.median(elev_or[in_stroke]) > 0:          # the shaft always tilts down toward the blade
            flip = -flip; elev_or = -elev_or
        pos = in_stroke & (rate > 0); neg = in_stroke & (rate <= 0)
        if pos.any() and neg.any():
            inverted = bool(np.median(elev_or[pos]) > np.median(elev_or[neg]))   # the drive is the deeper half
            if inverted:
                rate = -rate
            quality["drive_sign"] = "elevation"; quality["engine_sign_inverted"] = inverted
            quality["shaft_elev_drive_deg"] = float(np.median(elev_or[in_stroke & (rate > 0)]))
            quality["shaft_elev_recovery_deg"] = float(np.median(elev_or[in_stroke & (rate <= 0)]))
        else:
            quality["drive_sign"] = "assumed"
    else:
        quality["drive_sign"] = "assumed"

    # --- stroke boundaries: the reversal at the stern end of the arc ---------------------------------
    catches = _rising_crossings(rate, grid, refractory_s=0.5 * T, min_peak=20.0)
    if catches.size < 2 and eng.size >= 2:
        catches = eng
    in_stroke = _spans_mask(grid, catches)

    # --- sweep angle, closed per stroke, midpoint reference -----------------------------------
    angle_raw = _integrate(rate, fs)
    angle, spans = _close_per_stroke(angle_raw, grid, catches)
    mids = [(angle[i0:i1].max() + angle[i0:i1].min()) / 2 for i0, i1 in spans]
    angle = angle - (float(np.median(mids)) if mids else float(np.mean(angle)))
    reference = "midpoint"

    # --- feather and blade height -------------------------------------------------------------
    if flip is not None:
        frate = _lp(W @ flip, FEATHER_LP_HZ, fs)
        phi, _ = _close_per_stroke(_integrate(frate, fs), grid, catches)
        for i0, i1 in spans:                       # zero where the blade is square: the drive
            drive = rate[i0:i1] > 0
            if drive.any():
                phi[i0:i1] -= np.median(phi[i0:i1][drive])
        feather = phi
    else:
        feather = np.zeros(grid.size)

    elev = np.full(grid.size, np.nan)
    if elev_or is not None:
        # within-stroke shape from the gyro (rotation about the horizontal axis perpendicular to the
        # shaft, which neither the sweep nor the feather can leak into), level from the orientation
        # during the recovery, where the oar is lightly loaded and the orientation filter is trusted
        e_axis = np.cross(gb, flip[None, :]); e_axis /= np.maximum(np.linalg.norm(e_axis, axis=1), _EPS)[:, None]
        erate = -np.einsum("ij,ij->i", W, e_axis)          # + = blade end rising
        elev_g, _ = _close_per_stroke(_integrate(_lp(erate, HEIGHT_LP_HZ, fs), fs), grid, catches)
        elev = elev_or.copy()
        for i0, i1 in spans:
            rec = rate[i0:i1] <= 0
            if rec.sum() >= 4:
                elev[i0:i1] = elev_g[i0:i1] - np.median(elev_g[i0:i1][rec]) + np.median(elev_or[i0:i1][rec])
        quality["height_gyro_vs_orientation_drive_deg"] = float(np.median(np.abs(elev - elev_or)[in_stroke & (rate > 0)])) if (in_stroke & (rate > 0)).any() else None

    # --- per-stroke loops ---------------------------------------------------------------------
    strokes: list[StrokeLoop] = []
    for n, (i0, i1) in enumerate(spans):
        seg = angle[i0:i1]
        a0 = grid[i0]; a1 = grid[i1] if i1 < grid.size else grid[-1]
        T = a1 - a0
        rel = None
        j0 = i0 + int(0.25 * T * fs)
        cross = np.where((rate[j0:i1 - 1] > 0) & (rate[j0 + 1:i1] <= 0))[0]
        if cross.size:
            rel = float(grid[j0 + cross[0]])
        sq_before = ft_after = None
        if flip is not None:
            # squaring: the last time |feather| drops below 45° in the window from 0.6 T before the
            # catch to 0.25 T after it (positive = squared before the catch)
            back = max(0, i0 - int(0.6 * T * fs)); fwd = min(grid.size, i0 + int(0.25 * T * fs))
            c_sq = _crossings_down(np.abs(feather[back:fwd]), FEATHER_SQUARE_DEG)
            if c_sq.size:
                sq_before = float((grid[i0] - grid[back + c_sq[-1]]) * 1000.0)
            if rel is not None:
                # feathering: the first time |feather| rises past 45° from 0.4 T before the release to
                # 0.6 T after it (negative = feathered before the blade stopped sweeping)
                r0 = int(np.searchsorted(grid, rel)); b0 = max(i0, r0 - int(0.4 * T * fs)); b1 = min(grid.size, r0 + int(0.6 * T * fs))
                up = np.where((np.abs(feather[b0:b1 - 1]) < FEATHER_SQUARE_DEG) & (np.abs(feather[b0 + 1:b1]) >= FEATHER_SQUARE_DEG))[0]
                if up.size:
                    ft_after = float((grid[b0 + up[0] + 1] - rel) * 1000.0)
        h = elev[i0:i1]
        strokes.append(StrokeLoop(
            t0=float(a0), t1=float(a1), i0=i0, i1=i1,
            catch_deg=float(seg.min()), finish_deg=float(seg.max()), release_s=rel,
            square_before_ms=sq_before, feather_after_ms=ft_after,
            feather_range_deg=float(np.ptp(feather[i0:i1])) if flip is not None else 0.0,
            min_elev_deg=float(np.nanmin(h)) if np.isfinite(h).any() else float("nan"),
            max_elev_deg=float(np.nanmax(h)) if np.isfinite(h).any() else float("nan")))
    return OarKinematics(source=source, grid=grid, sweep_rate=rate, sweep_angle=angle, elev_deg=elev,
                         feather_deg=feather, strokes=strokes, angle_reference=reference, quality=quality)


def hull_attitude(source: str, times_s, gyro, euler, fs: float = FS_OUT) -> HullAttitude | None:
    """The hull unit's roll and pitch (mounting offset removed) and unwrapped heading on a uniform grid."""
    if euler is None:
        return None
    times_s = np.asarray(times_s, float); gyro = np.asarray(gyro, float); euler = np.asarray(euler, float)
    if not np.isfinite(euler).any():
        return None
    keep = np.r_[True, np.any(gyro[1:] != gyro[:-1], axis=1)] & np.isfinite(euler).all(axis=1)
    times_s, euler = times_s[keep], euler[keep]
    if times_s.size < 64 or times_s[-1] - times_s[0] < 5.0:
        return None
    grid = _grid(times_s, fs)
    cols = [np.interp(grid, times_s, np.degrees(np.unwrap(np.radians(euler[:, i])))) for i in range(3)]
    roll = _lp(cols[0] - np.median(cols[0]), HEIGHT_LP_HZ, fs)
    pitch = _lp(cols[1] - np.median(cols[1]), HEIGHT_LP_HZ, fs)
    return HullAttitude(source=source, grid=grid, roll_deg=roll, pitch_deg=pitch, yaw_deg=cols[2])


def _haversine_m(lat1, lon1, lat2, lon2) -> np.ndarray:
    R = 6371000.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1; dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def gps_distance(t_s, lat, lon, acc=None, max_acc_m: float = 30.0, max_gap_s: float = 3.0) -> dict:
    """Distance rowed over the stretches with usable GPS, and the average 500 m split over them.
    The app re-writes the last fix every second, so a row identical to the previous one is not a fix.
    Returns {"distance_m", "moving_s", "split_s_per_500", "n_fixes"}."""
    t = np.asarray(t_s, float); la = np.asarray(lat, float); lo = np.asarray(lon, float)
    ok = np.isfinite(la) & np.isfinite(lo) & np.isfinite(t)
    if acc is not None:
        a = np.asarray(acc, float); ok &= ~np.isfinite(a) | (a <= max_acc_m)
    t, la, lo = t[ok], la[ok], lo[ok]
    if t.size < 2:
        return {"distance_m": 0.0, "moving_s": 0.0, "split_s_per_500": None, "n_fixes": int(t.size)}
    order = np.argsort(t); t, la, lo = t[order], la[order], lo[order]
    fresh = np.r_[True, (la[1:] != la[:-1]) | (lo[1:] != lo[:-1])]
    t, la, lo = t[fresh], la[fresh], lo[fresh]
    if t.size < 2:
        return {"distance_m": 0.0, "moving_s": 0.0, "split_s_per_500": None, "n_fixes": int(t.size)}
    d = _haversine_m(la[:-1], lo[:-1], la[1:], lo[1:]); dt = np.diff(t)
    use = (dt > 0) & (dt <= max_gap_s)
    dist = float(d[use].sum()); secs = float(dt[use].sum())
    split = (500.0 * secs / dist) if dist > 50 and secs > 0 else None
    return {"distance_m": dist, "moving_s": secs, "split_s_per_500": split, "n_fixes": int(t.size)}


def format_split(seconds: float | None) -> str:
    if seconds is None or not np.isfinite(seconds):
        return "–"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d} /500 m"
