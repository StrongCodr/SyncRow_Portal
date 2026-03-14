# SyncRow Architecture

This document describes the system design and architecture of the SyncRow Data Explorer.

## Overview

SyncRow is a Panel-based web application for visualizing rowing sensor data. It's designed to handle large datasets (1M+ rows) through efficient data access patterns and visualization techniques.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Panel Application (app.py)                   │
├─────────────┬─────────────┬─────────────┬──────────────────────┤
│  Sidebar    │  Map View   │ Time Series │  Data Table          │
│  Controls   │  (GeoViews) │ (Datashader)│  (Tabulator)         │
└─────────────┴─────────────┴─────────────┴──────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│              State Management (param.Parameterized)             │
│                                                                 │
│   AppState: central reactive state container                    │
│   - connected, intervals, selected_interval                     │
│   - imu_data, location_data                                     │
│   - selected_sources, selected_fields, time_range               │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│                      Data Service Layer                         │
├─────────────────────────────┬──────────────────────────────────┤
│       InfluxService         │       LocationService            │
│       - fetch_interval_tags │       - load_track               │
│       - load_interval       │       - load_track_simplified    │
│       - load_aggregated     │       - calculate_distance       │
│       - summarize_interval  │       - calculate_speed_stats    │
└─────────────────────────────┴──────────────────────────────────┘
                              │
                         InfluxDB
                    (imu + phone_location)
```

## Component Responsibilities

### Config Layer (`srow/config/`)

- **settings.py**: Defines the `Settings` dataclass that holds InfluxDB connection parameters. Immutable (frozen dataclass) to prevent accidental modification.
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

### State Layer (`srow/state/`)

- **AppState**: Central reactive state using `param.Parameterized`. Components watch these parameters and update automatically when values change.

Key patterns:
```python
# Components watch state
state.param.watch(self._update_plot, "imu_data")

# State updates propagate to watchers
state.imu_data = new_dataframe  # Triggers _update_plot
```

### Component Layer (`srow/components/`)

- **SidebarComponent**: Interval selection, filters, connection status
- **TimeSeriesComponent**: Datashader-enabled time series plots
- **MapViewComponent**: GeoViews map with GPS tracks
- **DataTableComponent**: Tabulator with virtual scrolling

### Main Application (`app.py`)

The `create_app()` function:
1. Loads settings
2. Creates services
3. Creates state
4. Creates components
5. Wires up event handlers
6. Returns the Panel template

Supports dependency injection for testing.

## Data Flow

### Loading an Interval

```
User selects interval
        │
        ▼
state.selected_interval = {...}
        │
        ▼
load_interval_data() handler triggered
        │
        ├──► influx_service.load_interval()
        │           │
        │           ▼
        │    InfluxDB Flux query
        │           │
        │           ▼
        │    DataFrame with sensor data
        │
        ├──► influx_service.unwrap_angles()
        │           │
        │           ▼
        │    Angles corrected for ±180° wrap
        │
        ▼
state.imu_data = df
        │
        ▼
Components watching imu_data update automatically
```

### Filtering Data

```
User changes source selection
        │
        ▼
state.selected_sources = [...]
        │
        ▼
TimeSeriesComponent._update_plot() triggered
        │
        ▼
state.get_filtered_data() called
        │
        ▼
Plot updated with filtered data
```

## Performance Strategies

### Large Dataset Handling

1. **Datashader**: Server-side aggregation for plotting. Renders millions of points as aggregated images.

2. **Pre-aggregation**: `load_interval_aggregated()` uses InfluxDB's `aggregateWindow()` to reduce data before transfer.

3. **Virtual scrolling**: Tabulator only renders visible rows, handling millions efficiently.

4. **Time range queries**: `load_time_range()` loads only a window of data when zoomed in.

### Memory Management

- `clear_data()` releases data when switching intervals
- Services don't cache data (state is the single source)
- Chunked loading available via `load_interval_chunked()`

## Testing Strategy

### Unit Tests

- Services tested with mocked InfluxDB client
- Utility functions tested with edge cases
- State tested for reactive behavior

### Integration Tests

- Components tested with sample data fixtures
- Data flow tested end-to-end

### Fixtures

Common fixtures in `conftest.py`:
- `sample_settings`: Test configuration
- `sample_imu_data`: 200 rows of IMU data
- `sample_location_data`: 50 GPS points

## Security Considerations

### Flux Injection Prevention

All user input is escaped before use in Flux queries:

```python
def _escape_flux_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
```

### Credentials

- Never commit `.env` files
- Use environment variables in production
- `.gitignore` excludes `.env`

## Extension Points

### Adding New Measurements

1. Create new service in `srow/services/`
2. Add query methods following existing patterns
3. Update state with new data parameter
4. Create or update component to visualize

### Adding New Visualizations

1. Create component in `srow/components/`
2. Watch relevant state parameters
3. Add to template in `app.py`

### Adding New Analysis

1. Add analysis methods to appropriate service
2. Create component to display results
3. Wire up in `app.py`
