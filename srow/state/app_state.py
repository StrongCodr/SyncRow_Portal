"""Central application state management.

Uses param.Parameterized for reactive state that Panel components
can watch and respond to automatically.
"""

from datetime import datetime

import param
import pandas as pd
import geopandas as gpd


class AppState(param.Parameterized):
    """Central state container for the SyncRow application.

    Components can watch these parameters for reactivity. When a parameter
    changes, dependent components automatically update.

    Attributes:
        connected: Whether InfluxDB connection is active.
        intervals: List of available interval options.
        selected_interval: Currently selected interval dict.
        imu_data: Loaded IMU DataFrame.
        location_data: Loaded GPS GeoDataFrame.
        selected_sources: List of selected sensor sources to display.
        selected_fields: List of selected fields to plot.
        time_range: Tuple of (start, end) datetime for filtering.
        loading: Whether data is currently being loaded.
        error_message: Current error message to display (if any).
    """

    # Connection state
    connected = param.Boolean(
        default=False,
        doc="Whether InfluxDB connection is active",
    )

    # Interval selection
    intervals = param.List(
        default=[],
        doc="List of available interval dicts with tag, value, label",
    )
    selected_interval = param.Dict(
        default=None,
        allow_None=True,
        doc="Currently selected interval (dict with tag, value, label)",
    )

    # Data
    imu_data = param.DataFrame(
        default=pd.DataFrame(),
        doc="Loaded IMU sensor data",
    )
    location_data = param.Parameter(
        default=gpd.GeoDataFrame(),
        doc="Loaded GPS location data as GeoDataFrame",
    )

    # Filters
    available_sources = param.List(
        default=[],
        doc="List of available source labels in current data",
    )
    selected_sources = param.List(
        default=[],
        doc="List of selected sources to display",
    )
    available_fields = param.List(
        default=[],
        doc="List of available numeric fields in current data",
    )
    selected_fields = param.List(
        default=[],
        doc="List of selected fields to plot",
    )
    time_range = param.Tuple(
        default=(None, None),
        length=2,
        doc="Time range filter as (start, end) datetime tuple",
    )
    view_time_range = param.Tuple(
        default=(None, None),
        length=2,
        doc="Visible time range in charts (for linked zooming)",
    )

    # UI state
    loading = param.Boolean(
        default=False,
        doc="Whether data is currently being loaded",
    )
    error_message = param.String(
        default="",
        doc="Current error message to display",
    )

    def __init__(self, **params):
        """Initialize the application state."""
        super().__init__(**params)

    @param.depends("imu_data", watch=True)
    def _update_available_options(self):
        """Update available sources and fields when data changes."""
        if self.imu_data.empty:
            self.available_sources = []
            self.available_fields = []
            return

        # Extract unique sources
        if "source" in self.imu_data.columns:
            self.available_sources = sorted(self.imu_data["source"].unique().tolist())
        else:
            self.available_sources = []

        # Extract numeric fields (exclude time and source)
        exclude_cols = {"time", "source", "device_id"}
        numeric_cols = self.imu_data.select_dtypes(include=["number"]).columns
        self.available_fields = [c for c in numeric_cols if c not in exclude_cols]

        # Set default selections if empty
        if not self.selected_sources and self.available_sources:
            self.selected_sources = self.available_sources.copy()

        if not self.selected_fields and self.available_fields:
            # Default to common IMU fields if available
            default_fields = ["roll", "pitch", "yaw", "ax", "ay", "az"]
            self.selected_fields = [f for f in default_fields if f in self.available_fields]
            if not self.selected_fields:
                self.selected_fields = self.available_fields[:3]

    @param.depends("imu_data", watch=True)
    def _update_time_range(self):
        """Update time range bounds when data changes."""
        if self.imu_data.empty or "time" not in self.imu_data.columns:
            self.time_range = (None, None)
            self.view_time_range = (None, None)
            return

        t_min = self.imu_data["time"].min()
        t_max = self.imu_data["time"].max()
        self.time_range = (t_min, t_max)
        # Reset view to full range when new data loads
        self.view_time_range = (t_min, t_max)

    def get_filtered_data(self) -> pd.DataFrame:
        """Get IMU data filtered by current selections.

        Returns:
            Filtered DataFrame based on selected sources and time range.
        """
        df = self.imu_data

        if df.empty:
            return df

        # Filter by selected sources
        if self.selected_sources and "source" in df.columns:
            df = df[df["source"].isin(self.selected_sources)]

        # Filter by time range
        if self.time_range[0] is not None and "time" in df.columns:
            df = df[df["time"] >= self.time_range[0]]
        if self.time_range[1] is not None and "time" in df.columns:
            df = df[df["time"] <= self.time_range[1]]

        return df

    def get_plot_data(self) -> pd.DataFrame:
        """Get data ready for plotting.

        Returns:
            Filtered DataFrame with only time, source, and selected fields.
        """
        df = self.get_filtered_data()

        if df.empty:
            return df

        # Select only relevant columns
        cols = ["time", "source"]
        cols.extend([f for f in self.selected_fields if f in df.columns])

        return df[cols]

    def clear_data(self):
        """Clear all loaded data and reset selections."""
        self.imu_data = pd.DataFrame()
        self.location_data = gpd.GeoDataFrame()
        self.selected_sources = []
        self.selected_fields = []
        self.time_range = (None, None)
        self.error_message = ""

    def set_error(self, message: str):
        """Set an error message for display.

        Args:
            message: Error message to display.
        """
        self.error_message = message
        self.loading = False

    def clear_error(self):
        """Clear the current error message."""
        self.error_message = ""

    @property
    def has_data(self) -> bool:
        """Check if any data is currently loaded."""
        return not self.imu_data.empty

    @property
    def has_location_data(self) -> bool:
        """Check if GPS location data is loaded."""
        return not self.location_data.empty

    @property
    def data_summary(self) -> dict:
        """Get a summary of the current data.

        Returns:
            Dict with row_count, source_count, field_count, time_span.
        """
        if not self.has_data:
            return {
                "row_count": 0,
                "source_count": 0,
                "field_count": 0,
                "time_span": None,
            }

        df = self.imu_data
        return {
            "row_count": len(df),
            "source_count": len(self.available_sources),
            "field_count": len(self.available_fields),
            "time_span": self.time_range,
        }
