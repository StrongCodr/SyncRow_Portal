"""Chart/figure builders for the web UI.

Pure functions: DataFrame -> Plotly figure spec (plain dict) or map data.
Backgrounds are transparent so the client theme shows through; the browser
sets colors on render (see restyleCharts in base.html).
"""

import numpy as np
import pandas as pd

PALETTE = [
    "#7aa2f7", "#bb9af7", "#7dcfff", "#9ece6a",
    "#e0af68", "#f7768e", "#2ac3de", "#ff9e64",
]

_BASE_LAYOUT = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "margin": {"l": 62, "r": 20, "t": 10, "b": 40},   # identical on every chart so the plot areas line up
    "hovermode": "x unified",
    "legend": {"orientation": "h", "y": 1.15},
}


def _times(idx) -> list[str]:
    return [t.isoformat() for t in idx]


def _clean(vals) -> list:
    return [None if v != v else float(v) for v in vals]


def _xaxis(title: str, xrange) -> dict:
    """One x-axis spec for every chart: the same explicit range on all of them, so the same second
    sits at the same pixel on each. Without a range each chart would autoscale to its own data."""
    ax = {"title": title}
    if xrange:
        ax["range"] = [xrange[0], xrange[1]]; ax["autorange"] = False
    return ax


def shared_range(cross, df: pd.DataFrame | None, gdf, pad_s: float = 5.0) -> list[str] | None:
    """The time span all three charts open on: first to last piece stroke, padded, when the engine
    found a piece; otherwise the union of the data. ISO strings for Plotly date axes."""
    import datetime as _dt
    lo = hi = None
    piece = [st for st in (cross.strokes if cross is not None else []) if st.is_piece_stroke()]
    if len(piece) >= 2:
        lo = _dt.datetime.fromtimestamp(piece[0].stroke_time_s - pad_s, tz=_dt.timezone.utc)
        hi = _dt.datetime.fromtimestamp(piece[-1].stroke_time_s + pad_s, tz=_dt.timezone.utc)
    else:
        for frame, col in ((df, "time"), (gdf, "time")):
            if frame is None or len(frame) == 0:
                continue
            t = pd.to_datetime(frame[col] if col in frame.columns else frame.index, utc=True)
            a, b = t.min(), t.max()
            lo = a if lo is None or a < lo else lo; hi = b if hi is None or b > hi else hi
    return [lo.isoformat(), hi.isoformat()] if lo is not None and hi is not None and hi > lo else None


def _imu_field(df: pd.DataFrame):
    """Pick a rotation-invariant field: gyro magnitude if possible, else a channel."""
    d = df.copy()
    if all(c in d.columns for c in ("wx", "wy", "wz")):
        d["gyro_mag"] = np.sqrt(d["wx"] ** 2 + d["wy"] ** 2 + d["wz"] ** 2)
        return d, "gyro_mag"
    for c in ("pitch", "roll", "yaw", "az"):
        if c in d.columns:
            return d, c
    return d, None


def imu_fig(df: pd.DataFrame | None, xrange=None):
    """Per-sensor line chart of the rotation-invariant IMU field."""
    if df is None or df.empty or "source" not in df.columns:
        return None
    d, field = _imu_field(df)
    if field is None:
        return None
    data = []
    for i, (src, g) in enumerate(d.groupby("source")):
        g = g.sort_values("time")
        data.append({
            "type": "scatter", "mode": "lines", "name": str(src),
            "x": _times(g["time"]), "y": _clean(g[field]),
            "line": {"width": 1.2, "color": PALETTE[i % len(PALETTE)]},
        })
    layout = {**_BASE_LAYOUT, "height": 300,
              "xaxis": _xaxis("time", xrange),
              "yaxis": {"title": "deg/s" if field == "gyro_mag" else field, "fixedrange": True, "autorange": True}}
    return {"data": data, "layout": layout}


