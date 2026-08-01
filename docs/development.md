# SyncRow Portal Development Guide

This guide covers setting up the development environment and contributing.

## Prerequisites

- Python 3.11 or later
- Access to an InfluxDB instance with rowing data

## Setup

### 1. Clone the Repository

```bash
git clone https://github.com/StrongCodr/SyncRow_Portal.git
cd SyncRow_Portal
```

### 2. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install Dependencies

```bash
# web extra = FastAPI/uvicorn/Jinja2; dev extra = pytest/ruff
pip install -e ".[web,dev]"
```

### 4. Configure Environment

Create a `.env` file in the project root:

```env
INFLUX_URL=https://your-influxdb-server.com
INFLUX_TOKEN=your-api-token
INFLUX_ORG=YourOrganization
INFLUX_ORG_ID=your-org-id
INFLUX_BUCKET=syncrow
```

Optional for the web app: `SESSION_SECRET`, `SROW_USERS`
(comma-separated `user:password` pairs; dev fallback is `admin`/`admin`).

### 5. Verify Setup

```bash
# Run tests
pytest

# Start the dashboard
uvicorn web.main:app --reload --port 5006
```

Open http://localhost:5006.

## Project Structure

```
SyncRow_Portal/
├── pyproject.toml         # Project configuration and dependencies
├── .env                   # Environment configuration (not in git)
│
├── web/                   # FastAPI + HTMX + Plotly dashboard
│   ├── main.py            # Routes, session auth
│   ├── charts.py          # DataFrame -> Plotly figure-spec builders
│   └── templates/         # Jinja2 templates
│
├── srow/                  # Data-layer package
│   ├── config/            # Configuration management
│   └── services/          # Data access services (Influx, GPS, cache)
│
├── analyze_async.py       # Offline asynchronicity analyzer (research CLI)
│
├── tests/                 # Test suite
│   ├── conftest.py        # Shared fixtures
│   └── test_*.py          # Test modules
│
├── deploy/                # VPS deployment tooling
└── docs/                  # Documentation
```

## Running the Application

### Development Mode

```bash
# With auto-reload on code changes
uvicorn web.main:app --reload --port 5006
```

### Production Mode

Production runs under systemd + nginx on the VPS — do not run uvicorn
publicly by hand. See `deploy/DEPLOY.md`; deploys are driven via
`./deploy/deploy-from-laptop.sh update`.

## Testing

### Run All Tests

```bash
pytest
```

### Run with Coverage

```bash
pytest --cov=srow --cov-report=html

# Open coverage report
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

### Run Specific Tests

```bash
# Run a specific test file
pytest tests/test_settings.py

# Run tests matching a pattern
pytest -k "test_unwrap"

# Run with verbose output
pytest -v
```

## Code Style

### Linting

```bash
# Check for issues
ruff check .

# Auto-fix issues
ruff check --fix .
```

### Formatting

```bash
# Format code
ruff format .

# Check formatting without changes
ruff format --check .
```

### Style Guidelines

- Use type hints for function signatures
- Write docstrings for public functions and classes
- Keep functions focused and under 50 lines
- Use descriptive variable names

## Adding New Features

### Adding a New Service

1. Create the service file in `srow/services/`:

```python
# srow/services/my_service.py
from srow.config import Settings

class MyService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def my_method(self) -> dict:
        """Do something useful."""
        pass
```

2. Export from `__init__.py`:

```python
# srow/services/__init__.py
from .my_service import MyService
__all__ = [..., "MyService"]
```

3. Write tests in `tests/test_my_service.py`

4. Use in `web/main.py`

### Adding a New Chart

1. Add a pure builder function in `web/charts.py`
   (`DataFrame → Plotly figure-spec dict`, no framework imports):

```python
def my_fig(df: pd.DataFrame | None):
    if df is None or df.empty:
        return None
    data = [{"type": "scatter", "mode": "lines", ...}]
    layout = {**_BASE_LAYOUT, "height": 300}
    return {"data": data, "layout": layout}
```

2. Call it from the `/interval` route in `web/main.py` and embed the JSON in
   `templates/chart.html`.

3. Charts must use `scatter` (SVG), not `scattergl` — WebGL contexts crash on
   HTMX swaps. Theme restyling happens client-side in `base.html`
   (`restyleCharts`).

## Debugging

### Enable Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### FastAPI Interactive Docs

With the server running, http://localhost:5006/docs exposes the routes
(handy for checking `/interval` query params).

## Common Issues

### "Module not found" Errors

Make sure you've installed in editable mode with the web extra:

```bash
pip install -e ".[web,dev]"
```

### InfluxDB Connection Issues

1. Verify `.env` file exists and has correct values
2. Check network connectivity to InfluxDB
3. Verify token has read permissions

### Login Loop / Session Issues

Set a stable `SESSION_SECRET` — with the ephemeral default, sessions are
invalidated on every reload.

## Performance Profiling

### Profile Data Loading

```python
import cProfile
import pstats

with cProfile.Profile() as pr:
    df = service.load_interval(tag, value)

stats = pstats.Stats(pr)
stats.sort_stats('cumtime').print_stats(20)
```

## Contributing

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make changes and write tests
3. Run tests: `pytest`
4. Check style: `ruff check .`
5. Commit with descriptive message
6. Push and create pull request
