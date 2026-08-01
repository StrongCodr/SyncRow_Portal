# SyncRow Portal Architecture

This document describes the system design of the SyncRow Portal.

## Overview

The portal is a FastAPI web application for visualizing rowing sensor data,
plus an offline analyzer for quantifying crew asynchronicity. Charts are
rendered client-side with Plotly from JSON figure specs built on the server;
HTMX swaps HTML fragments so there is no SPA framework.

## System Architecture

```
                        Browser
        (HTMX partial swaps + Plotly client-side)
                           │ HTTPS (nginx, TLS)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                 FastAPI app (web/main.py)                   │
│  session-cookie auth · /login · / (intervals) · /interval   │
│                                                             │
│  web/charts.py: DataFrame → Plotly figure-spec dicts        │
│   - imu_fig    (per-sensor traces)                          │
│   - sync_fig   (z-score spread → 1/(1+spread) score)        │
│   - speed_fig  (GPS speed)                                  │
│   - map figure (GPS track)                                  │
└─────────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────────┐
│              Data Service Layer (srow/services/)            │
├──────────────────┬──────────────────┬───────────────────────┤
│  InfluxService   │ LocationService  │  CacheService         │
│  - interval tags │  - load_track    │  - Parquet local cache│
│  - load interval │  - distance      │    (used by the       │
│    (aggregated)  │  - speed stats   │     offline analyzer) │
└──────────────────┴──────────────────┴───────────────────────┘
                           │
                       InfluxDB
                (imu + phone_location)
```

## Component Responsibilities

### Config Layer (`srow/config/`)

- **settings.py**: Defines the `Settings` dataclass that holds InfluxDB
  connection parameters. Immutable (frozen dataclass).
- **env_utils.py**: Loads `.env` files to populate environment variables.

### Service Layer (`srow/services/`)

- **InfluxService**: Handles all IMU data queries. Key methods:
  - `fetch_interval_tags()`: Lists available intervals
  - `load_interval()`: Loads all data for an interval
  - `load_interval_aggregated()`: Loads pre-aggregated data (faster)
  - `get_time_bounds()`: Gets min/max time without loading all data
  - `unwrap_angles()`: Fixes angle discontinuities
  - `summarize_interval()`: Calculates statistics per source

- **LocationService**: Handles GPS data queries. Key methods:
  - `load_track()`: Loads GPS points as GeoDataFrame
  - `load_track_simplified()`: Simplified track for overview
  - `calculate_distance()`: Total distance in meters
  - `summarize_track()`: Track statistics

- **CacheService**: Local Parquet cache of raw interval data. Primarily used
  by `analyze_async.py` for resumable bulk downloads.

### Web Layer (`web/`)

- **main.py**: FastAPI routes and session-cookie auth. `GET /` lists
  intervals; `GET /interval` returns an HTML fragment embedding Plotly figure
  JSON for the selected interval. Users come from `SROW_USERS` env
  (dev fallback `admin`/`admin`).
- **charts.py**: Pure functions, `DataFrame → Plotly figure-spec dict`.
  No web-framework imports — testable in isolation.
- **templates/**: Jinja2 — `base.html` (theme, Plotly setup, HTMX),
  `index.html` (interval list), `chart.html` (chart fragment), `login.html`.

### Offline Analyzer (`analyze_async.py`)

Standalone research CLI (not part of the web app): downloads all intervals
into the Parquet cache, detects per-rower catch events (median-crossing,
sub-sample timing), matches catches across sensors into per-stroke
asynchronicity (ms), segments stable-SPM pieces, and compares empirical speed
loss to a linear model. Exports `async_analysis_results.csv`.

## Data Flow — loading an interval

```
User clicks an interval (HTMX GET /interval?...)
        │
        ▼
web/main.py: influx_service.load_interval_aggregated()
        │            (InfluxDB Flux query, ~200 ms windows)
        ▼
web/charts.py: build imu/sync/speed/map figure specs (JSON)
        │
        ▼
chart.html fragment rendered → HTMX swaps it in
        │
        ▼
Browser: Plotly renders; zoom/pan/theme are client-side
```

## Performance Strategies

1. **Pre-aggregation**: `load_interval_aggregated()` uses InfluxDB's
   `aggregateWindow()` to reduce data before transfer (~200 ms windows for
   display).
2. **Client-side rendering**: the server ships figure specs once; all
   interaction (zoom, pan, theme restyle) happens in the browser.
3. **Plain scatter (not WebGL)**: `scattergl` caused context-loss crashes on
   HTMX swaps; charts use SVG `scatter` and are explicitly purged on swap
   (see `restyleCharts` in `base.html`).
4. **Parquet cache**: the offline analyzer streams full-resolution data once
   into local Parquet, making reruns and resumes cheap.

## Testing Strategy

- Services tested with mocked InfluxDB client.
- Fixtures in `tests/conftest.py`: `sample_settings`, `sample_imu_data`,
  `sample_location_data`, `mock_influx_client`, `env_file`.

## Security Considerations

### Flux Injection Prevention

All user input is escaped before use in Flux queries:

```python
def _escape_flux_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
```

### Credentials & Auth

- Session-cookie login (Starlette `SessionMiddleware`); users from
  `SROW_USERS`, secret from `SESSION_SECRET`.
- Never commit `.env`; production secrets live in `/etc/syncrow/` on the VPS
  (see `deploy/CREDENTIALS.md`).
- uvicorn binds to localhost only; nginx is the sole public face (TLS).

## Deployment

Internet → nginx :443 (Let's Encrypt) → uvicorn `web.main:app` on
`127.0.0.1:5006`, managed by systemd (`deploy/syncrow-portal.service`), on a
VPS that also hosts InfluxDB. Driven from a laptop via
`deploy/deploy-from-laptop.sh`. See `deploy/DEPLOY.md`.

## Extension Points

### Adding New Measurements

1. Create a new service in `srow/services/` following existing patterns.
2. Add a figure builder in `web/charts.py`.
3. Wire a route/fragment in `web/main.py` + `templates/`.

### Adding New Analysis

1. Prefer putting reusable signal-processing logic in the `srow` package so
   both the web app and `analyze_async.py` can share it.
2. Surface results either as a chart fragment (online) or CSV export
   (offline).
