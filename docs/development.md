# SyncRow Development Guide

This guide covers setting up the development environment and contributing to SyncRow.

## Prerequisites

- Python 3.11 or later
- Access to an InfluxDB instance with rowing data

## Setup

### 1. Clone the Repository

```bash
cd /path/to/your/projects
git clone <repository-url> srow
cd srow
```

### 2. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install Dependencies

```bash
# Install with development dependencies
pip install -e ".[dev]"
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

### 5. Verify Setup

```bash
# Run tests
pytest

# Start the app
panel serve app.py --show --autoreload
```

## Project Structure

```
srow/
├── app.py                  # Main application entry point
├── pyproject.toml          # Project configuration and dependencies
├── start.sh               # Quick-start script
├── .env                   # Environment configuration (not in git)
│
├── srow/                  # Main package
│   ├── config/           # Configuration management
│   ├── services/         # Data access services
│   ├── components/       # Panel UI components
│   ├── state/            # Application state
│   └── utils/            # Utility functions
│
├── tests/                # Test suite
│   ├── conftest.py      # Shared fixtures
│   └── test_*.py        # Test modules
│
└── docs/                 # Documentation
```

## Running the Application

### Development Mode

```bash
# With auto-reload on code changes
panel serve app.py --show --autoreload

# Or use the start script
./start.sh
```

### Production Mode

```bash
panel serve app.py --address 0.0.0.0 --port 5006 --allow-websocket-origin=*
```

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

4. Use in `app.py`

### Adding a New Component

1. Create the component in `srow/components/`:

```python
# srow/components/my_component.py
import panel as pn
import param
from srow.state import AppState

class MyComponent(pn.viewable.Viewer):
    state = param.ClassSelector(class_=AppState)

    def __init__(self, state: AppState, **params):
        params["state"] = state
        super().__init__(**params)
        # Initialize widgets...

    def __panel__(self):
        return pn.Column(...)
```

2. Export from `__init__.py`

3. Add to the template in `app.py`

### Adding State Parameters

1. Add parameter to `AppState`:

```python
# srow/state/app_state.py
class AppState(param.Parameterized):
    my_param = param.String(default="", doc="Description")
```

2. Components can watch the parameter:

```python
state.param.watch(self._on_my_param_change, "my_param")
```

## Debugging

### Enable Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Panel Debug Mode

```bash
panel serve app.py --show --autoreload --log-level=debug
```

### Inspect State

In a running app, you can access state via:

```python
# In browser console (if using Panel with pn.extension(debugger))
Bokeh.documents[0].get_model_by_name('AppState')
```

## Common Issues

### "Module not found" Errors

Make sure you've installed in editable mode:

```bash
pip install -e ".[dev]"
```

### InfluxDB Connection Issues

1. Verify `.env` file exists and has correct values
2. Check network connectivity to InfluxDB
3. Verify token has read permissions

### GeoViews Not Available

Install geospatial dependencies:

```bash
pip install geoviews geopandas
```

On macOS, you may need:

```bash
brew install gdal
```

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

### Memory Profiling

```bash
pip install memory-profiler

python -m memory_profiler app.py
```

## Contributing

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make changes and write tests
3. Run tests: `pytest`
4. Check style: `ruff check .`
5. Commit with descriptive message
6. Push and create pull request
