"""Tests for LocationService."""

import pytest
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from unittest.mock import MagicMock, patch

from srow.services.location_service import LocationService


class TestLocationService:
    """Tests for LocationService class."""

    @pytest.fixture
    def service(self, sample_settings):
        """Create a LocationService with mocked client."""
        with patch("srow.services.location_service.influxdb_client.InfluxDBClient"):
            service = LocationService(sample_settings)
            service._client = MagicMock()
            return service

    def test_ping(self, service):
        """Test ping method."""
        service._client.ping.return_value = True
        assert service.ping() is True

    def test_calculate_distance(self, service, sample_location_data):
        """Test distance calculation."""
        distance = service.calculate_distance(sample_location_data)

        # Should be positive distance
        assert distance > 0

        # Roughly estimate: 50 points, each ~10m apart = ~500m
        # (0.0001 degrees at 42° lat is roughly 11 meters)
        assert 400 < distance < 800

    def test_calculate_distance_empty(self, service):
        """Test distance calculation with empty GeoDataFrame."""
        gdf = gpd.GeoDataFrame()
        distance = service.calculate_distance(gdf)
        assert distance == 0.0

    def test_calculate_distance_single_point(self, service):
        """Test distance calculation with single point."""
        gdf = gpd.GeoDataFrame(
            {"time": [pd.Timestamp("2024-01-01")]},
            geometry=[Point(-71.0, 42.0)],
            crs="EPSG:4326",
        )
        distance = service.calculate_distance(gdf)
        assert distance == 0.0

    def test_calculate_speed_stats(self, service, sample_location_data):
        """Test speed statistics calculation."""
        stats = service.calculate_speed_stats(sample_location_data)

        assert "avg_speed" in stats
        assert "max_speed" in stats
        assert "min_speed" in stats
        assert "std_speed" in stats

        # Verify values are reasonable
        assert stats["min_speed"] < stats["avg_speed"] < stats["max_speed"]

    def test_calculate_speed_stats_empty(self, service):
        """Test speed stats with empty GeoDataFrame."""
        gdf = gpd.GeoDataFrame()
        stats = service.calculate_speed_stats(gdf)
        assert stats == {}

    def test_calculate_speed_stats_no_speed_column(self, service):
        """Test speed stats without speed column."""
        gdf = gpd.GeoDataFrame(
            {"time": [pd.Timestamp("2024-01-01")]},
            geometry=[Point(-71.0, 42.0)],
            crs="EPSG:4326",
        )
        stats = service.calculate_speed_stats(gdf)
        assert stats == {}

    def test_summarize_track(self, service, sample_location_data):
        """Test track summary."""
        summary = service.summarize_track(sample_location_data)

        assert "start" in summary
        assert "end" in summary
        assert "duration_sec" in summary
        assert "n_points" in summary
        assert "total_distance_m" in summary

        assert summary["n_points"] == 50
        assert summary["duration_sec"] == 49  # 50 points at 1s intervals

    def test_summarize_track_empty(self, service):
        """Test track summary with empty data."""
        gdf = gpd.GeoDataFrame()
        summary = service.summarize_track(gdf)
        assert summary == {}
