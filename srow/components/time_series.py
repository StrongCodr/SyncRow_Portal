"""Time series visualization component using Datashader.

Provides high-performance rendering of large time-series datasets
with linked brushing and multi-source overlay support.
"""

import panel as pn
import param
import holoviews as hv
import pandas as pd

from srow.state import AppState

# Enable Bokeh backend
hv.extension("bokeh")


class TimeSeriesComponent(pn.viewable.Viewer):
    """Time series plot with Datashader for large datasets.

    Automatically uses Datashader for datasets over 10,000 points
    to maintain smooth interaction.
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")
    datashader_threshold = param.Integer(
        default=10000,
        doc="Point count above which to use Datashader",
    )

    def __init__(self, state: AppState, **params):
        """Initialize the time series component.

        Args:
            state: Application state to watch.
        """
        params["state"] = state
        super().__init__(**params)

        self._plot_pane = pn.pane.HoloViews(
            None,
            sizing_mode="stretch_both",
            min_height=400,
        )

        # Watch for data and selection changes
        state.param.watch(self._update_plot, ["imu_data", "selected_sources", "selected_fields"])

    def _update_plot(self, event=None):
        """Update the plot when data or selections change."""
        df = self.state.get_plot_data()

        if df.empty or not self.state.selected_fields:
            self._plot_pane.object = hv.Curve([]).opts(
                title="No data selected",
                xlabel="Time",
                ylabel="Value",
            )
            return

        # Build overlay of curves for each source and field
        curves = []

        for field in self.state.selected_fields:
            if field not in df.columns:
                continue

            for source in df["source"].unique():
                source_df = df[df["source"] == source]
                if source_df.empty:
                    continue

                # Create curve
                curve = hv.Curve(
                    source_df,
                    kdims=["time"],
                    vdims=[field],
                    label=f"{source} - {field}",
                )
                curves.append(curve)

        if not curves:
            self._plot_pane.object = hv.Curve([]).opts(title="No data to plot")
            return

        # Combine curves into overlay
        overlay = hv.Overlay(curves)

        # Determine if we should use datashader
        total_points = len(df) * len(self.state.selected_fields)
        use_datashader = total_points > self.datashader_threshold

        if use_datashader:
            # Use datashader for large datasets
            from holoviews.operation.datashader import datashade, dynspread

            # For datashader, we need to handle each curve separately
            # and use rasterize instead of datashade for colored lines
            from holoviews.operation.datashader import rasterize

            overlay = rasterize(overlay).opts(
                cmap="viridis",
                colorbar=True,
            )

        # Apply styling
        plot_opts = {
            "width": 800,
            "height": 400,
            "responsive": True,
            "tools": ["pan", "wheel_zoom", "box_zoom", "reset", "save"],
            "active_tools": ["wheel_zoom"],
            "title": "IMU Time Series",
            "xlabel": "Time",
            "ylabel": "Value",
            "legend_position": "right",
            "show_grid": True,
        }

        self._plot_pane.object = overlay.opts(**plot_opts)

    def __panel__(self):
        """Return the Panel layout for this component."""
        return pn.Column(
            self._plot_pane,
            sizing_mode="stretch_both",
        )


class SimpleTimeSeriesComponent(pn.viewable.Viewer):
    """Simplified time series using hvplot for easier debugging.

    Use this if the full Datashader component has issues.
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")

    def __init__(self, state: AppState, **params):
        """Initialize the simple time series component."""
        params["state"] = state
        super().__init__(**params)

        self._plot_pane = pn.pane.HoloViews(
            None,
            sizing_mode="stretch_both",
            min_height=400,
            linked_axes=True,  # Link axes with other HoloViews panes
        )

        state.param.watch(self._update_plot, ["imu_data", "selected_sources", "selected_fields"])

    def _update_plot(self, event=None):
        """Update the plot when data changes."""
        import hvplot.pandas  # noqa: F401

        df = self.state.get_plot_data()

        if df.empty or not self.state.selected_fields:
            self._plot_pane.object = None
            return

        # Melt for easier plotting
        id_vars = ["time", "source"]
        value_vars = [f for f in self.state.selected_fields if f in df.columns]

        if not value_vars:
            self._plot_pane.object = None
            return

        df_melted = df.melt(
            id_vars=id_vars,
            value_vars=value_vars,
            var_name="field",
            value_name="value",
        )

        # Create label column
        df_melted["label"] = df_melted["source"] + " - " + df_melted["field"]

        # Plot with hvplot
        plot = df_melted.hvplot.line(
            x="time",
            y="value",
            by="label",
            responsive=True,
            height=400,
            legend="right",
            title="IMU Time Series",
        )

        self._plot_pane.object = plot

    def __panel__(self):
        """Return the Panel layout."""
        return self._plot_pane


