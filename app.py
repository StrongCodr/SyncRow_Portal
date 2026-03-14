"""SyncRow Data Explorer - Main Application.

A high-performance data visualization application for rowing IMU and GPS data.
Built with Panel + Datashader for handling millions of data points smoothly.

Usage:
    panel serve app.py --show --autoreload

Or use the start.sh script:
    ./start.sh
"""

import panel as pn

from srow.config import load_settings, Settings
from srow.services import CacheService, InfluxService, LocationService, format_influx_error
from srow.state import AppState
from srow.components import (
    SidebarComponent,
    TimeSeriesComponent,
    MapViewComponent,
    DataTableComponent,
)
from srow.components.data_table import SummaryTableComponent
from srow.components.time_series import SimpleTimeSeriesComponent, SyncIndicatorComponent, SpeedChartComponent, LinkedChartsComponent

# Enable Panel extensions
pn.extension("tabulator", sizing_mode="stretch_width")


# Display aggregation threshold - raw data above this count gets aggregated for display
DISPLAY_AGGREGATION_THRESHOLD = 10000


def _get_cache_status_text(cache_service) -> str:
    """Generate cache status markdown text."""
    if cache_service is None:
        return ""
    stats = cache_service.get_cache_stats()
    return f"""---
**Cache:** {stats['intervals_cached']} intervals ({stats['total_size_mb']:.1f} MB)"""


def _cache_in_background(cache_service, interval: dict, status_pane=None):
    """Cache raw data in background thread.

    Downloads full raw data from cloud and saves to local parquet
    without blocking the UI.
    """
    import threading

    def do_cache():
        try:
            # Fetch and cache raw IMU data
            cache_service.get_imu_data(interval)
            # Update status pane if available
            if status_pane:
                status_pane.object = _get_cache_status_text(cache_service)
        except Exception:
            pass  # Silently fail background caching

    thread = threading.Thread(target=do_cache, daemon=True)
    thread.start()


