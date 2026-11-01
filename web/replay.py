"""Replay payload: one JSON document per run with everything the Replay tab draws, on one shared
25 Hz grid, so the browser can animate without further requests.

    grid:    {"t0": epoch_s, "hz": 25, "n": samples}
    oars:    [{"name", "color", "angle", "rate", "height" (cm above water, or null per sample when
               unknown), "feather", "strokes": [{"t0","t1","i0","i1","catch","finish","flagged"}]}]
    hull:    {"name", "accel" (m/s², catch check negative), "speed" (m/s, null where unknown),
              "roll", "pitch"} or null
    gps:     [[t, lat, lon, dist_m_so_far], ...] usable, de-duplicated fixes
    ref:     the reference oar's name
    strokes: per reference stroke: {"t0","t1","spm","dt": {seat: ms}, "rho": {seat: rho}, "ok": {seat: bool},
              "piece": bool, "speed": m/s or null, "split": s/500 m or null, "dv": m/s or null}
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from srow.analysis.kinematics import blade_height_cm, gps_distance
from web.charts import PALETTE, HULL_COLOR, _fold

HZ = 25.0


def _r(x, d=2):
    """Round a float array for JSON; NaN -> None."""
    out = []
    for v in np.asarray(x, float):
        out.append(None if not np.isfinite(v) else round(float(v), d))
    return out


def _interp(grid, t, y, max_gap_s: float | None = None):
    """Linear interpolation of (t, y) onto grid; NaN outside the data and, with max_gap_s, inside gaps
    longer than that."""
    t = np.asarray(t, float); y = np.asarray(y, float)
    ok = np.isfinite(t) & np.isfinite(y)
    t, y = t[ok], y[ok]
    if t.size < 2:
        return np.full(grid.size, np.nan)
    out = np.interp(grid, t, y, left=np.nan, right=np.nan)
    if max_gap_s is not None:
        j = np.clip(np.searchsorted(t, grid), 1, t.size - 1)
        gap = t[j] - t[j - 1]
        out[gap > max_gap_s] = np.nan
    return out


def hull_sign_by_catch_check(grid: np.ndarray, accel: np.ndarray, catches: np.ndarray) -> float:
    """+1 if the hull signal's sharp minimum (the catch check) comes before its maximum within the
    mean stroke, else -1 (the sign to multiply by). hull_surge's own sign test assumes the drive push
    outweighs the check, which is not so for a scull."""
    catches = np.asarray(catches, float)
    if catches.size < 4:
        return 1.0
    T = float(np.median(np.diff(catches)))
    tt = np.linspace(0, T, int(T * 50))
    H = _fold(grid, accel, catches, tt)
    if H.shape[0] < 3:
        return 1.0
    hm = H.mean(axis=0)
    return -1.0 if int(np.argmax(hm)) < int(np.argmin(hm)) else 1.0


def replay_payload(bundle: dict, gdf, rig=None) -> dict | None:
    """Build the replay document from the analysis bundle (web.main._analysis), the GPS track and the
    run's rigging. None when there are no oar kinematics."""
    kin = (bundle or {}).get("kin") or {}
    if not kin:
        return None
    cross = bundle.get("cross")
    flags = bundle.get("flags") or {}
    t0 = max(float(k.grid[0]) for k in kin.values()); t1 = min(float(k.grid[-1]) for k in kin.values())
    n = int((t1 - t0) * HZ)
    if n < HZ * 5:
        return None
    grid = t0 + np.arange(n) / HZ
    ref = cross.reference if cross is not None and cross.reference in kin else next(iter(kin))

    oars = []
    for i, (src, k) in enumerate(kin.items()):
        height = blade_height_cm(k.elev_deg, rig) if rig is not None and np.isfinite(k.elev_deg).any() else None
        strokes = []
        for j, s in enumerate(k.strokes):
            strokes.append({"t0": round(s.t0, 3), "t1": round(s.t1, 3),
                            "i0": int(np.clip(np.searchsorted(grid, s.t0), 0, n)), "i1": int(np.clip(np.searchsorted(grid, s.t1), 0, n)),
                            "catch": round(s.catch_deg, 1), "finish": round(s.finish_deg, 1), "flagged": j in flags.get(src, set())})
        oars.append({"name": str(src), "color": PALETTE[i % len(PALETTE)],
                     "angle": _r(_interp(grid, k.grid, k.sweep_angle), 1), "rate": _r(_interp(grid, k.grid, k.sweep_rate), 1),
                     "height": _r(_interp(grid, k.grid, height), 1) if height is not None else None,
                     "feather": _r(_interp(grid, k.grid, k.feather_deg), 0), "strokes": strokes})

    ref_catches = np.array([s.t0 for s in kin[ref].strokes], float)
    hull = None
    hull_series = bundle.get("hull") or {}
    if hull_series:
        hname, (hg, ha) = next(iter(hull_series.items()))
        hg = np.asarray(hg, float); ha = np.asarray(ha, float) * hull_sign_by_catch_check(hg, np.asarray(ha, float), ref_catches)
        accel = _interp(grid, hg, ha)
        speed = np.full(n, np.nan)
        fused = (bundle.get("fused") or {}).get(hname)
        if fused is not None:
            speed = _interp(grid, fused[0], fused[1])
        if not np.isfinite(speed).any() and gdf is not None and len(gdf) and "speed" in gdf.columns:
            gt, gv = _gps_arrays(gdf)
            speed = _interp(grid, gt, gv, max_gap_s=3.0)
        att = bundle.get("attitude")
        hull = {"name": str(hname), "color": HULL_COLOR, "accel": _r(accel, 2), "speed": _r(speed, 2),
                "roll": _r(_interp(grid, att.grid, att.roll_deg), 1) if att is not None else None,
                "pitch": _r(_interp(grid, att.grid, att.pitch_deg), 1) if att is not None else None}
        accel_arr = accel; speed_arr = speed
    else:
        accel_arr = np.full(n, np.nan); speed_arr = np.full(n, np.nan)
        if gdf is not None and len(gdf) and "speed" in gdf.columns:
            gt, gv = _gps_arrays(gdf)
            speed_arr = _interp(grid, gt, gv, max_gap_s=3.0)

    gps = []
    if gdf is not None and len(gdf) and "latitude" in gdf.columns:
        t = gdf["time"] if "time" in gdf.columns else gdf.index
        gt = np.array([pd.Timestamp(x).timestamp() for x in t], float)
        la = pd.to_numeric(gdf["latitude"], errors="coerce").to_numpy(float); lo = pd.to_numeric(gdf["longitude"], errors="coerce").to_numpy(float)
        acc = pd.to_numeric(gdf["accuracy"], errors="coerce").to_numpy(float) if "accuracy" in gdf.columns else np.full(gt.size, np.nan)
        ok = np.isfinite(gt) & np.isfinite(la) & np.isfinite(lo) & (~np.isfinite(acc) | (acc <= 30.0))
        gt, la, lo = gt[ok], la[ok], lo[ok]
        order = np.argsort(gt); gt, la, lo = gt[order], la[order], lo[order]
        fresh = np.r_[True, (la[1:] != la[:-1]) | (lo[1:] != lo[:-1])]
        gt, la, lo = gt[fresh], la[fresh], lo[fresh]
        dist = np.zeros(gt.size)
        if gt.size >= 2:
            from srow.analysis.kinematics import _haversine_m
            d = _haversine_m(la[:-1], lo[:-1], la[1:], lo[1:]); dt = np.diff(gt)
            d[(dt <= 0) | (dt > 3.0)] = 0.0
            dist[1:] = np.cumsum(d)
        gps = [[round(float(a), 2), round(float(b), 6), round(float(c), 6), round(float(e), 1)] for a, b, c, e in zip(gt, la, lo, dist)]

    # per reference stroke: engine offsets and confidence, speed, split, within-stroke speed change
    eng = []
    if cross is not None:
        eng = sorted(cross.strokes, key=lambda st: st.stroke_time_s)
    eng_t = np.array([st.stroke_time_s for st in eng], float)
    T = float(np.median(np.diff(ref_catches))) if ref_catches.size >= 3 else 2.5
    strokes = []
    for s in kin[ref].strokes:
        i0, i1 = int(np.clip(np.searchsorted(grid, s.t0), 0, n)), int(np.clip(np.searchsorted(grid, s.t1), 0, n))
        rec = {"t0": round(s.t0, 3), "t1": round(s.t1, 3), "spm": round(60.0 / max(s.t1 - s.t0, 1e-6), 1),
               "dt": {}, "rho": {}, "ok": {}, "piece": True, "speed": None, "split": None, "dv": None}
        if eng_t.size:
            j = int(np.argmin(np.abs(eng_t - s.t0)))
            if abs(eng_t[j] - s.t0) <= 0.6 * T:
                st = eng[j]
                rec["dt"] = {k_: round(v, 0) for k_, v in st.offsets_ms.items() if k_ != ref}
                rec["rho"] = {k_: round(v, 2) for k_, v in st.corr.items() if k_ != ref}
                rec["ok"] = {k_: (v == "ok") for k_, v in st.seat_status.items() if k_ != ref}
                rec["piece"] = st.is_piece_stroke()
        sp = speed_arr[i0:i1]
        if i1 > i0 and np.isfinite(sp).any():
            v = float(np.nanmean(sp)); rec["speed"] = round(v, 2); rec["split"] = round(500.0 / v, 1) if v > 0.3 else None
        ac = accel_arr[i0:i1]
        if i1 - i0 >= 8 and np.isfinite(ac).all():
            dv = np.concatenate([[0.0], np.cumsum((ac[1:] + ac[:-1]) / 2) / HZ])
            rec["dv"] = round(float(np.ptp(dv - np.linspace(0, dv[-1], dv.size))), 2)
        strokes.append(rec)

    return {"grid": {"t0": round(t0, 3), "hz": HZ, "n": n}, "oars": oars, "hull": hull, "gps": gps, "ref": str(ref), "strokes": strokes,
            "rig": rig.as_dict() if rig is not None else None}


def _gps_arrays(gdf):
    t = gdf["time"] if "time" in gdf.columns else gdf.index
    gt = np.array([pd.Timestamp(x).timestamp() for x in t], float)
    gv = pd.to_numeric(gdf["speed"], errors="coerce").to_numpy(float)
    if "accuracy" in gdf.columns:
        acc = pd.to_numeric(gdf["accuracy"], errors="coerce").to_numpy(float)
        gv = np.where(np.isfinite(acc) & (acc > 30.0), np.nan, gv)
    return gt, gv
