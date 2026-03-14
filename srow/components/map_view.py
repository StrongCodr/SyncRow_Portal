"""Map visualization component for GPS tracks.

Uses GeoViews with tile basemaps to display boat tracks
from phone GPS data.
"""

import panel as pn
import param
import geopandas as gpd

from srow.state import AppState

# Import GeoViews components
try:
    import geoviews as gv
    import geoviews.tile_sources as gvts
    gv.extension("bokeh")
    GEOVIEWS_AVAILABLE = True
except ImportError:
    GEOVIEWS_AVAILABLE = False


class MapViewComponent(pn.viewable.Viewer):
    """Map view showing GPS tracks on a tile basemap.

    Displays phone_location data as points or lines on an interactive map.
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")

    def __init__(self, state: AppState, **params):
        """Initialize the map view component.

        Args:
            state: Application state to watch.
        """
        params["state"] = state
        super().__init__(**params)

        self._map_pane = pn.pane.HoloViews(
            self._create_empty_map(),
            sizing_mode="stretch_both",
            min_height=400,
        )

        # Watch for location data changes
        state.param.watch(self._update_map, "location_data")

    def _create_empty_map(self):
        """Create an empty map with just the basemap."""
        if not GEOVIEWS_AVAILABLE:
            import holoviews as hv
            return hv.Text(0, 0, "GeoViews not available").opts(
                width=800,
                height=400,
            )

        # Return just the tile source centered on a default location
        tiles = gvts.CartoLight()
        return tiles.opts(
            width=800,
            height=400,
            title="GPS Track Map",
            tools=["pan", "wheel_zoom", "reset"],
            active_tools=["wheel_zoom"],
        )

    def _update_map(self, event=None):
        """Update the map when location data changes."""
        if not GEOVIEWS_AVAILABLE:
            return

        gdf = self.state.location_data

        if gdf is None or gdf.empty:
            self._map_pane.object = self._create_empty_map()
            return

        # Filter out None geometries
        valid_gdf = gdf[gdf.geometry.notna()].copy()

        if valid_gdf.empty:
            self._map_pane.object = self._create_empty_map()
            return

        # Get bounds with 20% margin
        lon_min, lat_min, lon_max, lat_max = valid_gdf.total_bounds

        lon_range = lon_max - lon_min
        lat_range = lat_max - lat_min

        # Handle edge case where range is 0 (single point or same location)
        if lon_range < 0.001:
            lon_range = 0.01
        if lat_range < 0.001:
            lat_range = 0.01

        padding_lon = lon_range * 0.20
        padding_lat = lat_range * 0.20

        lon_min -= padding_lon
        lon_max += padding_lon
        lat_min -= padding_lat
        lat_max += padding_lat

        # Use hvplot for simpler, more reliable geo plotting
        import hvplot.pandas  # noqa

        # Determine color column (use speed if available)
        color_col = None
        if "speed" in valid_gdf.columns and valid_gdf["speed"].notna().any():
            color_col = "speed"

        # Create map using hvplot geo
        # Color-code by speed when available
        plot = valid_gdf.hvplot.points(
            x="longitude",
            y="latitude",
            geo=True,
            tiles="CartoLight",
            c=color_col,
            cmap="viridis" if color_col else None,
            color="red" if not color_col else None,
            size=80,
            alpha=0.8,
            line_color="darkblue",
            line_width=0.5,
            xlim=(lon_min, lon_max),
            ylim=(lat_min, lat_max),
            width=800,
            height=400,
            title=f"GPS Track ({len(valid_gdf)} points)",
            hover_cols=["time", "speed"] if "speed" in valid_gdf.columns else ["time"],
        )

        self._map_pane.object = plot

    def __panel__(self):
        """Return the Panel layout for this component."""
        if not GEOVIEWS_AVAILABLE:
            return pn.pane.Alert(
                "GeoViews is not installed. Install with: pip install geoviews",
                alert_type="warning",
            )

        return pn.Column(
            self._map_pane,
            sizing_mode="stretch_both",
        )


class MapPlaceholder(pn.viewable.Viewer):
    """Placeholder for map when no GPS data is available."""

    def __panel__(self):
        """Return placeholder content."""
        return pn.Column(
            pn.pane.Markdown(
                """
                ## GPS Track Map

                No GPS data loaded for this interval.

                GPS tracks will appear here when `phone_location` data is available.
                """,
                styles={"text-align": "center", "color": "#666"},
            ),
            pn.layout.Spacer(height=200),
            sizing_mode="stretch_both",
            styles={"background": "#f5f5f5", "border-radius": "5px"},
        )
