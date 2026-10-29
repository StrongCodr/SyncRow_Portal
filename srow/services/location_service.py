"""Location service for querying GPS/phone location data.

Provides methods to load and analyze GPS track data stored in InfluxDB.
"""

from datetime import datetime

import certifi
import geopandas as gpd
import influxdb_client
import pandas as pd
from shapely.geometry import Point

from srow.config import Settings


def _escape_flux_string(value: str) -> str:
    """Escape a string value for safe use in Flux queries."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class LocationService:
    """Service for querying GPS location data from InfluxDB.

    Provides methods to fetch and analyze phone_location data
    for GPS tracks and boat positioning.

    Attributes:
        settings: Application settings including InfluxDB connection info.
    """

    MEASUREMENT = "phone_location"
    # Note: actual data uses "lat"/"lon" not "latitude"/"longitude"
    EXPECTED_FIELDS = ["lat", "lon", "altitude", "speed", "accuracy", "bearing"]

    def __init__(self, settings: Settings) -> None:
        """Initialize the location service.

        Args:
            settings: Application settings with InfluxDB connection details.
        """
        self.settings = settings
        self._client = influxdb_client.InfluxDBClient(
            url=settings.url,
            token=settings.token,
            org=settings.effective_org(),
            ssl_ca_cert=certifi.where(),
        )

    def query_api(self) -> influxdb_client.QueryApi:
        """Get the InfluxDB query API."""
        return self._client.query_api()

    def ping(self) -> bool:
        """Check if InfluxDB is reachable."""
        return self._client.ping()

    def load_track(
        self,
        tag_name: str,
        interval_value: str,
        device_id: str | None = None,
    ) -> gpd.GeoDataFrame:
        """Load GPS track as a GeoDataFrame.

        Args:
            tag_name: The tag key to filter on (e.g., "intervalId").
            interval_value: The tag value to match.
            device_id: Optional device ID to filter specific device.

        Returns:
            GeoDataFrame with columns: time, latitude, longitude, altitude,
            speed, accuracy, bearing, geometry, device_id
            Returns empty GeoDataFrame if no data.
        """
        safe_tag = _escape_flux_string(tag_name)
        safe_value = _escape_flux_string(interval_value)
        safe_bucket = _escape_flux_string(self.settings.bucket)

        device_filter = ""
        if device_id:
            safe_device = _escape_flux_string(device_id)
            device_filter = f'|> filter(fn: (r) => r.deviceId == "{safe_device}" or r.device_id == "{safe_device}")'

        flux = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{self.MEASUREMENT}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  {device_filter}
  |> sort(columns: ["_time"])
'''
        tables = self.query_api().query(flux, org=self.settings.effective_org())

        rows = []
        for table in tables:
            for record in table.records:
                vals = record.values
                device = vals.get("deviceId") or vals.get("device_id") or "UNKNOWN"
                rows.append({
                    "time": record["_time"],
                    "field": record["_field"],
                    "value": record["_value"],
                    "device_id": device,
                })

        if not rows:
            return gpd.GeoDataFrame()

        df_long = pd.DataFrame(rows)
        df_wide = df_long.pivot_table(
            index=["time", "device_id"],
            columns="field",
            values="value",
        ).reset_index()

        df_wide = df_wide.sort_values(["time", "device_id"])

        # Handle both naming conventions (lat/lon and latitude/longitude)
        lat_col = "lat" if "lat" in df_wide.columns else "latitude"
        lon_col = "lon" if "lon" in df_wide.columns else "longitude"

        # Ensure required columns exist
        if lat_col not in df_wide.columns or lon_col not in df_wide.columns:
            return gpd.GeoDataFrame()

        # Rename to standard names for consistency
        df_wide = df_wide.rename(columns={lat_col: "latitude", lon_col: "longitude"})

        # Create geometry from lat/lon
        geometry = [
            Point(lon, lat) if pd.notna(lon) and pd.notna(lat) else None
            for lat, lon in zip(df_wide["latitude"], df_wide["longitude"])
        ]

        gdf = gpd.GeoDataFrame(df_wide, geometry=geometry, crs="EPSG:4326")

        return gdf

    def get_time_bounds(
        self,
        tag_name: str,
        interval_value: str,
    ) -> tuple[datetime | None, datetime | None]:
        """Get the time range for location data in an interval.

        Args:
            tag_name: The tag key to filter on.
            interval_value: The tag value to match.

        Returns:
            Tuple of (start_time, end_time) or (None, None) if no data.
        """
        safe_tag = _escape_flux_string(tag_name)
        safe_value = _escape_flux_string(interval_value)
        safe_bucket = _escape_flux_string(self.settings.bucket)

        flux_first = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{self.MEASUREMENT}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> keep(columns: ["_time"])
  |> first()
'''
        flux_last = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{self.MEASUREMENT}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> keep(columns: ["_time"])
  |> last()
'''
        start_time = None
        end_time = None

        try:
            tables_first = self.query_api().query(flux_first, org=self.settings.effective_org())
            for table in tables_first:
                for record in table.records:
                    t = record["_time"]
                    if start_time is None or t < start_time:
                        start_time = t

            tables_last = self.query_api().query(flux_last, org=self.settings.effective_org())
            for table in tables_last:
                for record in table.records:
                    t = record["_time"]
                    if end_time is None or t > end_time:
                        end_time = t
        except Exception:
            pass

        return start_time, end_time

    def calculate_distance(self, gdf: gpd.GeoDataFrame) -> float:
        """Calculate total distance traveled in meters.

        Uses geodesic distance between consecutive points.

        Args:
            gdf: GeoDataFrame with geometry column.

        Returns:
            Total distance in meters. Returns 0 if insufficient data.
        """
        if gdf.empty or len(gdf) < 2:
            return 0.0

        # Project to a meter-based CRS for distance calculation
        # Use UTM zone based on centroid
        centroid = gdf.geometry.union_all().centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        utm_crs = f"+proj=utm +zone={utm_zone} +{hemisphere} +datum=WGS84"

        gdf_utm = gdf.to_crs(utm_crs)

        total_distance = 0.0
        points = gdf_utm.geometry.tolist()

        for i in range(1, len(points)):
            if points[i] is not None and points[i - 1] is not None:
                total_distance += points[i].distance(points[i - 1])

        return total_distance

    def calculate_speed_stats(self, gdf: gpd.GeoDataFrame) -> dict:
        """Calculate speed statistics from GPS data.

        Args:
            gdf: GeoDataFrame with speed column.

        Returns:
            Dict with keys: avg_speed, max_speed, min_speed (in m/s).
            Returns empty dict if no speed data.
        """
        if gdf.empty or "speed" not in gdf.columns:
            return {}

        speeds = gdf["speed"].dropna()
        if speeds.empty:
            return {}

        return {
            "avg_speed": float(speeds.mean()),
            "max_speed": float(speeds.max()),
            "min_speed": float(speeds.min()),
            "std_speed": float(speeds.std()),
        }

    def summarize_track(self, gdf: gpd.GeoDataFrame) -> dict:
        """Generate summary statistics for a GPS track.

        Args:
            gdf: GeoDataFrame with track data.

        Returns:
            Dict with track statistics.
        """
        if gdf.empty:
            return {}

        t_start = gdf["time"].min()
        t_end = gdf["time"].max()
        duration_sec = (t_end - t_start).total_seconds() if t_start and t_end else 0

        stats = {
            "start": t_start,
            "end": t_end,
            "duration_sec": duration_sec,
            "n_points": len(gdf),
            "n_devices": gdf["device_id"].nunique() if "device_id" in gdf.columns else 0,
            "total_distance_m": self.calculate_distance(gdf),
        }

        speed_stats = self.calculate_speed_stats(gdf)
        stats.update(speed_stats)

        return stats
