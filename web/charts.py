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
    "margin": {"l": 55, "r": 20, "t": 10, "b": 40},
    "hovermode": "x unified",
    "legend": {"orientation": "h", "y": 1.15},
}


def _times(idx) -> list[str]:
    return [t.isoformat() for t in idx]


def _clean(vals) -> list:
    return [None if v != v else float(v) for v in vals]


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


def imu_fig(df: pd.DataFrame | None):
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
              "xaxis": {"title": "time"}, "yaxis": {"title": field}}
    return {"data": data, "layout": layout}


def offsets_fig(cross):
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
        xs, ys = [], []
        for st in piece:
            if st.seat_status.get(s) == "ok" and s in st.offsets_ms:
                xs.append(_ts(st.stroke_time_s))
                ys.append(round(st.offsets_ms[s], 1))
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
              "xaxis": {"title": "time"},
              "yaxis": {"title": "ms behind (+) / ahead (−) vs stroke", "zeroline": True}}
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


def speed_fig(gdf) -> dict | None:
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
              "xaxis": {"title": "time"}, "yaxis": {"title": "km/h"}}
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