class SyncIndicatorComponent(pn.viewable.Viewer):
    """Synchronicity indicator showing how well-aligned sensors are.

    Computes a running sync metric by:
    1. Computing rotation-invariant magnitude (sqrt(ax²+ay²+az²))
    2. Normalizing each sensor's signal (z-score)
    3. Computing the spread (std) across sensors at each time point
    4. Low spread = synchronized, high spread = out of sync

    Uses RAW data from cache for accurate sync computation, then
    aggregates the result for smooth display.
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")
    cache_service = param.Parameter(default=None, doc="Cache service for raw data access")

    def __init__(self, state: AppState, cache_service=None, **params):
        """Initialize the sync indicator component.

        Args:
            state: Application state to watch.
            cache_service: CacheService for loading raw data.
        """
        params["state"] = state
        params["cache_service"] = cache_service
        super().__init__(**params)

        self._plot_pane = pn.pane.HoloViews(
            None,
            sizing_mode="stretch_both",
            min_height=200,
            linked_axes=True,  # Link axes with other HoloViews panes
        )

        state.param.watch(self._update_plot, ["selected_interval"])

    def _compute_magnitude(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute rotation-invariant magnitude fields.

        Creates accel_mag and gyro_mag from individual axis components.
        These are rotation-invariant, so sensors mounted at different
        orientations can be meaningfully compared.

        Args:
            df: DataFrame with ax, ay, az, wx, wy, wz columns.

        Returns:
            DataFrame with added accel_mag and gyro_mag columns.
        """
        import numpy as np

        result = df.copy()

        # Acceleration magnitude (rotation invariant)
        if all(c in df.columns for c in ["ax", "ay", "az"]):
            result["accel_mag"] = np.sqrt(
                df["ax"] ** 2 + df["ay"] ** 2 + df["az"] ** 2
            )

        # Angular velocity magnitude (rotation invariant)
        if all(c in df.columns for c in ["wx", "wy", "wz"]):
            result["gyro_mag"] = np.sqrt(
                df["wx"] ** 2 + df["wy"] ** 2 + df["wz"] ** 2
            )

        return result

    def _compute_sync_metric(self, df: pd.DataFrame, field: str) -> pd.DataFrame:
        """Compute synchronicity metric for a single field across all sources.

        Args:
            df: DataFrame with time, source, and field columns.
            field: The field to analyze.

        Returns:
            DataFrame with time and sync_metric columns.
        """
        if field not in df.columns or "source" not in df.columns:
            return pd.DataFrame()

        # Pivot to have sources as columns
        pivot = df.pivot_table(
            index="time",
            columns="source",
            values=field,
            aggfunc="first",
        )

        if pivot.empty or len(pivot.columns) < 2:
            return pd.DataFrame()

        # Normalize each source (z-score) - removes absolute offset differences
        normalized = pivot.apply(lambda x: (x - x.mean()) / x.std() if x.std() > 0 else x * 0)

        # Compute spread across sources at each time point
        # Low std = synchronized, high std = out of sync
        sync_spread = normalized.std(axis=1)

        # Invert and scale to make it intuitive (higher = better sync)
        # Use 1 / (1 + spread) to get a 0-1 metric where 1 = perfect sync
        sync_metric = 1 / (1 + sync_spread)

        result = pd.DataFrame({
            "time": sync_spread.index,
            "sync_spread": sync_spread.values,
            "sync_score": sync_metric.values,
        })

        return result

    def _update_plot(self, event=None):
        """Update the sync indicator plot.

        Loads RAW data from cache for accurate sync computation,
        then aggregates the result for smooth display.
        """
        import hvplot.pandas  # noqa: F401

        interval = self.state.selected_interval
        if interval is None:
            self._plot_pane.object = None
            return

        # Need cache service to access raw data
        if self.cache_service is None:
            # Fall back to state data if no cache service
            df = self.state.imu_data
            if df.empty:
                self._plot_pane.object = None
                return
        else:
            # Check if raw data is cached
            cache_status = self.cache_service.is_cached(interval)
            if not cache_status.get("imu", False):
                # Raw data not cached yet - show placeholder
                self._plot_pane.object = None
                return

            # Load RAW data from cache (full resolution)
            df = self.cache_service.get_imu_data(interval)

        if df.empty:
            self._plot_pane.object = None
            return

        # Compute rotation-invariant magnitude fields
        df = self._compute_magnitude(df)

        # Get unique sources
        sources = df["source"].unique() if "source" in df.columns else []
        if len(sources) < 2:
            # Need at least 2 sensors to measure sync
            self._plot_pane.object = None
            return

        # Use magnitude fields for rotation-invariant sync measurement
        # Also include roll/pitch/yaw which are already orientation-aware
        sync_fields = ["accel_mag", "gyro_mag", "roll", "pitch", "yaw"]

        # Compute sync metric for each field
        sync_dfs = []
        for field in sync_fields:
            if field not in df.columns:
                continue
            sync_df = self._compute_sync_metric(df, field)
            if not sync_df.empty:
                sync_df["field"] = field
                sync_dfs.append(sync_df)

        if not sync_dfs:
            self._plot_pane.object = None
            return

        combined = pd.concat(sync_dfs, ignore_index=True)

        # Aggregate for display (raw data may have 400K+ rows)
        # Resample to ~100ms windows for smooth plotting
        if len(combined) > 10000:
            combined = combined.set_index("time")
            # Group by field, resample, then reset
            aggregated_dfs = []
            for field_name in combined["field"].unique():
                field_df = combined[combined["field"] == field_name].copy()
                field_df = field_df.drop(columns=["field"])
                resampled = field_df.resample("100ms").mean()
                resampled["field"] = field_name
                aggregated_dfs.append(resampled.reset_index())
            combined = pd.concat(aggregated_dfs, ignore_index=True)

        # Plot the sync spread (lower = better sync)
        plot = combined.hvplot.line(
            x="time",
            y="sync_spread",
            by="field",
            responsive=True,
            height=200,
            legend="right",
            title=f"Sync Deviation ({len(sources)} sensors) - Lower = Better Sync",
            ylabel="Deviation",
            color=["#e41a1c", "#377eb8", "#4daf4a", "#984ea3", "#ff7f00"],
        )

        self._plot_pane.object = plot

    def __panel__(self):
        """Return the Panel layout."""
        return self._plot_pane