def offsets_fig(cross, xrange=None):
    """Per-seat catch-timing offset vs the stroke seat, in milliseconds, over the
    piece. Positive = that seat catches AFTER stroke (behind); negative = before
    (ahead); the stroke seat is the flat 0 line. This replaces the old unitless
    1/(1+spread) 'synchronicity' score — see `IntervalAnalysis` / `analyze_interval`.

    `cross` is the engine's CrossResult (srow.analysis.crosssensor). Returns None
    if there isn't enough to plot.
    """
    if cross is None or cross.reference is None or not cross.strokes:
        return None
    ref = cross.reference
    seats = [s for s in cross.rowers if s != ref]
    if not seats:
        return None

    import datetime as _dt

    def _ts(epoch_s: float) -> str:
        return _dt.datetime.fromtimestamp(epoch_s, tz=_dt.timezone.utc).isoformat()

    data = []
    # flat zero baseline = the stroke seat (everything is measured against it)
    piece = [st for st in cross.strokes if st.is_piece_stroke()]
    if len(piece) < 2:
        return None
    t0, t1 = _ts(piece[0].stroke_time_s), _ts(piece[-1].stroke_time_s)
    data.append({
        "type": "scatter", "mode": "lines", "name": f"{ref} (stroke)",
        "x": [t0, t1], "y": [0, 0],
        "line": {"width": 1.4, "color": "#9aa5b1", "dash": "dot"},
        "hoverinfo": "skip",
    })
    for i, s in enumerate(seats):
        # only this seat's trustworthy strokes (its own OK status)
        xs, ys, last_t = [], [], None
        for st in piece:
            if st.seat_status.get(s) == "ok" and s in st.offsets_ms:
                if last_t is not None and st.stroke_time_s - last_t > 6.0:
                    xs.append(_ts(last_t + 1.0)); ys.append(None)   # a stop: break the line rather than bridge it
                xs.append(_ts(st.stroke_time_s))
                ys.append(round(st.offsets_ms[s], 1)); last_t = st.stroke_time_s
        if xs:
            data.append({
                "type": "scatter", "mode": "lines+markers", "name": str(s),
                "x": xs, "y": ys,
                "line": {"width": 1.6, "color": PALETTE[i % len(PALETTE)]},
                "marker": {"size": 4},
            })
    if len(data) < 2:  # only the baseline — nothing measured
        return None
    layout = {**_BASE_LAYOUT, "height": 260,
              "xaxis": _xaxis("time (one point per stroke)", xrange),
              "yaxis": {"fixedrange": True, "autorange": True, "title": "ms", "zeroline": True}}
    return {"data": data, "layout": layout}


def offsets_summary(cross) -> list[dict]:
    """Per-seat headline numbers for the offset chart: median ms behind/ahead over
    that seat's trustworthy strokes, plus how many strokes backed it."""
    if cross is None or cross.reference is None or not cross.strokes:
        return []
    ref = cross.reference
    out = []
    piece = [st for st in cross.strokes if st.is_piece_stroke()]
    for s in [x for x in cross.rowers if x != ref]:
        vals = [st.offsets_ms[s] for st in piece
                if st.seat_status.get(s) == "ok" and s in st.offsets_ms]
        if not vals:
            out.append({"seat": str(s), "text": "no clean strokes", "n": 0})
            continue
        med = float(np.median(vals))
        word = "behind" if med >= 0 else "ahead"
        out.append({"seat": str(s), "text": f"{abs(med):.0f} ms {word}", "n": len(vals)})
    return out


def offsets_boat(cross) -> str | None:
    """Boat-level headline: the crew SPREAD (widest gap between any two seats) per
    stroke, summarized as median and p90. This is the single 'how together is the
    boat' number — lower is tighter."""
    if cross is None or cross.reference is None or not cross.strokes:
        return None
    spreads = [st.spread_ms for st in cross.strokes
               if st.is_piece_stroke() and st.n_matched >= 2]
    if not spreads:
        return None
    return f"{np.median(spreads):.0f} ms typical, {np.percentile(spreads, 90):.0f} ms worst"


def speed_fig(gdf, xrange=None) -> dict | None:
    """Boat speed (km/h) over time from GPS."""
    if gdf is None or len(gdf) == 0 or "speed" not in gdf.columns:
        return None
    t = gdf["time"] if "time" in gdf.columns else gdf.index
    speed = pd.to_numeric(gdf["speed"], errors="coerce") * 3.6  # m/s -> km/h
    mask = speed.notna() & (speed >= 0)
    t, speed = pd.Series(list(t))[mask.values], speed[mask.values]
    if len(speed) == 0:
        return None
    data = [{
        "type": "scatter", "mode": "lines", "name": "speed",
        "x": _times(t), "y": _clean(speed),
        "line": {"width": 1.5, "color": "#2ac3de"},
    }]
    layout = {**_BASE_LAYOUT, "height": 200, "showlegend": False,
              "xaxis": _xaxis("time", xrange), "yaxis": {"title": "km/h", "fixedrange": True, "autorange": True}}
    return {"data": data, "layout": layout}


