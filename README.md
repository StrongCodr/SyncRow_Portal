# SyncRow Portal

Web dashboard and analysis tools for rowing IMU and GPS sensor data. The portal
(FastAPI + HTMX + Plotly) visualizes recorded intervals — crew synchronicity,
boat speed, and GPS track — from data uploaded to InfluxDB by the SyncRow
Android app. Live at https://syncrow.cloud.

## Components

- **`web/`** — production dashboard: FastAPI + HTMX + Tailwind + Plotly.
  Session-cookie login, interval picker, sync/speed/IMU charts, GPS map.
  Server builds Plotly figure specs as JSON; the browser handles zoom/pan/theme.
- **`srow/`** — framework-agnostic data layer: InfluxDB queries
  (`srow/services/influx_service.py`), GPS tracks (`location_service.py`),
  local Parquet cache (`cache_service.py`), settings (`srow/config/`).
- **`analyze_async.py`** — offline research CLI: per-stroke catch detection,
  cross-sensor asynchronicity (ms), and empirical speed-loss analysis.
- **`deploy/`** — native (no-Docker) VPS deployment: nginx + uvicorn + systemd.
  See [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

## Quick Start

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (web extra = FastAPI/uvicorn/Jinja2)
pip install -e ".[web,dev]"

# Run the dashboard
uvicorn web.main:app --reload --port 5006
```

Then open http://localhost:5006 (dev login `admin`/`admin` unless `SROW_USERS` is set).

## Configuration

Create a `.env` file with your InfluxDB credentials:

```env
INFLUX_URL=https://your-influxdb-server.com
INFLUX_TOKEN=your-token-here
INFLUX_ORG=YourOrg
INFLUX_ORG_ID=your-org-id
INFLUX_BUCKET=syncrow
```

The web app additionally reads `SESSION_SECRET` and `SROW_USERS`
(`user:password` pairs, comma-separated).

> **Production credentials** (dashboard login, Influx token, deploy key, TLS) are
> kept on the server, not in this repo. See [`deploy/CREDENTIALS.md`](deploy/CREDENTIALS.md)
> for exactly where each one lives.

## Data Model

### IMU Measurement

Inertial Measurement Unit data from sensors mounted on oars/seats:

| Field | Description |
|-------|-------------|
| `ax`, `ay`, `az` | Accelerometer (m/s^2) |
| `roll`, `pitch`, `yaw` | Orientation angles (degrees) |

Tags: `intervalId`, `sensorId`, `seat`

### Phone Location Measurement

GPS data from phones:

| Field | Description |
|-------|-------------|
| `latitude`, `longitude` | Position (degrees) |
| `altitude` | Elevation (meters) |
| `speed` | Velocity (m/s) |
| `accuracy` | GPS accuracy (meters) |
| `bearing` | Heading (degrees) |

Tags: `intervalId`, `deviceId`

## Offline Analysis

`analyze_async.py` downloads all interval data into the local Parquet cache and
quantifies crew asynchronicity: it detects each rower's catch (median-crossing,
sub-sample precision), matches catches across sensors to get per-stroke async
in ms, segments stable-SPM pieces, and tests empirical speed loss against a
linear model. Results export to `async_analysis_results.csv`.

```bash
python analyze_async.py --help
```

## Development

```bash
# Run tests
pytest

# Run tests with coverage
pytest --cov=srow --cov-report=html

# Lint code
ruff check .

# Format code
ruff format .
```

## Project Structure

```
SyncRow_Portal/
├── web/                   # FastAPI + HTMX + Plotly dashboard (production UI)
│   ├── main.py            # Routes, session auth
│   ├── charts.py          # DataFrame -> Plotly figure-spec builders
│   └── templates/         # Jinja2 templates (base, index, chart, login)
├── srow/
│   ├── config/            # Settings and environment loading
│   └── services/          # Data access (InfluxDB, GPS, Parquet cache)
├── analyze_async.py       # Offline asynchronicity analyzer (research CLI)
├── deploy/                # VPS deployment (nginx, systemd, scripts)
├── tests/                 # Test suite
└── docs/                  # Documentation
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for detailed system design.

## License

MIT