class SpeedChartComponent(pn.viewable.Viewer):
    """Speed chart showing GPS speed over time.

    Displays boat speed from GPS data, synchronized with other charts.
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")

    def __init__(self, state: AppState, **params):
        """Initialize the speed chart component.

        Args:
            state: Application state to watch.
        """
        params["state"] = state
        super().__init__(**params)

        self._plot_pane = pn.pane.HoloViews(
            None,
            sizing_mode="stretch_both",
            min_height=150,
            linked_axes=True,  # Link axes with other HoloViews panes
        )

        state.param.watch(self._update_plot, ["location_data"])

    def _update_plot(self, event=None):
        """Update the speed chart when location data changes."""
        import hvplot.pandas  # noqa: F401

        gdf = self.state.location_data

        if gdf.empty or "speed" not in gdf.columns:
            self._plot_pane.object = None
            return

        # Convert to regular DataFrame for hvplot
        df = pd.DataFrame({
            "time": gdf["time"] if "time" in gdf.columns else gdf.index,
            "speed": gdf["speed"],
        })

        # Filter out invalid speeds
        df = df[df["speed"].notna() & (df["speed"] >= 0)]

        if df.empty:
            self._plot_pane.object = None
            return

        # Convert speed from m/s to km/h for readability
        df["speed_kmh"] = df["speed"] * 3.6

        # Plot speed over time
        plot = df.hvplot.line(
            x="time",
            y="speed_kmh",
            responsive=True,
            height=150,
            title="Boat Speed",
            ylabel="Speed (km/h)",
            color="#2ca02c",  # Green
            line_width=1.5,
        )

        self._plot_pane.object = plot

    def __panel__(self):
        """Return the Panel layout."""
        return self._plot_pane


class LinkedChartsComponent(pn.viewable.Viewer):
    """Combined time-series charts with linked zooming.

    Combines IMU time series, sync indicator, and speed chart into
    a single HoloViews layout so zooming on any chart affects all.
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")
    cache_service = param.Parameter(default=None, doc="Cache service for raw data access")

    def __init__(self, state: AppState, cache_service=None, **params):
        """Initialize the linked charts component."""
        params["state"] = state
        params["cache_service"] = cache_service
        super().__init__(**params)

        self._plot_pane = pn.pane.HoloViews(
            None,
            sizing_mode="stretch_both",
            min_height=700,
        )

        # Watch for data changes
        state.param.watch(
            self._update_plots,
            ["imu_data", "location_data", "selected_sources", "selected_fields", "selected_interval"]
        )

    def _create_imu_plot(self):
        """Create the IMU time series plot."""
        import hvplot.pandas  # noqa: F401

        df = self.state.get_plot_data()

        if df.empty or not self.state.selected_fields:
            return None

        id_vars = ["time", "source"]
        value_vars = [f for f in self.state.selected_fields if f in df.columns]

        if not value_vars:
            return None

        df_melted = df.melt(
            id_vars=id_vars,
            value_vars=value_vars,
            var_name="field",
            value_name="value",
        )
        df_melted["label"] = df_melted["source"] + " - " + df_melted["field"]

        plot = df_melted.hvplot.line(
            x="time",
            y="value",
            by="label",
            responsive=True,
            height=350,
            legend="right",
            title="IMU Time Series",
        )

        return plot

    def _compute_magnitude(self, df):
        """Compute rotation-invariant magnitude fields."""
        import numpy as np

        result = df.copy()

        if all(c in df.columns for c in ["ax", "ay", "az"]):
            result["accel_mag"] = np.sqrt(
                df["ax"] ** 2 + df["ay"] ** 2 + df["az"] ** 2
            )

        if all(c in df.columns for c in ["wx", "wy", "wz"]):
            result["gyro_mag"] = np.sqrt(
                df["wx"] ** 2 + df["wy"] ** 2 + df["wz"] ** 2
            )

        return result

    def _compute_sync_metric(self, df, field):
        """Compute sync metric for a field across sources."""
        if field not in df.columns or "source" not in df.columns:
            return pd.DataFrame()

        pivot = df.pivot_table(
            index="time",
            columns="source",
            values=field,
            aggfunc="first",
        )

        if pivot.empty or len(pivot.columns) < 2:
            return pd.DataFrame()

        normalized = pivot.apply(lambda x: (x - x.mean()) / x.std() if x.std() > 0 else x * 0)
        sync_spread = normalized.std(axis=1)

        return pd.DataFrame({
            "time": sync_spread.index,
            "sync_spread": sync_spread.values,
        })

    def _create_sync_plot(self):
        """Create the sync indicator plot."""
        import hvplot.pandas  # noqa: F401

        interval = self.state.selected_interval
        if interval is None or self.cache_service is None:
            return None

        cache_status = self.cache_service.is_cached(interval)
        if not cache_status.get("imu", False):
            return None

        df = self.cache_service.get_imu_data(interval)
        if df.empty:
            return None

        df = self._compute_magnitude(df)

        sources = df["source"].unique() if "source" in df.columns else []
        if len(sources) < 2:
            return None

        sync_fields = ["accel_mag", "gyro_mag", "roll", "pitch", "yaw"]
        sync_dfs = []

        for field in sync_fields:
            if field not in df.columns:
                continue
            sync_df = self._compute_sync_metric(df, field)
            if not sync_df.empty:
                sync_df["field"] = field
                sync_dfs.append(sync_df)

        if not sync_dfs:
            return None

        combined = pd.concat(sync_dfs, ignore_index=True)

        # Aggregate for display
        if len(combined) > 10000:
            combined = combined.set_index("time")
            aggregated_dfs = []
            for field_name in combined["field"].unique():
                field_df = combined[combined["field"] == field_name].copy()
                field_df = field_df.drop(columns=["field"])
                resampled = field_df.resample("100ms").mean()
                resampled["field"] = field_name
                aggregated_dfs.append(resampled.reset_index())
            combined = pd.concat(aggregated_dfs, ignore_index=True)

        plot = combined.hvplot.line(
            x="time",
            y="sync_spread",
            by="field",
            responsive=True,
            height=180,
            legend="right",
            title=f"Sync Deviation ({len(sources)} sensors) - Lower = Better",
            ylabel="Deviation",
            color=["#e41a1c", "#377eb8", "#4daf4a", "#984ea3", "#ff7f00"],
        )

        return plot

    def _create_speed_plot(self):
        """Create the speed chart."""
        import hvplot.pandas  # noqa: F401

        gdf = self.state.location_data

        if gdf.empty or "speed" not in gdf.columns:
            return None

        df = pd.DataFrame({
            "time": gdf["time"] if "time" in gdf.columns else gdf.index,
            "speed": gdf["speed"],
        })

        df = df[df["speed"].notna() & (df["speed"] >= 0)]

        if df.empty:
            return None

        df["speed_kmh"] = df["speed"] * 3.6

        plot = df.hvplot.line(
            x="time",
            y="speed_kmh",
            responsive=True,
            height=150,
            title="Boat Speed",
            ylabel="Speed (km/h)",
            color="#2ca02c",
            line_width=1.5,
        )

        return plot

    def _update_plots(self, event=None):
        """Update all plots when data changes."""
        plots = []

        # IMU time series
        imu_plot = self._create_imu_plot()
        if imu_plot is not None:
            plots.append(imu_plot)

        # Sync indicator
        sync_plot = self._create_sync_plot()
        if sync_plot is not None:
            plots.append(sync_plot)

        # Speed chart
        speed_plot = self._create_speed_plot()
        if speed_plot is not None:
            plots.append(speed_plot)

        if not plots:
            self._plot_pane.object = None
            return

        # Combine plots into a layout with shared x-axis
        # Using hv.Layout with .cols(1) creates a vertical stack with linked axes
        combined = hv.Layout(plots).cols(1).opts(shared_axes=True)

        self._plot_pane.object = combined

    def __panel__(self):
        """Return the Panel layout."""
        return self._plot_pane
