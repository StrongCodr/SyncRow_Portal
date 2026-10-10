"""SyncRow Web — FastAPI + HTMX + Tailwind + Plotly frontend.

A lightweight, client-side-interactive UI. Reuses the srow data layer
(InfluxService); the server returns HTML fragments and Plotly figure specs
(plain JSON) — the browser handles zoom/pan/theme with no server round-trip.

Auth is a session cookie set by a real login page (no browser basic-auth popup).

Run:
    uvicorn web.main:app --reload            # dev
    uvicorn web.main:app --host 127.0.0.1 --port 5006   # prod (behind nginx TLS)
"""

import datetime as dt
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from srow.config import load_settings
from srow.services import InfluxService, LocationService
from srow.analysis.load import load_raw_by_source
from srow.analysis.engine import analyze_interval
from web import charts

BASE_DIR = Path(__file__).parent
settings = load_settings()
influx = InfluxService(settings)
location = LocationService(settings)

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
    SessionMiddleware, secret_key=SESSION_SECRET, https_only=os.getenv("SROW_INSECURE_COOKIES") != "1", same_site="lax"
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.cache = None  # jinja2 LRU cache trips on Python 3.14; harmless to disable


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


def _analyze_offsets(tag: str, value: str):
    """Run the real analysis engine on RAW data for this interval and return its
    CrossResult (per-seat ms offsets). Heavy (streaming raw load + PCA + x-corr);
    on-demand for now, TODO move to the precompute worker. Returns None on failure
    so the rest of the page still renders. Window: ±2 days around the interval's
    name epoch (name/data clocks differ ~9h; a tight window misses the data)."""
    m = re.search(r"(\d{13})", value)
    if not m:
        return None
    anchor = dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt.timezone.utc)
    try:
        by_source = load_raw_by_source(
            settings, tag, value,
            anchor - dt.timedelta(days=2), anchor + dt.timedelta(days=2),
        )
        if not by_source:
            return None
        return analyze_interval(by_source).cross
    except Exception:
        return None


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

    try:
        gdf = location.load_track(tag_name=tag, interval_value=value)
    except Exception:
        gdf = None

    if (df is None or df.empty) and (gdf is None or len(gdf) == 0):
        return HTMLResponse('<div class="text-subtle">No data for this interval.</div>')

    # Real timing offsets (ms) from the raw-resolution engine — replaces the old
    # unitless synchronicity score.
    cross = _analyze_offsets(tag, value)

    return templates.TemplateResponse(
        request=request, name="chart.html",
        context={
            "tag": tag,
            "value": value,
            "n": len(df) if df is not None else 0,
            "offsets_json": json.dumps(charts.offsets_fig(cross)),
            "offsets_summary": charts.offsets_summary(cross),
            "offsets_boat": charts.offsets_boat(cross),
            "speed_json": json.dumps(charts.speed_fig(gdf)),
            "imu_json": json.dumps(charts.imu_fig(df)),
            "track_json": json.dumps(charts.track_data(gdf)),
        },
    )


def _series(tag: str, value: str, start: dt.datetime, stop: dt.datetime, px: int) -> dict:
    """IMU + speed traces for [start, stop] at a resolution of at most ~4 points per pixel
    (raw rows once the span is short enough). Shared by the endpoint and tests."""
    span = max((stop - start).total_seconds(), 0.001); budget = 4 * max(int(px), 50)
    window = charts.pick_window(span, px)
    imu_df = influx.load_series(tag, value, start, stop, window, measurement="imu", fields=("wx", "wy", "wz"))
    if imu_df is not None and not imu_df.empty and len(imu_df) > budget * 1.1:
        # the budget is for all sensors together: coarsen so that n_sensors * span / window fits
        n_src = max(imu_df["source"].nunique(), 1)
        coarser = charts.pick_window(span * n_src, px)
        if coarser == window:
            coarser = charts.next_window(window)
        if coarser != window:
            window = coarser
            imu_df = influx.load_series(tag, value, start, stop, window, measurement="imu", fields=("wx", "wy", "wz"))
    gps_window = None if (window is None or window.endswith("ms")) else window   # GPS is 1 Hz: sub-second buckets = raw
    gps_df = influx.load_series(tag, value, start, stop, gps_window, measurement="phone_location", fields=("speed",))
    payload = charts.series_payload(imu_df, gps_df, window)
    payload["start"], payload["stop"], payload["px"] = start.isoformat(), stop.isoformat(), px
    return payload


@app.get("/interval/series")
def interval_series(request: Request, tag: str, value: str, start: str, stop: str, px: int = 800):
    """Resolution-adaptive data for the IMU and speed charts (see _series). Times are RFC 3339 / ISO."""
    if not _user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        t0 = dt.datetime.fromisoformat(start.replace("Z", "+00:00")); t1 = dt.datetime.fromisoformat(stop.replace("Z", "+00:00"))
        t0 = t0.astimezone(dt.timezone.utc) if t0.tzinfo else t0.replace(tzinfo=dt.timezone.utc)
        t1 = t1.astimezone(dt.timezone.utc) if t1.tzinfo else t1.replace(tzinfo=dt.timezone.utc)
        if t1 <= t0:
            return JSONResponse({"error": "empty range"}, status_code=400)
        return JSONResponse(_series(tag, value, t0, t1, px))
    except Exception as e:  # pragma: no cover
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Redirects & favicon ─────────────────────────────────────────────────────

@app.get("/app")
def app_redirect():
    return RedirectResponse("/", status_code=301)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)
