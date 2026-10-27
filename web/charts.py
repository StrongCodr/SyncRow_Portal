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