def _aggregate_for_display(df, target_rows: int = 5000):
    """Aggregate DataFrame for faster display rendering.

    Reduces row count by resampling on time while preserving source grouping.
    Used when raw data has too many points for smooth visualization.

    Args:
        df: DataFrame with 'time' and 'source' columns.
        target_rows: Target number of rows after aggregation.

    Returns:
        Aggregated DataFrame.
    """
    import pandas as pd

    if df.empty or len(df) <= target_rows:
        return df

    # Calculate aggregation window based on data duration and target rows
    if "time" not in df.columns:
        return df

    time_range = df["time"].max() - df["time"].min()
    n_sources = df["source"].nunique() if "source" in df.columns else 1
    target_per_source = max(1, target_rows // n_sources)

    # Calculate window size
    if hasattr(time_range, "total_seconds"):
        total_seconds = time_range.total_seconds()
    else:
        total_seconds = float(time_range) / 1e9  # nanoseconds to seconds

    window_seconds = max(1, int(total_seconds / target_per_source))
    window = f"{window_seconds}s"

    # Set time as index for resampling
    df_indexed = df.set_index("time")

    # Numeric columns to aggregate
    numeric_cols = df_indexed.select_dtypes(include=["number"]).columns.tolist()

    if "source" in df.columns:
        # Aggregate per source
        aggregated = (
            df_indexed.groupby("source")
            .resample(window)[numeric_cols]
            .mean()
            .reset_index()
        )
    else:
        aggregated = df_indexed.resample(window)[numeric_cols].mean().reset_index()

    return aggregated


def create_app(
    settings: Settings | None = None,
    influx_service: InfluxService | None = None,
    location_service: LocationService | None = None,
    cache_service: CacheService | None = None,
) -> pn.Template:
    """Create the Panel application.

    Args:
        settings: Optional settings (loads from .env if not provided).
        influx_service: Optional InfluxService (creates from settings if not provided).
        location_service: Optional LocationService (creates from settings if not provided).
        cache_service: Optional CacheService (creates from settings if not provided).

    Returns:
        Configured Panel template ready to serve.
    """
    # Initialize configuration and services
    if settings is None:
        try:
            settings = load_settings()
        except ValueError as e:
            # Return error page if settings are missing
            return _create_error_template(str(e))

    if influx_service is None:
        influx_service = InfluxService(settings)

    if location_service is None:
        location_service = LocationService(settings)

    # Create cache service if caching is enabled
    if cache_service is None and settings.cache_enabled:
        cache_service = CacheService(
            influx_service=influx_service,
            location_service=location_service,
            cache_dir=settings.cache_dir,
        )

    # Create application state
    state = AppState()

    # Check connection
    try:
        state.connected = influx_service.ping()
    except Exception as e:
        state.connected = False
        state.set_error(f"Connection failed: {format_influx_error(e)}")

    # Load initial intervals - use cache service if available
    if state.connected:
        try:
            if cache_service:
                # Sync interval list from cloud, update cache metadata
                state.intervals = cache_service.sync_interval_list()
            else:
                state.intervals = influx_service.fetch_interval_tags()
        except Exception as e:
            state.set_error(f"Failed to load intervals: {format_influx_error(e)}")

    # Create UI components
    sidebar = SidebarComponent(state)
    # Use LinkedChartsComponent for IMU, sync, and speed with linked zooming
    linked_charts = LinkedChartsComponent(state, cache_service=cache_service)
    map_view = MapViewComponent(state)
    data_table = DataTableComponent(state)
    summary_table = SummaryTableComponent(state)

    # Create cache status indicator (if caching is enabled)
    cache_status_pane = None
    if cache_service:
        cache_status_pane = pn.pane.Markdown(
            _get_cache_status_text(cache_service),
            styles={"font-size": "12px", "color": "#666"},
        )

        def update_cache_status():
            """Update cache status display."""
            if cache_status_pane:
                cache_status_pane.object = _get_cache_status_text(cache_service)

    # Wire up event handlers
    def load_interval_data(event=None):
        """Load data when interval selection changes."""
        interval = state.selected_interval
        if interval is None:
            state.clear_data()
            return

        state.loading = True
        state.clear_error()

        try:
            # Check if data is already cached
            is_imu_cached = False
            is_loc_cached = False
            if cache_service:
                cache_status = cache_service.is_cached(interval)
                is_imu_cached = cache_status.get("imu", False)
                is_loc_cached = cache_status.get("location", False)

            # Load IMU data
            if cache_service and is_imu_cached:
                # Cache hit - load from local parquet (instant)
                df = cache_service.get_imu_data(interval)
            else:
                # Cache miss - use aggregated data for fast display
                df = influx_service.load_interval_aggregated(
                    tag_name=interval["tag"],
                    interval_value=interval["value"],
                    window="1s",
                    aggregation="mean",
                )
                # Trigger background caching of raw data
                if cache_service:
                    _cache_in_background(cache_service, interval, cache_status_pane)

            # Note: Angle unwrapping disabled - rowing data oscillates around ±180
            # and unwrapping causes accumulation. Raw bounded angles are better.
            if not df.empty:
                # Optionally aggregate for display if too many points
                if len(df) > DISPLAY_AGGREGATION_THRESHOLD:
                    df = _aggregate_for_display(df)

            state.imu_data = df

            # Load location data
            try:
                if cache_service and is_loc_cached:
                    # Cache hit - load from local parquet
                    gdf = cache_service.get_location_data(interval)
                else:
                    # Cache miss - load from cloud
                    gdf = location_service.load_track(
                        tag_name=interval["tag"],
                        interval_value=interval["value"],
                    )
                    # Cache it for next time (location data is usually small)
                    if cache_service and not gdf.empty:
                        cache_service._fetch_and_cache_location(interval)
                state.location_data = gdf
            except Exception:
                # Location data may not exist for this interval
                import geopandas as gpd
                state.location_data = gpd.GeoDataFrame()

        except Exception as e:
            state.set_error(f"Failed to load data: {format_influx_error(e)}")
        finally:
            state.loading = False
            # Update cache status after loading
            if cache_service and cache_status_pane:
                cache_status_pane.object = _get_cache_status_text(cache_service)

    def refresh_intervals(event=None):
        """Refresh the list of available intervals from cloud."""
        try:
            if cache_service:
                # Re-sync from cloud - updates cache metadata, deletes stale
                state.intervals = cache_service.sync_interval_list()
            else:
                state.intervals = influx_service.fetch_interval_tags()
            state.clear_error()
        except Exception as e:
            state.set_error(f"Failed to refresh: {format_influx_error(e)}")

    # Connect handlers
    state.param.watch(load_interval_data, "selected_interval")
    sidebar.on_refresh(refresh_intervals)

    # Create loading indicator
    loading_indicator = pn.indicators.LoadingSpinner(
        value=False,
        width=30,
        height=30,
    )
    state.param.watch(lambda e: setattr(loading_indicator, "value", e.new), "loading")

    # Create error alert
    error_alert = pn.pane.Alert(
        "",
        alert_type="danger",
        visible=False,
    )

    def update_error(event):
        if event.new:
            error_alert.object = event.new
            error_alert.visible = True
        else:
            error_alert.visible = False

    state.param.watch(update_error, "error_message")

    # Build the main content area with tabs
    main_tabs = pn.Tabs(
        ("Overview", pn.Column(
            summary_table,
            linked_charts,  # Combined IMU, sync, and speed charts with linked zooming
            sizing_mode="stretch_both",
        )),
        ("Map", map_view),
        ("Raw Data", data_table),
        sizing_mode="stretch_both",
        dynamic=True,
    )

    # Create the main layout
    main_content = pn.Column(
        pn.Row(
            pn.pane.Markdown("# SyncRow Data Explorer"),
            pn.layout.HSpacer(),
            loading_indicator,
        ),
        error_alert,
        main_tabs,
        sizing_mode="stretch_both",
    )

    # Build sidebar content
    sidebar_content = [sidebar]
    if cache_status_pane:
        sidebar_content.append(cache_status_pane)

    # Use FastListTemplate for a clean layout
    template = pn.template.FastListTemplate(
        title="SyncRow",
        sidebar=sidebar_content,
        main=[main_content],
        accent_base_color="#1f77b4",
        header_background="#1f77b4",
    )

    return template


def _create_error_template(error_message: str) -> pn.Template:
    """Create an error template for configuration issues.

    Args:
        error_message: The error message to display.

    Returns:
        Panel template showing the error.
    """
    content = pn.Column(
        pn.pane.Markdown("# Configuration Error"),
        pn.pane.Alert(error_message, alert_type="danger"),
        pn.pane.Markdown("""
## Setup Instructions

1. Create a `.env` file in the project root with your InfluxDB credentials:

```
INFLUX_URL=https://your-influxdb-server.com
INFLUX_TOKEN=your-token-here
INFLUX_ORG=YourOrg
INFLUX_ORG_ID=your-org-id
INFLUX_BUCKET=syncrow
```

2. Restart the application.

See the README for more details.
        """),
        sizing_mode="stretch_width",
    )

    return pn.template.FastListTemplate(
        title="SyncRow - Configuration Error",
        main=[content],
    )


# Create the app instance for Panel to serve
app = create_app()

# Make it servable
app.servable()
