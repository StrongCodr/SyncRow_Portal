"""InfluxDB service for querying IMU sensor data.

Provides methods to:
- Fetch available intervals
- Load interval data (full, aggregated, or windowed)
- Summarize interval statistics
- Delete intervals
"""

from datetime import datetime
from typing import Iterator

import certifi
import influxdb_client
import pandas as pd

from srow.config import Settings


def format_influx_error(err: Exception) -> str:
    """Format an InfluxDB error for display.

    Extracts common error attributes (status, reason, message, body)
    and combines them into a readable string.

    Args:
        err: The exception to format.

    Returns:
        Formatted error string.
    """
    parts = [err.__class__.__name__]
    for name in ("status", "reason", "message"):
        val = getattr(err, name, None)
        if val:
            parts.append(f"{name}={val}")
    text = str(err)
    if text and text not in parts:
        parts.append(text)
    return "; ".join(parts)


def is_delete_not_supported(err: Exception) -> bool:
    """Check if error indicates delete is not supported.

    InfluxDB serverless v3 buckets don't support delete ranges.

    Args:
        err: The exception to check.

    Returns:
        True if the error indicates delete is not supported.
    """
    return "deletes ranges are not supported for serverless v3 buckets" in str(err).lower()


def _escape_flux_string(value: str) -> str:
    """Escape a string value for safe use in Flux queries.

    Prevents Flux injection by escaping special characters.

    Args:
        value: The string to escape.

    Returns:
        Escaped string safe for Flux queries.
    """
    # Escape backslashes first, then quotes
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _parse_interval_datetime(interval_value: str) -> str:
    """Parse datetime from interval name like 'Interval_1767410242328'.

    Args:
        interval_value: Interval identifier containing Unix timestamp in ms.

    Returns:
        Formatted datetime string, or original value if parsing fails.
    """
    import re

    # Extract numeric part (Unix timestamp in milliseconds)
    match = re.search(r"(\d{13})", interval_value)
    if match:
        try:
            ts_ms = int(match.group(1))
            dt = datetime.fromtimestamp(ts_ms / 1000)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            pass

    return interval_value


