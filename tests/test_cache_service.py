"""Tests for the CacheService."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from srow.services.cache_service import CacheService


@pytest.fixture
def mock_influx_service():
    """Create a mock InfluxService."""
    mock = MagicMock()
    mock.fetch_interval_tags.return_value = [
        {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"},
        {"tag": "intervalId", "value": "Interval_002", "label": "Interval_002"},
    ]
    mock.load_interval.return_value = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=100, freq="100ms"),
        "source": ["sensor1"] * 100,
        "ax": range(100),
        "ay": range(100, 200),
        "az": range(200, 300),
    })
    return mock


@pytest.fixture
def mock_location_service():
    """Create a mock LocationService."""
    mock = MagicMock()
    gdf = gpd.GeoDataFrame({
        "time": pd.date_range("2024-01-01", periods=10, freq="1s"),
        "device_id": ["device1"] * 10,
        "latitude": [24.5 + i * 0.001 for i in range(10)],
        "longitude": [54.4 + i * 0.001 for i in range(10)],
        "speed": [2.0] * 10,
    })
    gdf["geometry"] = [Point(lon, lat) for lat, lon in zip(gdf["latitude"], gdf["longitude"])]
    gdf = gdf.set_crs("EPSG:4326")
    mock.load_track.return_value = gdf
    return mock


@pytest.fixture
def temp_cache_dir(tmp_path):
    """Create a temporary cache directory."""
    return tmp_path / "cache"


class TestCacheService:
    """Tests for CacheService."""

    def test_init_creates_directories(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test that initialization creates cache directories."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)

        assert (temp_cache_dir / "imu").exists()
        assert (temp_cache_dir / "phone_location").exists()

    def test_load_meta_empty(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test loading meta when no file exists."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)

        assert cache.meta == {"last_sync": None, "intervals": {}}

    def test_load_meta_existing(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test loading existing meta file."""
        temp_cache_dir.mkdir(parents=True, exist_ok=True)
        meta_path = temp_cache_dir / "meta.json"
        meta_data = {
            "last_sync": "2024-01-01T00:00:00Z",
            "intervals": {
                "Interval_001": {
                    "tag": "intervalId",
                    "value": "Interval_001",
                    "imu_cached": True,
                }
            }
        }
        with open(meta_path, "w") as f:
            json.dump(meta_data, f)

        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)

        assert cache.meta["last_sync"] == "2024-01-01T00:00:00Z"
        assert "Interval_001" in cache.meta["intervals"]

    def test_sync_interval_list(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test syncing interval list from cloud."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)

        intervals = cache.sync_interval_list()

        assert len(intervals) == 2
        assert intervals[0]["value"] == "Interval_001"
        assert "imu_cached" in intervals[0]
        assert cache.meta["last_sync"] is not None
        mock_influx_service.fetch_interval_tags.assert_called_once()

    def test_sync_interval_list_deletes_stale(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test that sync deletes stale local files."""
        # Create cache with a stale interval
        temp_cache_dir.mkdir(parents=True, exist_ok=True)
        (temp_cache_dir / "imu").mkdir()
        (temp_cache_dir / "phone_location").mkdir()

        # Create stale parquet file
        stale_file = temp_cache_dir / "imu" / "Stale_Interval.parquet"
        stale_file.touch()

        # Set up meta with stale interval
        meta_path = temp_cache_dir / "meta.json"
        with open(meta_path, "w") as f:
            json.dump({
                "last_sync": None,
                "intervals": {
                    "Stale_Interval": {"imu_cached": True}
                }
            }, f)

        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        cache.sync_interval_list()

        # Stale file should be deleted
        assert not stale_file.exists()
        assert "Stale_Interval" not in cache.meta["intervals"]

    def test_get_imu_data_cache_miss(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test fetching IMU data on cache miss."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        df = cache.get_imu_data(interval)

        assert len(df) == 100
        assert "ax" in df.columns
        mock_influx_service.load_interval.assert_called_once()

        # Verify parquet file was created
        parquet_path = temp_cache_dir / "imu" / "Interval_001.parquet"
        assert parquet_path.exists()

    def test_get_imu_data_cache_hit(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test loading IMU data from cache."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        # First call - cache miss
        df1 = cache.get_imu_data(interval)
        call_count_1 = mock_influx_service.load_interval.call_count

        # Second call - should hit cache
        df2 = cache.get_imu_data(interval)
        call_count_2 = mock_influx_service.load_interval.call_count

        assert call_count_1 == 1
        assert call_count_2 == 1  # No additional call
        assert len(df1) == len(df2)

    def test_get_location_data_cache_miss(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test fetching location data on cache miss."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        gdf = cache.get_location_data(interval)

        assert len(gdf) == 10
        assert "latitude" in gdf.columns
        mock_location_service.load_track.assert_called_once()

        # Verify parquet file was created
        parquet_path = temp_cache_dir / "phone_location" / "Interval_001.parquet"
        assert parquet_path.exists()

    def test_get_location_data_cache_hit(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test loading location data from cache."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        # First call - cache miss
        gdf1 = cache.get_location_data(interval)
        call_count_1 = mock_location_service.load_track.call_count

        # Second call - should hit cache
        gdf2 = cache.get_location_data(interval)
        call_count_2 = mock_location_service.load_track.call_count

        assert call_count_1 == 1
        assert call_count_2 == 1  # No additional call
        assert len(gdf1) == len(gdf2)

    def test_is_cached(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test checking cache status."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        # Initially not cached
        status = cache.is_cached(interval)
        assert status["imu"] is False
        assert status["location"] is False

        # Cache IMU data
        cache.get_imu_data(interval)

        status = cache.is_cached(interval)
        assert status["imu"] is True
        assert status["location"] is False
        assert status["imu_rows"] == 100

    def test_clear_cache(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test clearing all cache files."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        # Create some cached data
        cache.get_imu_data(interval)
        cache.get_location_data(interval)

        # Verify files exist
        assert (temp_cache_dir / "imu" / "Interval_001.parquet").exists()
        assert (temp_cache_dir / "phone_location" / "Interval_001.parquet").exists()

        # Clear cache
        cache.clear_cache()

        # Verify files are gone
        assert not (temp_cache_dir / "imu" / "Interval_001.parquet").exists()
        assert not (temp_cache_dir / "phone_location" / "Interval_001.parquet").exists()
        assert cache.meta == {"last_sync": None, "intervals": {}}

    def test_get_cache_stats(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test getting cache statistics."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        # Initially empty
        stats = cache.get_cache_stats()
        assert stats["intervals_cached"] == 0
        assert stats["imu_files"] == 0

        # Cache some data
        cache.get_imu_data(interval)
        cache.get_location_data(interval)

        stats = cache.get_cache_stats()
        assert stats["intervals_cached"] == 1
        assert stats["imu_files"] == 1
        assert stats["location_files"] == 1
        assert stats["total_size_mb"] > 0

    def test_geodataframe_roundtrip(self, mock_influx_service, mock_location_service, temp_cache_dir):
        """Test that GeoDataFrame survives parquet roundtrip."""
        cache = CacheService(mock_influx_service, mock_location_service, temp_cache_dir)
        interval = {"tag": "intervalId", "value": "Interval_001", "label": "Interval_001"}

        # First load (fetches and caches)
        gdf1 = cache.get_location_data(interval)

        # Second load (from cache)
        gdf2 = cache.get_location_data(interval)

        # Check geometry is preserved
        assert gdf2.crs == "EPSG:4326"
        assert gdf2.geometry.iloc[0] is not None
        assert gdf2.geometry.iloc[0].x == gdf1.geometry.iloc[0].x
        assert gdf2.geometry.iloc[0].y == gdf1.geometry.iloc[0].y
