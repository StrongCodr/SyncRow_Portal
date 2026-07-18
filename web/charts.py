"""Chart/figure builders for the web UI.

Pure functions: DataFrame -> Plotly figure spec (plain dict) or map data.
Backgrounds are transparent so the client theme shows through; the browser
sets colors on render (see restyleCharts in base.html). Reuses the sync-metric
logic from the retired Panel app.
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


def sync_fig(df: pd.DataFrame | None):
    """Synchronicity over time: z-score each sensor, spread across sensors, 1/(1+spread)."""
    if df is None or df.empty or "source" not in df.columns:
        return None
    d, field = _imu_field(df)
    if field is None:
        return None
    pivot = d.pivot_table(index="time", columns="source", values=field, aggfunc="first")
    if pivot.shape[1] < 2:  # need >= 2 sensors to have a sync metric
        return None
    normalized = pivot.apply(lambda x: (x - x.mean()) / x.std() if x.std() > 0 else x * 0)
    spread = normalized.std(axis=1)
    score = 1.0 / (1.0 + spread)
    data = [{
        "type": "scatter", "mode": "lines", "name": "sync",
        "x": _times(score.index), "y": _clean(score.values),
        "line": {"width": 1.6, "color": "#9ece6a"},
        "fill": "tozeroy", "fillcolor": "rgba(158,206,106,0.12)",
    }]
    layout = {**_BASE_LAYOUT, "height": 240, "showlegend": False,
              "xaxis": {"title": "time"},
              "yaxis": {"title": "sync (1 = perfect)", "range": [0, 1]}}
    return {"data": data, "layout": layout}


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
