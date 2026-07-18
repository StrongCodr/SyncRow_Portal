"""SyncRow Web — FastAPI + HTMX + Tailwind + Plotly frontend.

A lightweight, client-side-interactive UI. Reuses the srow data layer
(InfluxService); the server returns HTML fragments and Plotly figure specs
(plain JSON) — the browser handles zoom/pan/theme with no server round-trip.

Auth is a session cookie set by a real login page (no browser basic-auth popup).

Run:
    uvicorn web.main:app --reload            # dev
    uvicorn web.main:app --host 127.0.0.1 --port 5006   # prod (behind nginx TLS)
"""

import json
import os
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from srow.config import load_settings
from srow.services import InfluxService

BASE_DIR = Path(__file__).parent
settings = load_settings()
influx = InfluxService(settings)

# Session secret: reuse COOKIE_SECRET from the env if present.
SESSION_SECRET = (
    os.getenv("SESSION_SECRET")
    or os.getenv("COOKIE_SECRET")
    or "dev-insecure-secret-change-me"
)


def _load_users() -> dict[str, str]:
    """Users from SROW_USERS env ('user:pass,user2:pass2'). Dev fallback: admin/admin."""
    users: dict[str, str] = {}
    for pair in os.getenv("SROW_USERS", "").split(","):
        pair = pair.strip()
        if ":" in pair:
            u, p = pair.split(":", 1)
            users[u.strip()] = p
    return users or {"admin": "admin"}


USERS = _load_users()

app = FastAPI(title="SyncRow Web")
app.add_middleware(
    SessionMiddleware, secret_key=SESSION_SECRET, https_only=True, same_site="lax"
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.cache = None  # jinja2 LRU cache trips on Python 3.14; harmless to disable

PALETTE = [
    "#7aa2f7", "#bb9af7", "#7dcfff", "#9ece6a",
    "#e0af68", "#f7768e", "#2ac3de", "#ff9e64",
]


def _user(request: Request) -> str | None:
    return request.session.get("user")


# ─── Auth ────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if USERS.get(username) == password:
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"error": "Invalid username or password"}, status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ─── App ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    try:
        intervals = influx.fetch_interval_tags()
        error = None
    except Exception as e:  # pragma: no cover
        intervals, error = [], str(e)
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"intervals": intervals, "error": error, "user": _user(request)},
    )


def _build_fig(df):
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
            "type": "scattergl", "mode": "lines", "name": str(src),
            "x": [t.isoformat() for t in g["time"]],
            "y": [None if v != v else float(v) for v in g[field]],
            "line": {"width": 1.3, "color": PALETTE[i % len(PALETTE)]},
        })
    layout = {
        "margin": {"l": 55, "r": 20, "t": 10, "b": 40},
        "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
        "xaxis": {"title": "time"}, "yaxis": {"title": field},
        "legend": {"orientation": "h", "y": 1.12}, "hovermode": "x unified",
        "height": 460,
    }
    return {"data": data, "layout": layout}, field


@app.get("/interval", response_class=HTMLResponse)
def interval(request: Request, tag: str, value: str):
    if not _user(request):
        resp = HTMLResponse("", status_code=401)
        resp.headers["HX-Redirect"] = "/login"  # tell HTMX to bounce to login
        return resp
    try:
        df = influx.load_interval_aggregated(tag_name=tag, interval_value=value, window="200ms")
    except Exception as e:  # pragma: no cover
        return HTMLResponse(f'<div class="text-red-400 text-sm">Query failed: {e}</div>')

    fig, field = _build_fig(df)
    if fig is None:
        return HTMLResponse('<div class="text-subtle">No plottable data for this interval.</div>')

    return templates.TemplateResponse(
        request=request, name="chart.html",
        context={"fig_json": json.dumps(fig), "value": value, "n": len(df), "field": field},
    )


# ─── Niceties: retire the old Panel path, quiet the favicon 404 ──────────────

@app.get("/app")
def old_panel_path():
    return RedirectResponse("/", status_code=301)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)
