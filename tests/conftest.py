"""Pytest fixtures and configuration for SyncRow tests."""

import os
import pytest
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from unittest.mock import MagicMock

from srow.config import Settings
from srow.state import AppState


@pytest.fixture
def sample_settings() -> Settings:
    """Create sample settings for testing."""
    return Settings(
        url="http://localhost:8086",
        token="test-token",
        org="test-org",
        org_id="test-org-id",
        bucket="test-bucket",
    )


@pytest.fixture
def sample_imu_data() -> pd.DataFrame:
    """Create sample IMU data for testing."""
    import numpy as np

    n_samples = 100
    times = pd.date_range("2024-01-01", periods=n_samples, freq="10ms")

    data = {
        "time": list(times) * 2,  # Two sources
        "source": ["Sensor1"] * n_samples + ["Sensor2"] * n_samples,
        "ax": np.random.randn(n_samples * 2),
        "ay": np.random.randn(n_samples * 2),
        "az": np.random.randn(n_samples * 2) + 9.8,
        "roll": np.cumsum(np.random.randn(n_samples * 2)) % 360 - 180,
        "pitch": np.random.randn(n_samples * 2) * 10,
        "yaw": np.cumsum(np.random.randn(n_samples * 2)) % 360 - 180,
    }

    return pd.DataFrame(data)


@pytest.fixture
def sample_location_data() -> gpd.GeoDataFrame:
    """Create sample GPS location data for testing."""
    n_points = 50
    times = pd.date_range("2024-01-01", periods=n_points, freq="1s")

    # Simulate a track moving northeast
    base_lat = 42.3601  # Boston
    base_lon = -71.0589
    lats = [base_lat + i * 0.0001 for i in range(n_points)]
    lons = [base_lon + i * 0.0001 for i in range(n_points)]

    data = {
        "time": times,
        "device_id": ["phone1"] * n_points,
        "latitude": lats,
        "longitude": lons,
        "altitude": [10.0] * n_points,
        "speed": [2.5 + i * 0.1 for i in range(n_points)],
        "accuracy": [5.0] * n_points,
    }

    df = pd.DataFrame(data)
    geometry = [Point(lon, lat) for lat, lon in zip(lats, lons)]

    return gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")


@pytest.fixture
def app_state() -> AppState:
    """Create an AppState instance for testing."""
    return AppState()


@pytest.fixture
def app_state_with_data(app_state, sample_imu_data) -> AppState:
    """Create an AppState with sample data loaded."""
    app_state.imu_data = sample_imu_data
    return app_state


@pytest.fixture
def mock_influx_client():
    """Create a mock InfluxDB client."""
    client = MagicMock()
    client.ping.return_value = True
    client.query_api.return_value = MagicMock()
    client.delete_api.return_value = MagicMock()
    return client


@pytest.fixture
def env_file(tmp_path):
    """Create a temporary .env file for testing."""
    env_path = tmp_path / ".env"
    env_path.write_text("""
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=test-token
INFLUX_ORG=test-org
INFLUX_ORG_ID=test-org-id
INFLUX_BUCKET=test-bucket
""")
    return str(env_path)
