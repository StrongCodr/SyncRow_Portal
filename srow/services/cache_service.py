"""Local Parquet cache for InfluxDB data.

Stores raw data at whatever sampling rate was recorded.
Aggregation/downsampling happens at display time, not cache time.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .influx_service import InfluxService
from .location_service import LocationService


class CacheService:
    """Local Parquet cache for InfluxDB data.

    Stores RAW data at whatever sampling rate was recorded.
    Aggregation/downsampling happens at display time, not cache time.

    Attributes:
        influx: InfluxDB service for IMU data.
        location: Location service for GPS data.
        cache_dir: Path to local cache directory.
    """

    DEFAULT_CACHE_DIR = Path.home() / ".srow" / "cache"

    def __init__(
        self,
        influx_service: InfluxService,
        location_service: LocationService,
        cache_dir: Path | str | None = None,
    ) -> None:
        """Initialize the cache service.

        Args:
            influx_service: InfluxDB service for IMU queries.
            location_service: Location service for GPS queries.
            cache_dir: Path to cache directory. Defaults to ~/.srow/cache.
        """
        self.influx = influx_service
        self.location = location_service
        self.cache_dir = Path(cache_dir) if cache_dir else self.DEFAULT_CACHE_DIR

        # Ensure cache directories exist
        self._imu_dir.mkdir(parents=True, exist_ok=True)
        self._location_dir.mkdir(parents=True, exist_ok=True)

        self.meta = self._load_meta()

    @property
    def _meta_path(self) -> Path:
        """Path to meta.json file."""
        return self.cache_dir / "meta.json"

    @property
    def _imu_dir(self) -> Path:
        """Path to IMU parquet directory."""
        return self.cache_dir / "imu"

    @property
    def _location_dir(self) -> Path:
        """Path to location parquet directory."""
        return self.cache_dir / "phone_location"

    def _load_meta(self) -> dict:
        """Load meta.json or return empty structure."""
        if self._meta_path.exists():
            with open(self._meta_path) as f:
                return json.load(f)
        return {"last_sync": None, "intervals": {}}

    def _save_meta(self) -> None:
        """Persist meta.json."""
        with open(self._meta_path, "w") as f:
            json.dump(self.meta, f, indent=2, default=str)

    def _interval_key(self, interval: dict) -> str:
        """Get unique key for an interval."""
        return interval["value"]

    def _imu_path(self, interval: dict) -> Path:
        """Get parquet path for IMU data."""
        return self._imu_dir / f"{self._interval_key(interval)}.parquet"

    def _location_path(self, interval: dict) -> Path:
        """Get parquet path for location data."""
        return self._location_dir / f"{self._interval_key(interval)}.parquet"

    def sync_interval_list(self) -> list[dict]:
        """Fetch interval list from cloud, update local meta, delete stale.

        Returns list of intervals with cache status added.
        Deletes local parquet files for intervals no longer in cloud.

        Returns:
            List of interval dicts with cache status.
        """
        # Fetch current intervals from cloud
        cloud_intervals = self.influx.fetch_interval_tags()
        cloud_keys = {i["value"] for i in cloud_intervals}

        # Find stale cached intervals (in local but not in cloud)
        stale_keys = set(self.meta["intervals"].keys()) - cloud_keys

        # Delete stale local files
        for key in stale_keys:
            imu_file = self._imu_dir / f"{key}.parquet"
            loc_file = self._location_dir / f"{key}.parquet"
            if imu_file.exists():
                imu_file.unlink()
            if loc_file.exists():
                loc_file.unlink()
            del self.meta["intervals"][key]

        # Update last sync time
        self.meta["last_sync"] = datetime.now(timezone.utc).isoformat()

        # Ensure all cloud intervals are in meta (may not be cached yet)
        for interval in cloud_intervals:
            key = self._interval_key(interval)
            if key not in self.meta["intervals"]:
                self.meta["intervals"][key] = {
                    "tag": interval["tag"],
                    "value": interval["value"],
                    "label": interval["label"],
                    "imu_cached": False,
                    "imu_rows": 0,
                    "location_cached": False,
                    "location_rows": 0,
                    "cached_at": None,
                }

        self._save_meta()

        # Add cache status to returned intervals
        result = []
        for interval in cloud_intervals:
            key = self._interval_key(interval)
            meta_entry = self.meta["intervals"].get(key, {})
            interval_with_status = {
                **interval,
                "imu_cached": meta_entry.get("imu_cached", False),
                "location_cached": meta_entry.get("location_cached", False),
            }
            result.append(interval_with_status)

        return result

    def get_imu_data(self, interval: dict) -> pd.DataFrame:
        """Get RAW IMU data - from cache if available, else fetch & cache.

        Returns full-resolution data (whatever rate was recorded).
        Caller can downsample for display if needed.

        Args:
            interval: Interval dict with tag and value.

        Returns:
            DataFrame with raw IMU data.
        """
        parquet_path = self._imu_path(interval)

        # Check cache
        if parquet_path.exists():
            return pd.read_parquet(parquet_path)

        # Fetch from cloud and cache
        return self._fetch_and_cache_imu(interval)

    def get_location_data(self, interval: dict) -> gpd.GeoDataFrame:
        """Get RAW GPS data - from cache if available, else fetch & cache.

        Returns full-resolution data (whatever rate was recorded).

        Args:
            interval: Interval dict with tag and value.

        Returns:
            GeoDataFrame with raw GPS data.
        """
        parquet_path = self._location_path(interval)

        # Check cache
        if parquet_path.exists():
            df = pd.read_parquet(parquet_path)
            if df.empty:
                return gpd.GeoDataFrame()
            # Reconstruct GeoDataFrame from parquet
            return self._dataframe_to_geodataframe(df)

        # Fetch from cloud and cache
        return self._fetch_and_cache_location(interval)

    def is_cached(self, interval: dict) -> dict:
        """Check cache status for an interval.

        Args:
            interval: Interval dict with tag and value.

        Returns:
            Dict with keys: imu, location, imu_rows, location_rows
        """
        key = self._interval_key(interval)
        meta_entry = self.meta["intervals"].get(key, {})

        return {
            "imu": meta_entry.get("imu_cached", False),
            "location": meta_entry.get("location_cached", False),
            "imu_rows": meta_entry.get("imu_rows", 0),
            "location_rows": meta_entry.get("location_rows", 0),
        }

    def clear_cache(self) -> None:
        """Delete all local cache files and reset meta."""
        if self._imu_dir.exists():
            shutil.rmtree(self._imu_dir)
        if self._location_dir.exists():
            shutil.rmtree(self._location_dir)

        # Recreate directories
        self._imu_dir.mkdir(parents=True, exist_ok=True)
        self._location_dir.mkdir(parents=True, exist_ok=True)

        # Reset meta
        self.meta = {"last_sync": None, "intervals": {}}
        self._save_meta()

    def get_cache_stats(self) -> dict:
        """Return cache statistics.

        Returns:
            Dict with keys: intervals_cached, total_size_mb, imu_files, location_files
        """
        imu_files = list(self._imu_dir.glob("*.parquet"))
        loc_files = list(self._location_dir.glob("*.parquet"))

        total_bytes = sum(f.stat().st_size for f in imu_files)
        total_bytes += sum(f.stat().st_size for f in loc_files)

        # Count intervals with at least one cached file
        cached_intervals = sum(
            1 for entry in self.meta["intervals"].values()
            if entry.get("imu_cached") or entry.get("location_cached")
        )

        return {
            "intervals_cached": cached_intervals,
            "total_size_mb": round(total_bytes / (1024 * 1024), 2),
            "imu_files": len(imu_files),
            "location_files": len(loc_files),
        }

    def _fetch_and_cache_imu(self, interval: dict) -> pd.DataFrame:
        """Fetch raw IMU from cloud and save to parquet.

        Args:
            interval: Interval dict with tag and value.

        Returns:
            DataFrame with IMU data.
        """
        # Fetch full raw data from cloud
        df = self.influx.load_interval(
            tag_name=interval["tag"],
            interval_value=interval["value"],
            measurement="imu",
        )

        # Save to parquet
        parquet_path = self._imu_path(interval)
        if not df.empty:
            df.to_parquet(parquet_path, index=False)

        # Update meta
        key = self._interval_key(interval)
        if key not in self.meta["intervals"]:
            self.meta["intervals"][key] = {
                "tag": interval["tag"],
                "value": interval["value"],
                "label": interval.get("label", interval["value"]),
            }

        self.meta["intervals"][key]["imu_cached"] = True
        self.meta["intervals"][key]["imu_rows"] = len(df)
        self.meta["intervals"][key]["cached_at"] = datetime.now(timezone.utc).isoformat()
        self._save_meta()

        return df

    def _fetch_and_cache_location(self, interval: dict) -> gpd.GeoDataFrame:
        """Fetch raw GPS from cloud and save to parquet.

        Args:
            interval: Interval dict with tag and value.

        Returns:
            GeoDataFrame with GPS data.
        """
        # Fetch full raw data from cloud
        gdf = self.location.load_track(
            tag_name=interval["tag"],
            interval_value=interval["value"],
        )

        # Save to parquet (convert geometry to WKT for storage)
        parquet_path = self._location_path(interval)
        if not gdf.empty:
            df_for_storage = self._geodataframe_to_dataframe(gdf)
            df_for_storage.to_parquet(parquet_path, index=False)

        # Update meta
        key = self._interval_key(interval)
        if key not in self.meta["intervals"]:
            self.meta["intervals"][key] = {
                "tag": interval["tag"],
                "value": interval["value"],
                "label": interval.get("label", interval["value"]),
            }

        self.meta["intervals"][key]["location_cached"] = True
        self.meta["intervals"][key]["location_rows"] = len(gdf)
        self.meta["intervals"][key]["cached_at"] = datetime.now(timezone.utc).isoformat()
        self._save_meta()

        return gdf

    @staticmethod
    def _geodataframe_to_dataframe(gdf: gpd.GeoDataFrame) -> pd.DataFrame:
        """Convert GeoDataFrame to regular DataFrame for parquet storage.

        Stores geometry as WKT string.
        """
        df = pd.DataFrame(gdf.drop(columns=["geometry"]))
        df["geometry_wkt"] = gdf.geometry.apply(
            lambda g: g.wkt if g is not None else None
        )
        return df

    @staticmethod
    def _dataframe_to_geodataframe(df: pd.DataFrame) -> gpd.GeoDataFrame:
        """Convert DataFrame from parquet back to GeoDataFrame."""
        from shapely import wkt

        geometry = df["geometry_wkt"].apply(
            lambda w: wkt.loads(w) if w is not None else None
        )
        df_clean = df.drop(columns=["geometry_wkt"])
        return gpd.GeoDataFrame(df_clean, geometry=geometry, crs="EPSG:4326")