def _decimate(t: np.ndarray, y: np.ndarray, budget: int):
    """At most `budget` points: bucket means over equal spans (the trace keeps its shape when zoomed out)."""
    n = len(t)
    if n <= budget:
        return t, y
    k = int(np.ceil(n / budget)); m = (n // k) * k
    tt = t[:m].reshape(-1, k).mean(axis=1); yy = y[:m].reshape(-1, k).mean(axis=1)
    return tt, yy


HULL_COLOR = "#9ece6a"


def _signal_traces(series: dict, t0: float, t1: float, budget: int, width: float = 1.4, colors=None) -> list[dict]:
    """series: {name: (epoch_s array, values array)} -> Plotly traces clipped to [t0, t1] and decimated."""
    import datetime as _dt
    out = []
    for i, (name, (t, y)) in enumerate(series.items()):
        i0, i1 = np.searchsorted(t, t0), np.searchsorted(t, t1)
        tt, yy = _decimate(t[i0:i1], y[i0:i1], max(budget // max(len(series), 1), 50))
        if len(tt) == 0:
            continue
        out.append({"type": "scatter", "mode": "lines", "name": str(name),
                    "x": [_dt.datetime.fromtimestamp(float(v), tz=_dt.timezone.utc).isoformat() for v in tt],
                    "y": _clean(yy), "line": {"width": width, "color": (colors or PALETTE)[i % len(colors or PALETTE)]}})
    return out


def _catch_trace(catches_s, t0: float, t1: float, name: str) -> dict | None:
    """The stroke seat's catches as markers on the zero line (drawn only when few enough to read)."""
    import datetime as _dt
    c = np.asarray([x for x in (catches_s or []) if t0 <= x <= t1], float)
    if c.size == 0 or c.size > 400:
        return None
    return {"type": "scatter", "mode": "markers", "name": name, "hoverinfo": "x+name",
            "x": [_dt.datetime.fromtimestamp(float(v), tz=_dt.timezone.utc).isoformat() for v in c],
            "y": [0.0] * int(c.size), "marker": {"symbol": "line-ns-open", "size": 14, "color": "#9aa5b1", "line": {"width": 1.4}}}


def _span(xrange, series: dict):
    import datetime as _dt
    if xrange:
        return (_dt.datetime.fromisoformat(xrange[0]).timestamp(), _dt.datetime.fromisoformat(xrange[1]).timestamp())
    lo = min(float(t[0]) for t, _ in series.values()); hi = max(float(t[-1]) for t, _ in series.values())
    return lo, hi


def sweep_fig(oars: dict, catches_s, xrange=None, budget: int = 3200) -> dict | None:
    """Each oar's sweep angular velocity (deg/s, drive positive) with the stroke seat's catches."""
    if not oars:
        return None
    t0, t1 = _span(xrange, oars)
    data = _signal_traces(oars, t0, t1, budget)
    if not data:
        return None
    c = _catch_trace(catches_s, t0, t1, "catch (stroke seat)")
    if c:
        data.append(c)
    layout = {**_BASE_LAYOUT, "height": 300, "xaxis": _xaxis("time", xrange),
              "yaxis": {"title": "deg/s", "fixedrange": True, "autorange": True, "zeroline": True}}
    return {"data": data, "layout": layout}


def hull_fig(hull: dict, catches_s, xrange=None, budget: int = 3200) -> dict | None:
    """The hull unit's fore-aft acceleration (m/s², drive positive) with the stroke seat's catches."""
    if not hull:
        return None
    t0, t1 = _span(xrange, hull)
    data = _signal_traces(hull, t0, t1, budget, colors=[HULL_COLOR])
    if not data:
        return None
    c = _catch_trace(catches_s, t0, t1, "catch (stroke seat)")
    if c:
        data.append(c)
    layout = {**_BASE_LAYOUT, "height": 240, "xaxis": _xaxis("time", xrange),
              "yaxis": {"title": "m/s²", "fixedrange": True, "autorange": True, "zeroline": True}}
    return {"data": data, "layout": layout}


FUSED_COLOR = "#73daca"


def _kmh(series: dict) -> dict:
    return {k: (t, np.asarray(v, float) * 3.6) for k, (t, v) in series.items()}


def _gps_trace(gps, t0: float, t1: float, limit: int = 600) -> dict | None:
    """The GPS fixes the fused speed is anchored to, as dots (km/h), when few enough to read."""
    import datetime as _dt
    if gps is None:
        return None
    t, v = gps
    m = (t >= t0) & (t <= t1)
    if not m.any() or int(m.sum()) > limit:
        return None
    return {"type": "scatter", "mode": "markers", "name": "GPS fix", "hoverinfo": "x+y+name",
            "x": [_dt.datetime.fromtimestamp(float(x), tz=_dt.timezone.utc).isoformat() for x in t[m]],
            "y": _clean(v[m] * 3.6), "marker": {"size": 5, "color": "#2ac3de"}}


def fused_fig(fused: dict, gps, xrange=None, budget: int = 3200) -> dict | None:
    """Boat speed (km/h) from GPS refined by the hull unit's accelerometer, with the GPS fixes it rests on.
    Blank where there is no steady GPS."""
    if not fused:
        return None
    t0, t1 = _span(xrange, fused)
    data = _signal_traces(_kmh(fused), t0, t1, budget, width=1.6, colors=[FUSED_COLOR])
    if not data:
        return None
    g = _gps_trace(gps, t0, t1)
    if g:
        data.append(g)
    layout = {**_BASE_LAYOUT, "height": 220, "xaxis": _xaxis("time", xrange),
              "yaxis": {"title": "km/h", "fixedrange": True, "autorange": True}}
    return {"data": data, "layout": layout}


def signal_payload(oars: dict, hull: dict, catches_s, t0: float, t1: float, budget: int,
                   fused: dict | None = None, gps=None) -> dict:
    """JSON-ready traces for the sweep, hull and fused-speed charts over [t0, t1] (epoch seconds)."""
    out = {"sweep": _signal_traces(oars, t0, t1, budget) if oars else [],
           "hull": _signal_traces(hull, t0, t1, budget, colors=[HULL_COLOR]) if hull else [],
           "fused": _signal_traces(_kmh(fused), t0, t1, budget, width=1.6, colors=[FUSED_COLOR]) if fused else []}
    out["catch"] = _catch_trace(catches_s, t0, t1, "catch (stroke seat)")
    out["gps"] = _gps_trace(gps, t0, t1) if fused else None
    return out


def track_data(gdf) -> dict | None:
    """GPS lat/lon points + center for a Leaflet polyline."""
    if gdf is None or len(gdf) == 0 or "latitude" not in gdf.columns:
        return None
    pts = [
        [float(la), float(lo)]
        for la, lo in zip(gdf["latitude"], gdf["longitude"])
        if pd.notna(la) and pd.notna(lo)
    ]
    if not pts:
        return None
    center = [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]
    return {"points": pts, "center": center}


# Resolution-adaptive series: pick the aggregation window so that the visible span yields at most
# about `points_per_px` points per pixel, down to raw rows when the span is short enough.
_WINDOW_LADDER_S = (0.02, 0.04, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0)


def pick_window(span_s: float, px: int, points_per_px: int = 4) -> str | None:
    """Flux duration for the bucket, or None for raw rows."""
    px = max(int(px), 50)
    needed = span_s / (points_per_px * px)
    if needed <= 0.02:
        return None
    for w in _WINDOW_LADDER_S:
        if w >= needed:
            return f"{int(round(w * 1000))}ms" if w < 1 else f"{int(w)}s"
    return "300s"


def next_window(window: str | None) -> str:
    """The next coarser bucket on the ladder (raw -> 20ms, 20ms -> 40ms, ...)."""
    cur = 0.0 if window is None else (int(window[:-2]) / 1000 if window.endswith("ms") else float(window[:-1]))
    for w in _WINDOW_LADDER_S:
        if w > cur:
            return f"{int(round(w * 1000))}ms" if w < 1 else f"{int(w)}s"
    return "300s"


def series_payload(imu_df: pd.DataFrame | None, gps_df: pd.DataFrame | None, window: str | None) -> dict:
    """JSON-ready traces for the IMU and speed charts at one resolution."""
    out = {"window_ms": None if window is None else (int(window[:-2]) if window.endswith("ms") else int(window[:-1]) * 1000),
           "imu": [], "speed": None, "n": 0}
    if imu_df is not None and not imu_df.empty and "source" in imu_df.columns:
        d, field = _imu_field(imu_df)
        if field is not None:
            for i, (src, g) in enumerate(d.groupby("source")):
                g = g.sort_values("time")
                out["imu"].append({"name": str(src), "x": _times(g["time"]), "y": _clean(g[field]),
                                   "color": PALETTE[i % len(PALETTE)]})
                out["n"] += len(g)
    if gps_df is not None and not gps_df.empty and "speed" in gps_df.columns:
        g = gps_df.sort_values("time"); sp = pd.to_numeric(g["speed"], errors="coerce") * 3.6
        m = sp.notna() & (sp >= 0)
        if m.any():
            out["speed"] = {"x": _times(g["time"][m]), "y": _clean(sp[m])}; out["n"] += int(m.sum())
    return out


# ─── Kinematics charts (loops, side view, arcs, mean stroke, attitude, feather) ──────────────────
# Inputs are srow.analysis.kinematics objects: kin = {source: OarKinematics} in the engine's rower
# order (so the colours match the sweep chart), hull_att = HullAttitude, flags = {source: set(stroke
# index)} for strokes the engine did not trust (drawn thin red, left out of the mean).

FLAG_COLOR = "#f7768e"
WATER_COLOR = "rgba(42,195,222,0.12)"
MAX_LOOP_STROKES = 400


def _kin_colors(kin: dict) -> dict:
    return {src: PALETTE[i % len(PALETTE)] for i, src in enumerate(kin)}


def _stroke_subset(strokes: list, limit: int = MAX_LOOP_STROKES) -> list[int]:
    n = len(strokes)
    if n <= limit:
        return list(range(n))
    step = n / limit
    return [int(i * step) for i in range(limit)]


def _loop_xy(k, y: str, rig=None):
    """(x, y) per sample for a loop chart: x = sweep angle, y = sweep rate or blade height."""
    from srow.analysis.kinematics import blade_height_cm
    if y == "rate":
        return k.sweep_angle, k.sweep_rate
    if rig is not None:
        return k.sweep_angle, blade_height_cm(k.elev_deg, rig)
    return k.sweep_angle, k.elev_deg


def _mean_loop(k, xs: np.ndarray, ys: np.ndarray, idx: list[int], n: int = 48):
    """The mean stroke as a loop: y averaged in bins of sweep angle, separately for the drive (rate > 0)
    and the recovery, so strokes that differ in timing do not shrink the mean. Returns (x, y, drive)
    with the drive first, catch to finish, then the recovery back, or None."""
    if not idx:
        return None
    sel = np.concatenate([np.arange(k.strokes[i].i0, k.strokes[i].i1) for i in idx])
    x, y, r = xs[sel], ys[sel], k.sweep_rate[sel]
    ok = np.isfinite(x) & np.isfinite(y)
    x, y, r = x[ok], y[ok], r[ok]
    if x.size < 20:
        return None
    lo, hi = np.percentile(x, 2), np.percentile(x, 98)
    edges = np.linspace(lo, hi, n + 1); mid = (edges[:-1] + edges[1:]) / 2
    def half(mask):
        xb, yb = [], []
        which = np.clip(np.digitize(x[mask], edges) - 1, 0, n - 1)
        for b_ in range(n):
            m = which == b_
            if m.sum() >= 3:
                xb.append(mid[b_]); yb.append(float(np.mean(y[mask][m])))
        return np.asarray(xb), np.asarray(yb)
    dx, dy = half(r > 0); rx, ry = half(r <= 0)
    if dx.size < 4 or rx.size < 4:
        return None
    mx = np.concatenate([dx, rx[::-1], dx[:1]]); my = np.concatenate([dy, ry[::-1], dy[:1]])
    drive = np.concatenate([np.ones(dx.size, bool), np.zeros(rx.size, bool), np.ones(1, bool)])
    return mx, my, drive


def loop_fig(kin: dict, flags: dict | None = None, y: str = "rate", rig=None, height: int = 360) -> dict | None:
    """Every stroke of each oar as a faint loop (sweep angle on x), the mean stroke bold, flagged
    strokes thin red. y = "rate" (deg/s) or "height" (cm above the water with rigging, else degrees of
    shaft elevation). One panel per oar, side by side."""
    if not kin:
        return None
    flags = flags or {}
    cols = _kin_colors(kin)
    n_oar = len(kin)
    data, layout = [], {**_BASE_LAYOUT, "height": height, "hovermode": "closest", "showlegend": False,
                       "margin": {"l": 62, "r": 20, "t": 28, "b": 40}, "annotations": [], "shapes": []}
    gap = 0.06
    width = (1.0 - gap * (n_oar - 1)) / n_oar
    ylab = "deg/s" if y == "rate" else ("cm above water" if rig is not None else "shaft elevation (deg)")
    ymin, ymax = np.inf, -np.inf
    for p, (src, k) in enumerate(kin.items()):
        xs, ys = _loop_xy(k, y, rig)
        if y != "rate" and not np.isfinite(ys).any():
            continue
        xa = "x" if p == 0 else f"x{p + 1}"; ya = "y" if p == 0 else f"y{p + 1}"
        good = [i for i in _stroke_subset(k.strokes) if i not in flags.get(src, set())]
        bad = [i for i in _stroke_subset(k.strokes) if i in flags.get(src, set())]
        for group, color, alpha, w in ((good, cols[src], 0.14, 0.8), (bad, FLAG_COLOR, 0.55, 0.8)):
            if not group:
                continue
            gx, gy = [], []
            for i in group:
                s = k.strokes[i]
                gx.extend(_clean(xs[s.i0:s.i1])); gy.extend(_clean(ys[s.i0:s.i1])); gx.append(None); gy.append(None)
            data.append({"type": "scattergl", "mode": "lines", "x": gx, "y": gy, "hoverinfo": "skip",
                         "line": {"width": w, "color": color}, "opacity": alpha, "xaxis": xa, "yaxis": ya,
                         "name": f"{src} {'flagged' if color == FLAG_COLOR else 'strokes'}"})
            fin = np.asarray([v for v in gy if v is not None], float)
            if fin.size:
                ymin, ymax = min(ymin, np.percentile(fin, 1)), max(ymax, np.percentile(fin, 99))
        m = _mean_loop(k, xs, ys, good)
        if m is not None:
            mx, my, drive = m
            if y == "rate":
                data.append({"type": "scatter", "mode": "lines", "x": _clean(mx), "y": _clean(my), "name": f"{src} mean",
                             "line": {"width": 3, "color": cols[src]}, "xaxis": xa, "yaxis": ya})
            else:
                for sel, dash, nm in ((drive, "solid", "drive"), (~drive, "dot", "recovery")):
                    data.append({"type": "scatter", "mode": "lines", "x": _clean(np.where(sel, mx, np.nan)), "y": _clean(np.where(sel, my, np.nan)),
                                 "name": f"{src} mean {nm}", "line": {"width": 3, "color": cols[src], "dash": dash}, "xaxis": xa, "yaxis": ya,
                                 "connectgaps": False})
            i_c, i_f = int(np.argmin(mx)), int(np.argmax(mx))
            data.append({"type": "scatter", "mode": "markers+text", "x": [float(mx[i_c]), float(mx[i_f])], "y": [float(my[i_c]), float(my[i_f])],
                         "text": ["catch", "finish"], "textposition": ["middle left", "middle right"], "hoverinfo": "skip",
                         "marker": {"size": 7, "color": "#c0caf5"}, "textfont": {"size": 11}, "xaxis": xa, "yaxis": ya, "name": "catch/finish"})
        x0, x1 = p * (width + gap), p * (width + gap) + width
        layout["xaxis" if p == 0 else f"xaxis{p + 1}"] = {"domain": [x0, x1], "title": "sweep angle (deg, 0 = mid-arc)", "zeroline": True}
        layout["yaxis" if p == 0 else f"yaxis{p + 1}"] = {"anchor": xa, "title": ylab if p == 0 else "", "zeroline": True,
                                                            "matches": "y" if p > 0 else None}
        layout["annotations"].append({"text": str(src), "x": (x0 + x1) / 2, "y": 1.03, "xref": "paper", "yref": "paper",
                                      "showarrow": False, "font": {"size": 12}})
        if y != "rate" and rig is not None:
            layout["shapes"].append({"type": "rect", "xref": f"{xa} domain" if p else "x domain", "yref": ya, "x0": 0, "x1": 1,
                                     "y0": -200, "y1": 0, "fillcolor": WATER_COLOR, "line": {"width": 0}, "layer": "below"})
    if not data:
        return None
    if np.isfinite(ymin) and np.isfinite(ymax):
        pad = 0.08 * (ymax - ymin)
        for key in [k_ for k_ in layout if k_.startswith("yaxis")]:
            layout[key]["range"] = [float(ymin - pad), float(ymax + pad)]
    for key in [k_ for k_ in layout if k_.startswith("yaxis")]:
        layout[key] = {k2: v for k2, v in layout[key].items() if v is not None}
    return {"data": data, "layout": layout}


def arc_fig(kin: dict, height: int | None = None) -> dict | None:
    """XBoat's top strip: per oar, where the catch and the finish fall stroke by stroke (10-90 %,
    quartiles, median) and the total arc. Zero is the mid-arc."""
    if not kin:
        return None
    cols = _kin_colors(kin)
    rows = []
    for src, k in kin.items():
        if len(k.strokes) < 3:
            continue
        c = np.array([s.catch_deg for s in k.strokes]); f = np.array([s.finish_deg for s in k.strokes])
        rows.append((src, c, f))
    if not rows:
        return None
    data, shapes, ann = [], [], []
    for r, (src, c, f) in enumerate(reversed(rows)):
        y = r
        for arr in (c, f):
            p10, p25, p50, p75, p90 = np.percentile(arr, [10, 25, 50, 75, 90])
            shapes.append({"type": "rect", "x0": float(p10), "x1": float(p90), "y0": y - 0.3, "y1": y + 0.3, "fillcolor": cols[src], "opacity": 0.25, "line": {"width": 0}})
            shapes.append({"type": "rect", "x0": float(p25), "x1": float(p75), "y0": y - 0.3, "y1": y + 0.3, "fillcolor": cols[src], "opacity": 0.6, "line": {"width": 0}})
            shapes.append({"type": "line", "x0": float(p50), "x1": float(p50), "y0": y - 0.36, "y1": y + 0.36, "line": {"width": 2, "color": "#c0caf5"}})
        shapes.append({"type": "rect", "x0": float(np.median(c)), "x1": float(np.median(f)), "y0": y - 0.12, "y1": y + 0.12, "fillcolor": cols[src], "opacity": 0.35, "line": {"width": 0}})
        ann.append({"x": 1.0, "xref": "paper", "xanchor": "left", "y": y, "yref": "y", "showarrow": False, "xshift": 8,
                    "text": f"{np.median(c):.0f}° → {np.median(f):.0f}°  ·  arc {np.median(f) - np.median(c):.0f}°", "font": {"size": 12}})
        data.append({"type": "scatter", "mode": "markers", "x": _clean(np.concatenate([c, f])), "y": [y] * (c.size + f.size), "name": str(src),
                     "marker": {"size": 3, "color": cols[src], "opacity": 0.0}, "hoverinfo": "x+name"})
    layout = {**_BASE_LAYOUT, "height": height or (70 + 50 * len(rows)), "showlegend": False, "hovermode": "closest",
              "margin": {"l": 62, "r": 170, "t": 10, "b": 40}, "shapes": shapes, "annotations": ann,
              "xaxis": {"title": "catch and finish angle per stroke (deg, 0 = mid-arc): 10–90 %, quartiles, median", "zeroline": True},
              "yaxis": {"tickvals": list(range(len(rows))), "ticktext": [str(src) for src, _, _ in reversed(rows)], "range": [-0.6, len(rows) - 0.4], "fixedrange": True}}
    return {"data": data, "layout": layout}


def kin_summary(kin: dict) -> list[dict]:
    """Per-oar headline numbers for the arc chart."""
    out = []
    for src, k in kin.items():
        if not k.strokes:
            continue
        arcs = [s.arc_deg for s in k.strokes]
        sq = [s.square_before_ms for s in k.strokes if s.square_before_ms is not None]
        ft = [s.feather_after_ms for s in k.strokes if s.feather_after_ms is not None]
        out.append({"seat": str(src), "n": len(k.strokes), "arc": f"{np.median(arcs):.0f}°",
                    "square": f"{np.median(sq):.0f} ms before the catch" if sq else "–",
                    "feather": f"{np.median(ft):.0f} ms after the release" if ft else "–",
                    "drive_sign": k.quality.get("drive_sign", "assumed"),
                    "inverted": bool(k.quality.get("engine_sign_inverted", False))})
    return out


def _fold(grid: np.ndarray, y: np.ndarray, catches: np.ndarray, tt: np.ndarray, period_ok=(1.2, 5.0)) -> np.ndarray:
    rows = []
    for a, b in zip(catches[:-1], catches[1:]):
        if period_ok[0] < b - a < period_ok[1] and a + tt[0] >= grid[0] and a + tt[-1] <= grid[-1]:
            rows.append(np.interp(a + tt, grid, y))
    return np.asarray(rows)


def stroke_time_fig(kin: dict, hull: dict | None, ref: str | None, height: int = 340) -> dict | None:
    """The mean stroke in time, aligned on the reference oar's catches: hull acceleration (mean ± SD)
    and hull speed change on the left axis, each oar's sweep rate on the right, with catch, minimum
    acceleration, minimum speed and release marked. XBoat's "force and hull acceleration vs time",
    with sweep rate in place of force."""
    if not kin:
        return None
    ref = ref if ref in kin else next(iter(kin))
    kr = kin[ref]
    catches = np.array([s.t0 for s in kr.strokes], float)
    if catches.size < 4:
        return None
    T = float(np.median(np.diff(catches)))
    tt = np.linspace(-0.3, T + 0.2, int((T + 0.5) * 50))
    cols = _kin_colors(kin)
    data, shapes, ann = [], [], []
    hm = None; hull_flipped = False
    if hull:
        g, a = next(iter(hull.values()))
        H = _fold(np.asarray(g, float), np.asarray(a, float), catches, tt)
        if H.shape[0] >= 3:
            hm, hs = H.mean(axis=0), H.std(axis=0)
            # hull_surge's sign test assumes the drive push outweighs the catch check, which is not so
            # for a scull; physically the sharp minimum (the check) comes at the catch and the push
            # later in the drive, so if the mean's maximum precedes its minimum the sign is flipped here
            win = (tt >= 0) & (tt <= T)
            if win.any() and np.argmax(np.where(win, hm, -np.inf)) < np.argmin(np.where(win, hm, np.inf)):
                hm = -hm; hull_flipped = True
            data.append({"type": "scatter", "mode": "lines", "x": _clean(tt), "y": _clean(hm + hs), "line": {"width": 0}, "hoverinfo": "skip", "showlegend": False})
            data.append({"type": "scatter", "mode": "lines", "x": _clean(tt), "y": _clean(hm - hs), "line": {"width": 0}, "fill": "tonexty",
                         "fillcolor": "rgba(158,206,106,0.18)", "hoverinfo": "skip", "name": "hull ± SD"})
            data.append({"type": "scatter", "mode": "lines", "x": _clean(tt), "y": _clean(hm), "name": "hull acceleration (m/s²)", "line": {"width": 2.4, "color": HULL_COLOR}})
            v = np.concatenate([[0.0], np.cumsum((hm[1:] + hm[:-1]) / 2) * (tt[1] - tt[0])]); v -= v.mean()
            data.append({"type": "scatter", "mode": "lines", "x": _clean(tt), "y": _clean(v * 5), "name": "hull speed change (m/s × 5)",
                         "line": {"width": 1.4, "color": "#73daca", "dash": "dot"}})
            i_min = int(np.argmin(np.where(tt > 0.05, hm, np.inf))); i_v = int(np.argmin(np.where(tt > 0.05, v, np.inf)))
            shapes += [{"type": "line", "x0": float(tt[i_min]), "x1": float(tt[i_min]), "y0": 0, "y1": 1, "yref": "paper", "line": {"width": 1, "dash": "dash", "color": "#f7768e"}},
                       {"type": "line", "x0": float(tt[i_v]), "x1": float(tt[i_v]), "y0": 0, "y1": 1, "yref": "paper", "line": {"width": 1, "dash": "dash", "color": "#9ece6a"}}]
            ann += [{"x": float(tt[i_min]), "y": 1.0, "yref": "paper", "text": "min accel", "showarrow": False, "textangle": -90, "xanchor": "right", "yanchor": "top", "font": {"size": 10, "color": "#f7768e"}},
                    {"x": float(tt[i_v]), "y": 1.0, "yref": "paper", "text": "min speed", "showarrow": False, "textangle": -90, "xanchor": "right", "yanchor": "top", "font": {"size": 10, "color": "#9ece6a"}}]
    rel = None
    for src, k in kin.items():
        Rm = _fold(k.grid, k.sweep_rate, catches, tt)
        if Rm.shape[0] < 3:
            continue
        rm = Rm.mean(axis=0)
        data.append({"type": "scatter", "mode": "lines", "x": _clean(tt), "y": _clean(rm), "name": f"{src} sweep rate (deg/s)",
                     "line": {"width": 2.2, "color": cols[src]}, "yaxis": "y2"})
        if src == ref:
            c = np.where((rm[:-1] > 0) & (rm[1:] <= 0) & (tt[:-1] > 0.3))[0]
            if c.size:
                rel = float(tt[c[0]])
    if len(data) == 0:
        return None
    shapes.append({"type": "line", "x0": 0, "x1": 0, "y0": 0, "y1": 1, "yref": "paper", "line": {"width": 1, "dash": "dash", "color": "#c0caf5"}})
    ann.append({"x": 0, "y": 1.0, "yref": "paper", "text": "catch", "showarrow": False, "textangle": -90, "xanchor": "right", "yanchor": "top", "font": {"size": 10}})
    if rel is not None:
        shapes.append({"type": "line", "x0": rel, "x1": rel, "y0": 0, "y1": 1, "yref": "paper", "line": {"width": 1, "dash": "dash", "color": "#c0caf5"}})
        ann.append({"x": rel, "y": 1.0, "yref": "paper", "text": "release", "showarrow": False, "textangle": -90, "xanchor": "right", "yanchor": "top", "font": {"size": 10}})
    shapes.append({"type": "line", "x0": T, "x1": T, "y0": 0, "y1": 1, "yref": "paper", "line": {"width": 1, "color": "#565f89"}})
    layout = {**_BASE_LAYOUT, "height": height, "hovermode": "x", "margin": {"l": 62, "r": 62, "t": 10, "b": 40}, "shapes": shapes, "annotations": ann,
              "xaxis": {"title": f"time from the {ref} catch (s); mean of {catches.size - 1} strokes" + ("; hull sign set by the catch check" if hull_flipped else ""),
                        "range": [float(tt[0]), float(tt[-1])], "zeroline": False},
              "yaxis": {"title": "m/s²", "zeroline": True},
              "yaxis2": {"title": "deg/s", "overlaying": "y", "side": "right", "zeroline": False, "showgrid": False}}
    return {"data": data, "layout": layout}


ATTITUDE_COLORS = ["#bb9af7", "#e0af68"]


def attitude_traces(att, t0: float, t1: float, budget: int) -> list[dict]:
    """Hull roll and pitch traces over [t0, t1] for the attitude chart (full chart and refine payload)."""
    if att is None:
        return []
    series = {"roll (deg)": (att.grid, att.roll_deg), "pitch (deg)": (att.grid, att.pitch_deg)}
    return _signal_traces(series, t0, t1, budget, width=1.4, colors=ATTITUDE_COLORS)


def attitude_fig(att, catches_s, xrange=None, budget: int = 3200) -> dict | None:
    """The hull unit's roll (set) and pitch over the piece, mounting offset removed, with the catches."""
    if att is None:
        return None
    t0, t1 = _span(xrange, {"a": (att.grid, att.roll_deg)})
    data = attitude_traces(att, t0, t1, budget)
    if not data:
        return None
    c = _catch_trace(catches_s, t0, t1, "catch (stroke seat)")
    if c:
        data.append(c)
    layout = {**_BASE_LAYOUT, "height": 240, "xaxis": _xaxis("time", xrange),
              "yaxis": {"title": "deg", "fixedrange": True, "autorange": True, "zeroline": True}}
    return {"data": data, "layout": layout}


def feather_fig(kin: dict, xrange=None) -> dict | None:
    """Per stroke and oar: how long before the catch the blade was square, and how long after the
    release it was feathered (ms). One point per stroke on the piece's time axis."""
    if not kin:
        return None
    import datetime as _dt
    cols = _kin_colors(kin)
    data = []
    for src, k in kin.items():
        for attr, sym, nm in (("square_before_ms", "circle", "squared before the catch"), ("feather_after_ms", "diamond-open", "feathered after the release")):
            xs = [_dt.datetime.fromtimestamp(s.t0, tz=_dt.timezone.utc).isoformat() for s in k.strokes if getattr(s, attr) is not None]
            ys = [round(getattr(s, attr)) for s in k.strokes if getattr(s, attr) is not None]
            if xs:
                data.append({"type": "scatter", "mode": "markers", "name": f"{src}: {nm}", "x": xs, "y": ys,
                             "marker": {"size": 5, "color": cols[src], "symbol": sym}})
    if not data:
        return None
    layout = {**_BASE_LAYOUT, "height": 260, "xaxis": _xaxis("time (one point per stroke)", xrange),
              "yaxis": {"title": "ms", "fixedrange": True, "autorange": True, "zeroline": True}}
    return {"data": data, "layout": layout}


def gps_stats(gdf) -> dict | None:
    """Distance rowed and average split from the GPS track (usable fixes only)."""
    from srow.analysis.kinematics import gps_distance, format_split
    if gdf is None or len(gdf) == 0 or "latitude" not in gdf.columns:
        return None
    t = gdf["time"] if "time" in gdf.columns else gdf.index
    gt = np.array([pd.Timestamp(x).timestamp() for x in t], float)
    acc = pd.to_numeric(gdf["accuracy"], errors="coerce").to_numpy(float) if "accuracy" in gdf.columns else None
    d = gps_distance(gt, pd.to_numeric(gdf["latitude"], errors="coerce").to_numpy(float),
                     pd.to_numeric(gdf["longitude"], errors="coerce").to_numpy(float), acc)
    if d["distance_m"] <= 0:
        return None
    d["distance_text"] = f"{d['distance_m'] / 1000:.2f} km" if d["distance_m"] >= 1000 else f"{d['distance_m']:.0f} m"
    d["split_text"] = format_split(d["split_s_per_500"])
    return d
