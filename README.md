# SyncRow Data Explorer

High-performance data visualization for rowing IMU and GPS sensor data. Built with Panel + Datashader to handle millions of data points smoothly.

## Features

- **Time Series Visualization**: Interactive plots with Datashader for smooth rendering of 1M+ points
- **GPS Track Maps**: View boat paths on interactive maps with GeoViews
- **Data Tables**: Virtual-scrolling tables for exploring raw sensor data
- **Filtering**: Filter by sensor source, fields, and time ranges
- **InfluxDB Integration**: Direct queries to InfluxDB time-series database

## Quick Start

```bash
# Clone and enter directory
cd srow

# Run the app (creates venv, installs deps, launches)
./start.sh
```

Or manually:

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run the app
panel serve app.py --show --autoreload
```

## Configuration

Create a `.env` file with your InfluxDB credentials:

```env
INFLUX_URL=https://your-influxdb-server.com
INFLUX_TOKEN=your-token-here
INFLUX_ORG=YourOrg
INFLUX_ORG_ID=your-org-id
INFLUX_BUCKET=syncrow
```

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
srow/
├── app.py                  # Main Panel application
├── srow/
│   ├── config/            # Settings and environment loading
│   ├── services/          # Data access (InfluxDB, GPS)
│   ├── components/        # Panel UI components
│   ├── state/             # Application state management
│   └── utils/             # Utility functions
├── tests/                 # Test suite
└── docs/                  # Documentation
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for detailed system design.

## License

MIT