class InfluxService:
    """Service for querying IMU data from InfluxDB.

    Provides methods to fetch, filter, and analyze IMU sensor data
    stored in InfluxDB time-series database.

    Attributes:
        settings: Application settings including InfluxDB connection info.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the InfluxDB service.

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
        """Get the InfluxDB query API.

        Returns:
            InfluxDB QueryApi instance.
        """
        return self._client.query_api()

    def delete_api(self) -> influxdb_client.DeleteApi:
        """Get the InfluxDB delete API.

        Returns:
            InfluxDB DeleteApi instance.
        """
        return self._client.delete_api()

    def ping(self) -> bool:
        """Check if InfluxDB is reachable.

        Returns:
            True if the server responds to ping.
        """
        return self._client.ping()

    def fetch_interval_tags(self) -> list[dict]:
        """Fetch all available interval identifiers.

        Queries for interval tags (intervalId, interval_id, intervalName)
        and returns a sorted list of unique intervals with parsed datetime labels.

        Returns:
            List of dicts with keys: tag, value, label
        """
        tag_keys = ["intervalId", "interval_id", "intervalName"]
        results = []
        seen = set()

        for tag in tag_keys:
            flux = f'''
import "influxdata/influxdb/schema"
schema.tagValues(bucket: "{_escape_flux_string(self.settings.bucket)}", tag: "{tag}", start: {self.settings.query_lookback})
'''
            tables = self.query_api().query(flux, org=self.settings.effective_org())
            for table in tables:
                for record in table.records:
                    val = record.get_value()
                    if val is None or val == "":
                        continue
                    key = (tag, str(val))
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({"tag": tag, "value": str(val)})

        # Sort by value (timestamp order)
        results = sorted(results, key=lambda x: x["value"])

        # Add index and parsed datetime labels
        for idx, item in enumerate(results, start=1):
            dt_str = _parse_interval_datetime(item["value"])
            item["label"] = f"{idx}. {dt_str}"

        return results

    def load_interval(
        self,
        tag_name: str,
        interval_value: str,
        measurement: str = "imu",
    ) -> pd.DataFrame:
        """Load all data for a specific interval.

        Args:
            tag_name: The tag key to filter on (e.g., "intervalId").
            interval_value: The tag value to match.
            measurement: The measurement to query. Defaults to "imu".

        Returns:
            DataFrame with columns: time, source, and sensor fields (ax, ay, etc).
            Returns empty DataFrame if no data found.
        """
        safe_tag = _escape_flux_string(tag_name)
        safe_value = _escape_flux_string(interval_value)
        safe_measurement = _escape_flux_string(measurement)
        safe_bucket = _escape_flux_string(self.settings.bucket)

        flux = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{safe_measurement}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> sort(columns: ["_time"])
'''
        tables = self.query_api().query(flux, org=self.settings.effective_org())

        rows = []
        for table in tables:
            for record in table.records:
                vals = record.values
                rows.append({
                    "time": record["_time"],
                    "field": record["_field"],
                    "value": record["_value"],
                    "source": self._source_label(vals),
                })

        if not rows:
            return pd.DataFrame()

        df_long = pd.DataFrame(rows)
        df_wide = df_long.pivot_table(
            index=["time", "source"],
            columns="field",
            values="value",
        ).reset_index()

        return df_wide.sort_values(["time", "source"])

    def load_interval_aggregated(
        self,
        tag_name: str,
        interval_value: str,
        window: str = "100ms",
        aggregation: str = "mean",
        measurement: str = "imu",
    ) -> pd.DataFrame:
        """Load aggregated data for a specific interval.

        Pre-aggregates data in InfluxDB to reduce data transfer and enable
        faster initial rendering of large datasets.

        Args:
            tag_name: The tag key to filter on.
            interval_value: The tag value to match.
            window: Aggregation window (e.g., "100ms", "1s"). Defaults to "100ms".
            aggregation: Aggregation function (mean, max, min). Defaults to "mean".
            measurement: The measurement to query. Defaults to "imu".

        Returns:
            Aggregated DataFrame with reduced row count.
        """
        safe_tag = _escape_flux_string(tag_name)
        safe_value = _escape_flux_string(interval_value)
        safe_measurement = _escape_flux_string(measurement)
        safe_bucket = _escape_flux_string(self.settings.bucket)

        flux = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{safe_measurement}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> aggregateWindow(every: {window}, fn: {aggregation}, createEmpty: false)
  |> sort(columns: ["_time"])
'''
        tables = self.query_api().query(flux, org=self.settings.effective_org())

        rows = []
        for table in tables:
            for record in table.records:
                vals = record.values
                rows.append({
                    "time": record["_time"],
                    "field": record["_field"],
                    "value": record["_value"],
                    "source": self._source_label(vals),
                })

        if not rows:
            return pd.DataFrame()

        df_long = pd.DataFrame(rows)
        df_wide = df_long.pivot_table(
            index=["time", "source"],
            columns="field",
            values="value",
        ).reset_index()

        return df_wide.sort_values(["time", "source"])

    def get_time_bounds(
        self,
        tag_name: str,
        interval_value: str,
        measurement: str = "imu",
    ) -> tuple[datetime | None, datetime | None]:
        """Get the time range for an interval without loading all data.

        Args:
            tag_name: The tag key to filter on.
            interval_value: The tag value to match.
            measurement: The measurement to query. Defaults to "imu".

        Returns:
            Tuple of (start_time, end_time) or (None, None) if no data.
        """
        safe_tag = _escape_flux_string(tag_name)
        safe_value = _escape_flux_string(interval_value)
        safe_measurement = _escape_flux_string(measurement)
        safe_bucket = _escape_flux_string(self.settings.bucket)

        flux = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{safe_measurement}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> keep(columns: ["_time"])
  |> first()
'''
        tables_first = self.query_api().query(flux, org=self.settings.effective_org())

        flux_last = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{safe_measurement}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> keep(columns: ["_time"])
  |> last()
'''
        tables_last = self.query_api().query(flux_last, org=self.settings.effective_org())

        start_time = None
        end_time = None

        for table in tables_first:
            for record in table.records:
                t = record["_time"]
                if start_time is None or t < start_time:
                    start_time = t

        for table in tables_last:
            for record in table.records:
                t = record["_time"]
                if end_time is None or t > end_time:
                    end_time = t

        return start_time, end_time

    def load_time_range(
        self,
        tag_name: str,
        interval_value: str,
        start: datetime,
        end: datetime,
        measurement: str = "imu",
    ) -> pd.DataFrame:
        """Load data for a specific time window within an interval.

        Used when user zooms into a specific region for detailed view.

        Args:
            tag_name: The tag key to filter on.
            interval_value: The tag value to match.
            start: Start of time window.
            end: End of time window.
            measurement: The measurement to query. Defaults to "imu".

        Returns:
            DataFrame with data only within the specified time range.
        """
        safe_tag = _escape_flux_string(tag_name)
        safe_value = _escape_flux_string(interval_value)
        safe_measurement = _escape_flux_string(measurement)
        safe_bucket = _escape_flux_string(self.settings.bucket)

        # Format timestamps for Flux
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        flux = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {start_str}, stop: {end_str})
  |> filter(fn: (r) => r._measurement == "{safe_measurement}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> sort(columns: ["_time"])
'''
        tables = self.query_api().query(flux, org=self.settings.effective_org())

        rows = []
        for table in tables:
            for record in table.records:
                vals = record.values
                rows.append({
                    "time": record["_time"],
                    "field": record["_field"],
                    "value": record["_value"],
                    "source": self._source_label(vals),
                })

        if not rows:
            return pd.DataFrame()

        df_long = pd.DataFrame(rows)
        df_wide = df_long.pivot_table(
            index=["time", "source"],
            columns="field",
            values="value",
        ).reset_index()

        return df_wide.sort_values(["time", "source"])

    def load_interval_chunked(
        self,
        tag_name: str,
        interval_value: str,
        chunk_size: int = 50000,
        measurement: str = "imu",
    ) -> Iterator[pd.DataFrame]:
        """Load interval data in chunks for memory efficiency.

        Yields DataFrames in chunks to avoid loading entire dataset into memory.

        Args:
            tag_name: The tag key to filter on.
            interval_value: The tag value to match.
            chunk_size: Maximum rows per chunk. Defaults to 50000.
            measurement: The measurement to query. Defaults to "imu".

        Yields:
            DataFrame chunks with sensor data.
        """
        # Get time bounds first
        start, end = self.get_time_bounds(tag_name, interval_value, measurement)
        if start is None or end is None:
            return

        # For now, just load full data and chunk it
        # Future optimization: use streaming query
        df = self.load_interval(tag_name, interval_value, measurement)

        if df.empty:
            return

        for i in range(0, len(df), chunk_size):
            yield df.iloc[i:i + chunk_size].copy()

    def unwrap_angles(self, df: pd.DataFrame, fields: list[str]) -> pd.DataFrame:
        """Unwrap angle fields to remove 360-degree discontinuities.

        Applies angle unwrapping per source to maintain continuous angle
        progression for roll, pitch, yaw fields.

        Args:
            df: DataFrame with angle columns.
            fields: List of column names to unwrap.

        Returns:
            DataFrame with unwrapped angles (copy of input).
        """
        if df.empty:
            return df

        df_copy = df.copy()
        for field in fields:
            if field not in df_copy.columns:
                continue
            df_copy[field] = (
                df_copy.groupby("source")[field]
                .transform(self._unwrap_angle_series)
            )
        return df_copy

    def summarize_interval(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate summary statistics for an interval.

        Calculates per-source statistics including duration, sample count,
        and approximate sampling frequency.

        Args:
            df: DataFrame with time and source columns.

        Returns:
            DataFrame with summary statistics per source.
        """
        if df.empty:
            return pd.DataFrame()

        def summarize(group: pd.DataFrame) -> pd.Series:
            t_start = group["time"].min()
            t_end = group["time"].max()
            duration_sec = (t_end - t_start).total_seconds()
            n_samples = len(group)
            hz = n_samples / duration_sec if duration_sec > 0 else float("nan")
            return pd.Series({
                "start": t_start,
                "end": t_end,
                "duration_sec": duration_sec,
                "samples": n_samples,
                "approx_hz": hz,
            })

        return df.groupby("source").apply(summarize).reset_index()

    def delete_interval(self, tag_name: str, interval_value: str) -> None:
        """Delete all data for a specific interval.

        Args:
            tag_name: The tag key to filter on.
            interval_value: The tag value to match.

        Note:
            May raise an error on serverless v3 buckets which don't support deletes.
        """
        start = "1970-01-01T00:00:00Z"
        stop = "2100-01-01T00:00:00Z"
        safe_value = _escape_flux_string(interval_value)
        predicate = f'_measurement="imu" AND {tag_name}="{safe_value}"'
        self.delete_api().delete(
            start=start,
            stop=stop,
            predicate=predicate,
            bucket=self.settings.bucket,
            org=self.settings.effective_org(),
        )

    def delete_all_intervals(self) -> None:
        """Delete all interval data from the bucket.

        Warning:
            This is a destructive operation. Use with caution.
            May raise an error on serverless v3 buckets.
        """
        start = "1970-01-01T00:00:00Z"
        stop = "2100-01-01T00:00:00Z"
        predicate = '_measurement="imu" AND interval_id!=""'
        self.delete_api().delete(
            start=start,
            stop=stop,
            predicate=predicate,
            bucket=self.settings.bucket,
            org=self.settings.effective_org(),
        )

    @staticmethod
    def _source_label(record_values: dict) -> str:
        """Create a display label from sensor/seat metadata.

        Args:
            record_values: Dictionary of record values from InfluxDB.

        Returns:
            Human-readable source label.
        """
        sensor = record_values.get("sensorId") or record_values.get("sensor_id")
        seat = record_values.get("seat")
        if sensor and seat:
            return f"{sensor} ({seat})"
        if sensor:
            return sensor
        if seat:
            return seat
        return "UNKNOWN"

    @staticmethod
    def _unwrap_angle_series(series: pd.Series) -> pd.Series:
        """Unwrap a single angle series to remove discontinuities.

        Handles jumps greater than 180 degrees by adding/subtracting 360.

        Args:
            series: Pandas Series of angle values in degrees.

        Returns:
            Unwrapped angle series.
        """
        if series.empty:
            return series

        out = series.copy()
        prev = out.iloc[0]

        for i in range(1, len(out)):
            val = out.iloc[i]
            if pd.isna(val) or pd.isna(prev):
                prev = val
                continue

            diff = val - prev
            while diff > 180:
                val -= 360
                diff = val - prev
            while diff < -180:
                val += 360
                diff = val - prev

            out.iloc[i] = val
            prev = val

        return out
