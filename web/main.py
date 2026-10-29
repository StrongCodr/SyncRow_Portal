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
import numpy as np
import pandas as pd
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
from srow.analysis.hull import hull_surge, fused_speed
from web import charts
from functools import lru_cache

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


@lru_cache(maxsize=6)
def _analysis(tag: str, value: str):
    """Run the analysis engine on RAW data for this interval, once per process (the raw load, PCA and
    x-corr are heavy; the result is a few MB). Returns a bundle, or None on failure so the page still
    renders:
      cross:   CrossResult (per-seat ms offsets)
      oars:    {source: (epoch_s, deg/s)} the rowing units' sweep angular velocity, drive positive
      hull:    {source: (epoch_s, m/s²)} fore-aft acceleration of every non-rowing unit (the hull)
      fused:   {source: (epoch_s, m/s)} boat speed, GPS refined by that unit's surge; NaN where the
               GPS is missing or unsteady (see hull.fused_speed)
      gps:     (epoch_s, m/s) the good GPS fixes on the hull unit's clock (lag removed), or None
      catches: [epoch_s] the stroke seat's catches
    Window: ±2 days around the interval's name epoch (name/data clocks differ ~9h)."""
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
        an = analyze_interval(by_source)
        cross = an.cross
        oars = {s: (an.frames[s].times_s, an.frames[s].signal * an.frames[s].sweep_energy)
                for s in cross.rowers if s in an.frames}
        catches = [c.time_s for c in an.catches.get(cross.reference, [])] if cross.reference else []
        hull = {}
        for s, d in by_source.items():
            if s in cross.rowers:
                continue
            g, a = hull_surge(d["times_s"], d["accel"], catches)
            if g is not None:
                hull[s] = (g, a)
        fused, gps = {}, None
        gt, gv, ga = _gps_arrays(tag, value)
        if gt is not None and gt.size:
            for s, (g, a) in hull.items():
                _, v, wins, lag = fused_speed(g, a, gt, gv, ga)
                if wins and np.isfinite(v).any():
                    fused[s] = (g, v)
                    if gps is None:
                        ok = np.isfinite(gv) & ((ga <= 30.0) if ga is not None else True)
                        gps = (gt[ok] - lag, gv[ok])
        return {"cross": cross, "oars": oars, "hull": hull, "fused": fused, "gps": gps, "catches": catches}
    except Exception:
        return None


def _gps_arrays(tag: str, value: str):
    """(epoch_s, speed m/s, accuracy m or None) of the interval's phone fixes, or (None, None, None)."""
    try:
        gdf = location.load_track(tag_name=tag, interval_value=value)
    except Exception:
        return None, None, None
    if gdf is None or len(gdf) == 0 or "speed" not in gdf.columns:
        return None, None, None
    t = gdf["time"] if "time" in gdf.columns else gdf.index
    gt = np.array([pd.Timestamp(x).timestamp() for x in t], float)
    gv = pd.to_numeric(gdf["speed"], errors="coerce").to_numpy(float)
    ga = pd.to_numeric(gdf["accuracy"], errors="coerce").to_numpy(float) if "accuracy" in gdf.columns else None
    return gt, gv, ga


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
    bundle = _analysis(tag, value) or {}
    cross = bundle.get("cross")
    xrange = charts.shared_range(cross, df, gdf)   # every chart opens on the same span

    return templates.TemplateResponse(
        request=request, name="chart.html",
        context={
            "tag": tag,
            "value": value,
            "n": len(df) if df is not None else 0,
            "offsets_json": json.dumps(charts.offsets_fig(cross, xrange)),
            "offsets_summary": charts.offsets_summary(cross),
            "offsets_boat": charts.offsets_boat(cross),
            "speed_json": json.dumps(charts.speed_fig(gdf, xrange)),
            "imu_json": json.dumps(charts.imu_fig(df, xrange)),
            "sweep_json": json.dumps(charts.sweep_fig(bundle.get("oars"), bundle.get("catches"), xrange)),
            "hull_json": json.dumps(charts.hull_fig(bundle.get("hull"), bundle.get("catches"), xrange)),
            "fused_json": json.dumps(charts.fused_fig(bundle.get("fused"), bundle.get("gps"), xrange)),
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
    b = _analysis(tag, value) or {}
    payload.update(charts.signal_payload(b.get("oars"), b.get("hull"), b.get("catches"), start.timestamp(), stop.timestamp(), budget,
                                         fused=b.get("fused"), gps=b.get("gps")))
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
