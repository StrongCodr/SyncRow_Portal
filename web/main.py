"""SyncRow Web — FastAPI + HTMX + Tailwind + Plotly frontend.

A lightweight, client-side-interactive alternative to the Panel app. Reuses the
existing srow data layer (InfluxService); the server returns HTML fragments and
Plotly figure specs (plain JSON) — the browser handles zoom/pan/theme with no
server round-trip.

Run:
    uvicorn web.main:app --reload            # dev
    uvicorn web.main:app --host 127.0.0.1 --port 5006   # prod (behind nginx)
"""

import json
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from srow.config import load_settings
from srow.services import InfluxService

BASE_DIR = Path(__file__).parent
settings = load_settings()
influx = InfluxService(settings)

app = FastAPI(title="SyncRow Web")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# jinja2's LRU cache trips on Python 3.14; disabling it is harmless (few templates).
templates.env.cache = None

# Line colors (Tokyo-ish accents; readable on every theme)
PALETTE = [
    "#7aa2f7", "#bb9af7", "#7dcfff", "#9ece6a",
    "#e0af68", "#f7768e", "#2ac3de", "#ff9e64",
]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    try:
        intervals = influx.fetch_interval_tags()
        error = None
    except Exception as e:  # pragma: no cover - surfaced in UI
        intervals, error = [], str(e)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"intervals": intervals, "error": error},
    )


def _build_fig(df):
    """Build a Plotly figure spec (plain dict) from an interval DataFrame.

    Plots a rotation-invariant gyro magnitude per sensor when available,
    else falls back to a raw orientation channel. Backgrounds are transparent
    so the client theme shows through; the browser sets colors on render.
    """
    if df is None or df.empty:
        return None, None

    field = None
    if all(c in df.columns for c in ("wx", "wy", "wz")):
        df = df.copy()
        df["gyro_mag"] = np.sqrt(df["wx"] ** 2 + df["wy"] ** 2 + df["wz"] ** 2)
        field = "gyro_mag"
    else:
        for c in ("pitch", "roll", "yaw", "az"):
            if c in df.columns:
                field = c
                break
    if field is None:
        return None, None

    data = []
    for i, (src, g) in enumerate(df.groupby("source")):
        g = g.sort_values("time")
        data.append({
            "type": "scattergl",  # WebGL — handles 100k+ points client-side
            "mode": "lines",
            "name": str(src),
            "x": [t.isoformat() for t in g["time"]],
            "y": [None if v != v else float(v) for v in g[field]],
            "line": {"width": 1.3, "color": PALETTE[i % len(PALETTE)]},
        })

    layout = {
        "margin": {"l": 55, "r": 20, "t": 10, "b": 40},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "xaxis": {"title": "time"},
        "yaxis": {"title": field},
        "legend": {"orientation": "h", "y": 1.12},
        "hovermode": "x unified",
        "height": 460,
    }
    return {"data": data, "layout": layout}, field


@app.get("/interval", response_class=HTMLResponse)
def interval(request: Request, tag: str, value: str):
    try:
        df = influx.load_interval_aggregated(
            tag_name=tag, interval_value=value, window="200ms",
        )
    except Exception as e:  # pragma: no cover
        return HTMLResponse(f'<div class="text-red-400 text-sm">Query failed: {e}</div>')

    fig, field = _build_fig(df)
    if fig is None:
        return HTMLResponse('<div class="text-subtle">No plottable data for this interval.</div>')

    return templates.TemplateResponse(
        request=request,
        name="chart.html",
        context={
            "fig_json": json.dumps(fig),
            "value": value,
            "n": len(df),
            "field": field,
        },
    )
