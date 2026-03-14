"""Tests for InfluxService."""

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from srow.services.influx_service import (
    InfluxService,
    format_influx_error,
    is_delete_not_supported,
    _escape_flux_string,
)


class TestFormatInfluxError:
    """Tests for format_influx_error function."""

    def test_format_basic_exception(self):
        """Test formatting a basic exception."""
        err = Exception("Something went wrong")
        result = format_influx_error(err)
        assert "Exception" in result
        assert "Something went wrong" in result

    def test_format_exception_with_attributes(self):
        """Test formatting an exception with status/reason attributes."""
        err = Exception("Error")
        err.status = 404
        err.reason = "Not Found"
        err.message = "Resource not found"

        result = format_influx_error(err)

        assert "status=404" in result
        assert "reason=Not Found" in result
        assert "message=Resource not found" in result


class TestIsDeleteNotSupported:
    """Tests for is_delete_not_supported function."""

    def test_detects_serverless_error(self):
        """Test detection of serverless v3 bucket error."""
        err = Exception("Deletes ranges are not supported for serverless v3 buckets")
        assert is_delete_not_supported(err) is True

    def test_returns_false_for_other_errors(self):
        """Test that other errors return False."""
        err = Exception("Connection timeout")
        assert is_delete_not_supported(err) is False


class TestEscapeFluxString:
    """Tests for _escape_flux_string function."""

    def test_escapes_quotes(self):
        """Test that double quotes are escaped."""
        result = _escape_flux_string('hello "world"')
        assert result == 'hello \\"world\\"'

    def test_escapes_backslashes(self):
        """Test that backslashes are escaped."""
        result = _escape_flux_string("path\\to\\file")
        assert result == "path\\\\to\\\\file"

    def test_handles_both(self):
        """Test escaping both quotes and backslashes."""
        result = _escape_flux_string('C:\\path\\"file"')
        assert result == 'C:\\\\path\\\\\\"file\\"'

    def test_handles_normal_string(self):
        """Test that normal strings pass through unchanged."""
        result = _escape_flux_string("normal-string-123")
        assert result == "normal-string-123"


class TestInfluxService:
    """Tests for InfluxService class."""

    @pytest.fixture
    def service(self, sample_settings):
        """Create an InfluxService with mocked client."""
        with patch("srow.services.influx_service.influxdb_client.InfluxDBClient"):
            service = InfluxService(sample_settings)
            service._client = MagicMock()
            return service

    def test_ping(self, service):
        """Test ping method."""
        service._client.ping.return_value = True
        assert service.ping() is True

    def test_source_label_with_sensor_and_seat(self):
        """Test _source_label with both sensor and seat."""
        record = {"sensorId": "IMU-1", "seat": "BOW"}
        label = InfluxService._source_label(record)
        assert label == "IMU-1 (BOW)"

    def test_source_label_with_sensor_only(self):
        """Test _source_label with only sensor."""
        record = {"sensorId": "IMU-1"}
        label = InfluxService._source_label(record)
        assert label == "IMU-1"

    def test_source_label_with_seat_only(self):
        """Test _source_label with only seat."""
        record = {"seat": "BOW"}
        label = InfluxService._source_label(record)
        assert label == "BOW"

    def test_source_label_fallback(self):
        """Test _source_label returns UNKNOWN when no identifiers."""
        record = {}
        label = InfluxService._source_label(record)
        assert label == "UNKNOWN"

    def test_unwrap_angle_series_basic(self):
        """Test basic angle unwrapping."""
        # 170, 175, -175 (wraps at 180) -> should become 170, 175, 185
        series = pd.Series([170, 175, -175, -170])
        result = InfluxService._unwrap_angle_series(series)

        assert result.iloc[0] == 170
        assert result.iloc[1] == 175
        assert result.iloc[2] == 185  # -175 + 360
        assert result.iloc[3] == 190  # -170 + 360

    def test_unwrap_angle_series_negative_wrap(self):
        """Test unwrapping when going negative direction."""
        # -170, -175, 175 (wraps at -180) -> should become -170, -175, -185
        series = pd.Series([-170, -175, 175, 170])
        result = InfluxService._unwrap_angle_series(series)

        assert result.iloc[0] == -170
        assert result.iloc[1] == -175
        assert result.iloc[2] == -185  # 175 - 360
        assert result.iloc[3] == -190  # 170 - 360

    def test_unwrap_angle_series_with_nan(self):
        """Test that NaN values are handled."""
        series = pd.Series([170, float("nan"), -175])
        result = InfluxService._unwrap_angle_series(series)

        assert result.iloc[0] == 170
        assert pd.isna(result.iloc[1])
        # After NaN, continues from -175
        assert result.iloc[2] == -175

    def test_unwrap_angle_series_empty(self):
        """Test empty series returns empty."""
        series = pd.Series([], dtype=float)
        result = InfluxService._unwrap_angle_series(series)
        assert result.empty

    def test_summarize_interval(self, service):
        """Test interval summarization."""
        df = pd.DataFrame({
            "time": pd.date_range("2024-01-01", periods=100, freq="10ms"),
            "source": ["Sensor1"] * 100,
            "ax": [1.0] * 100,
        })

        summary = service.summarize_interval(df)

        assert len(summary) == 1
        assert summary.iloc[0]["samples"] == 100
        assert summary.iloc[0]["duration_sec"] == pytest.approx(0.99, rel=0.01)
        assert summary.iloc[0]["approx_hz"] == pytest.approx(101, rel=1)

    def test_summarize_interval_empty(self, service):
        """Test summarizing empty DataFrame."""
        df = pd.DataFrame()
        summary = service.summarize_interval(df)
        assert summary.empty

    def test_unwrap_angles(self, service, sample_imu_data):
        """Test unwrapping multiple angle columns."""
        df = sample_imu_data.copy()
        result = service.unwrap_angles(df, ["roll", "pitch", "yaw"])

        # Should return a copy, not modify in place
        assert result is not df

        # Should have same shape
        assert result.shape == df.shape

        # Columns should exist
        assert "roll" in result.columns
        assert "pitch" in result.columns
        assert "yaw" in result.columns
