"""InfluxDB service for querying IMU sensor data.

Provides methods to:
- Fetch available intervals
- Load interval data (full, aggregated, or windowed)
- Summarize interval statistics
- Delete intervals
"""

import json
import logging
import time
from datetime import datetime

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


logger = logging.getLogger(__name__)


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

        # The tag index keeps an interval's id after its rows are deleted, so drop ids with no data.
        live = self.fetch_live_interval_ids()
        if live is not None:
            results = [r for r in results if r["tag"] != "intervalId" or r["value"] in live]

        # Sort by value (timestamp order)
        results = sorted(results, key=lambda x: x["value"])

        # Add index, parsed datetime and the user's name (if one was set)
        names = self.fetch_interval_names()
        for idx, item in enumerate(results, start=1):
            dt_str = _parse_interval_datetime(item["value"])
            item["date"] = dt_str
            item["name"] = names.get(item["value"], "")
            item["label"] = f"{idx}. {item['name'] or dt_str}"

        return results

    LIVE_IDS_TTL_S = 120.0

    def fetch_live_interval_ids(self) -> set[str] | None:
        """Interval ids that still hold data, or None if the query fails.

        schema.tagValues reads the tag index, which keeps a deleted interval's id until the index
        is rebuilt, so the sidebar would list runs that no longer exist. first() is pushed down per
        series, so this reads one point per unit and interval, not the data. Cached briefly because
        the sidebar is rebuilt on every page and after every rename.
        """
        cache = getattr(self, "_live_ids_cache", None)
        if cache and time.monotonic() - cache[0] < self.LIVE_IDS_TTL_S:
            return cache[1]
        flux = f'''
from(bucket: "{_escape_flux_string(self.settings.bucket)}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "imu" or r._measurement == "phone_location")
  |> filter(fn: (r) => r._field == "ax" or r._field == "lat")
  |> first()
  |> keep(columns: ["intervalId"])
  |> group()
  |> distinct(column: "intervalId")
'''
        try:
            tables = self.query_api().query(flux, org=self.settings.effective_org())
        except Exception:
            logger.warning("live interval id query failed; listing the tag index as is", exc_info=True)
            return None
        ids = {str(rec.get_value()) for t in tables for rec in t.records if rec.get_value()}
        self._live_ids_cache = (time.monotonic(), ids)
        return ids

    NAMES_MEASUREMENT = "interval_meta"
    META_FIELDS = ("name", "boat", "profile", "wind_ms", "wind_dir", "stream", "water_temp_c", "conditions")
    PROFILES_MEASUREMENT = "boat_profile"
    DEFAULTS_PROFILE_TAG = "_defaults"

    def fetch_interval_meta(self) -> dict[str, dict]:
        """{intervalId: {field: value}} from the interval_meta measurement: the run's name, boat class,
        boat profile and conditions. The latest point per interval and field wins; blank strings mean
        "cleared" and are dropped. Never raises: a missing measurement gives {}."""
        flux = f'''
from(bucket: "{_escape_flux_string(self.settings.bucket)}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{self.NAMES_MEASUREMENT}")
  |> group(columns: ["intervalId", "_field"])
  |> last()
'''
        meta: dict[str, dict] = {}
        try:
            for table in self.query_api().query(flux, org=self.settings.effective_org()):
                for record in table.records:
                    iv = record.values.get("intervalId"); field = record.values.get("_field"); val = record.get_value()
                    if not iv or field not in self.META_FIELDS or val is None:
                        continue
                    if isinstance(val, str):
                        val = val.strip()
                        if not val:
                            continue
                    meta.setdefault(str(iv), {})[field] = val
        except Exception:  # pragma: no cover - a bucket without the measurement, or a read-only token
            return {}
        return meta

    def fetch_interval_names(self) -> dict[str, str]:
        """{intervalId: name}; see fetch_interval_meta."""
        return {iv: m["name"] for iv, m in self.fetch_interval_meta().items() if m.get("name")}

    def set_interval_meta(self, interval_id: str, **fields) -> None:
        """Record metadata for an interval: one interval_meta point tagged with the id, carrying the
        given fields. Every field is written as a string (numbers too), so a field's type never changes
        between points and an empty string clears it on read; None skips a field.
        The app writes the same measurement, so a name or boat class set on either side shows on both."""
        from influxdb_client import Point, WritePrecision
        from influxdb_client.client.write_api import SYNCHRONOUS
        point = Point(self.NAMES_MEASUREMENT).tag("intervalId", interval_id)
        n = 0
        for k, v in fields.items():
            if k not in self.META_FIELDS or v is None:
                continue
            point = point.field(k, str(v).strip()); n += 1
        if n == 0:
            return
        point = point.time(datetime.now(tz=__import__("datetime").timezone.utc), WritePrecision.MS)
        self._client.write_api(write_options=SYNCHRONOUS).write(
            bucket=self.settings.bucket, org=self.settings.effective_org(), record=point)

    def set_interval_name(self, interval_id: str, name: str) -> None:
        """Record a name for an interval (an empty name clears it)."""
        self.set_interval_meta(interval_id, name=name)

    def fetch_boat_profiles(self):
        """(profiles, defaults): the user's boat profiles {id: BoatProfile} including deleted ones, and the
        default profile id per class {cls: id}. One JSON document per profile in the boat_profile
        measurement, tagged with the profile id; the latest point wins. Never raises."""
        from srow.boats import BoatProfile
        flux = f'''
from(bucket: "{_escape_flux_string(self.settings.bucket)}")
  |> range(start: {self.settings.query_lookback})
  |> filter(fn: (r) => r._measurement == "{self.PROFILES_MEASUREMENT}" and r._field == "json")
  |> group(columns: ["profile"])
  |> last()
'''
        profiles: dict = {}; defaults: dict[str, str] = {}
        try:
            for table in self.query_api().query(flux, org=self.settings.effective_org()):
                for record in table.records:
                    pid = record.values.get("profile"); raw = record.get_value()
                    if not pid or not isinstance(raw, str):
                        continue
                    try:
                        doc = json.loads(raw)
                    except ValueError:
                        continue
                    if pid == self.DEFAULTS_PROFILE_TAG:
                        defaults = {str(k): str(v) for k, v in (doc or {}).items() if v}
                    else:
                        profiles[str(pid)] = BoatProfile.from_json(str(pid), doc or {})
        except Exception:  # pragma: no cover
            return {}, {}
        return profiles, defaults

    def _write_profile_doc(self, profile_id: str, doc: dict) -> None:
        from influxdb_client import Point, WritePrecision
        from influxdb_client.client.write_api import SYNCHRONOUS
        point = (Point(self.PROFILES_MEASUREMENT).tag("profile", profile_id).field("json", json.dumps(doc))
                 .time(datetime.now(tz=__import__("datetime").timezone.utc), WritePrecision.MS))
        self._client.write_api(write_options=SYNCHRONOUS).write(
            bucket=self.settings.bucket, org=self.settings.effective_org(), record=point)

    def save_boat_profile(self, profile) -> None:
        """Write a boat profile (whole document; the latest point wins)."""
        self._write_profile_doc(profile.id, profile.to_json())

    def set_default_profiles(self, defaults: dict[str, str]) -> None:
        """Write the default profile id per class (whole mapping; the latest point wins)."""
        self._write_profile_doc(self.DEFAULTS_PROFILE_TAG, dict(defaults))

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

    def load_series(
        self,
        tag_name: str,
        interval_value: str,
        start: "datetime",
        stop: "datetime",
        window: str | None,
        measurement: str = "imu",
        fields: tuple[str, ...] | None = None,
    ) -> pd.DataFrame:
        """Load one time range of an interval, raw (window=None) or mean-aggregated per `window`.

        Returns a wide DataFrame: time, source, <fields>. Used by the resolution-adaptive
        chart endpoint; the window is chosen by the caller from the visible span and pixel width.
        """
        safe_tag = _escape_flux_string(tag_name)
        safe_value = _escape_flux_string(interval_value)
        safe_measurement = _escape_flux_string(measurement)
        safe_bucket = _escape_flux_string(self.settings.bucket)
        field_filter = ""
        if fields:
            ors = " or ".join(f'r._field == "{_escape_flux_string(f)}"' for f in fields)
            field_filter = f"|> filter(fn: (r) => {ors})"
        agg = f"|> aggregateWindow(every: {window}, fn: mean, createEmpty: false)" if window else ""
        flux = f'''
from(bucket: "{safe_bucket}")
  |> range(start: {start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]}Z, stop: {stop.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]}Z)
  |> filter(fn: (r) => r._measurement == "{safe_measurement}")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  {field_filter}
  {agg}
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
        df = pd.DataFrame(rows).pivot_table(index=["time", "source"], columns="field", values="value").reset_index()
        return df.sort_values(["source", "time"]).reset_index(drop=True)

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
