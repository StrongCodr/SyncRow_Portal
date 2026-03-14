"""Tests for angle utilities."""

import pytest
import pandas as pd

from srow.utils.angle_utils import unwrap_angle_series, unwrap_angles


class TestUnwrapAngleSeries:
    """Tests for unwrap_angle_series function."""

    def test_basic_positive_wrap(self):
        """Test unwrapping when crossing +180."""
        series = pd.Series([170, 175, 180, -175, -170])
        result = unwrap_angle_series(series)

        assert result.iloc[0] == 170
        assert result.iloc[1] == 175
        assert result.iloc[2] == 180
        assert result.iloc[3] == 185  # -175 + 360
        assert result.iloc[4] == 190  # -170 + 360

    def test_basic_negative_wrap(self):
        """Test unwrapping when crossing -180."""
        series = pd.Series([-170, -175, -180, 175, 170])
        result = unwrap_angle_series(series)

        assert result.iloc[0] == -170
        assert result.iloc[1] == -175
        assert result.iloc[2] == -180
        assert result.iloc[3] == -185  # 175 - 360
        assert result.iloc[4] == -190  # 170 - 360

    def test_multiple_wraps(self):
        """Test unwrapping with oscillation around 180 boundary."""
        # Oscillating around 180 degrees
        series = pd.Series([170, -170, 170, -170, 170])
        result = unwrap_angle_series(series)

        # The algorithm unwraps each transition independently
        # 170 -> -170 is a -340 degree jump, so -170 becomes 190
        # 190 -> 170 is a -20 degree change, stays at 170
        # etc.
        assert result.iloc[0] == 170
        assert result.iloc[1] == 190  # -170 + 360
        assert result.iloc[2] == 170  # back to 170 (diff = -20)
        assert result.iloc[3] == 190  # -170 + 360 again
        assert result.iloc[4] == 170  # back to 170

    def test_no_wrapping_needed(self):
        """Test that angles not crossing boundaries stay unchanged."""
        series = pd.Series([10, 20, 30, 40, 50])
        result = unwrap_angle_series(series)

        pd.testing.assert_series_equal(result, series)

    def test_handles_nan(self):
        """Test that NaN values are preserved."""
        series = pd.Series([170, float("nan"), 175])
        result = unwrap_angle_series(series)

        assert result.iloc[0] == 170
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == 175

    def test_empty_series(self):
        """Test empty series returns empty."""
        series = pd.Series([], dtype=float)
        result = unwrap_angle_series(series)
        assert result.empty

    def test_single_value(self):
        """Test single value returns unchanged."""
        series = pd.Series([45.0])
        result = unwrap_angle_series(series)
        assert result.iloc[0] == 45.0


class TestUnwrapAngles:
    """Tests for unwrap_angles DataFrame function."""

    def test_unwraps_by_group(self):
        """Test that unwrapping is done per group."""
        df = pd.DataFrame({
            "source": ["A", "A", "A", "B", "B", "B"],
            "yaw": [170, 175, -175, 10, 15, 20],
        })

        result = unwrap_angles(df, ["yaw"])

        # Group A should be unwrapped
        assert result[result["source"] == "A"]["yaw"].tolist() == [170, 175, 185]

        # Group B should stay the same (no wrapping)
        assert result[result["source"] == "B"]["yaw"].tolist() == [10, 15, 20]

    def test_multiple_fields(self):
        """Test unwrapping multiple angle fields."""
        df = pd.DataFrame({
            "source": ["A"] * 3,
            "roll": [170, 175, -175],
            "pitch": [10, 15, 20],
            "yaw": [-170, -175, 175],
        })

        result = unwrap_angles(df, ["roll", "pitch", "yaw"])

        # Roll should be unwrapped
        assert result["roll"].iloc[2] == 185

        # Pitch should stay same (no wrapping)
        assert result["pitch"].tolist() == [10, 15, 20]

        # Yaw should be unwrapped
        assert result["yaw"].iloc[2] == -185

    def test_missing_field_ignored(self):
        """Test that missing fields don't cause errors."""
        df = pd.DataFrame({
            "source": ["A"] * 3,
            "roll": [10, 20, 30],
        })

        # Should not raise even though "yaw" doesn't exist
        result = unwrap_angles(df, ["roll", "yaw"])

        assert "roll" in result.columns
        assert "yaw" not in result.columns

    def test_empty_dataframe(self):
        """Test empty DataFrame returns empty."""
        df = pd.DataFrame()
        result = unwrap_angles(df, ["roll"])
        assert result.empty

    def test_returns_copy(self):
        """Test that function returns a copy, not modifying original."""
        df = pd.DataFrame({
            "source": ["A"] * 3,
            "roll": [170, 175, -175],
        })
        original_roll = df["roll"].copy()

        result = unwrap_angles(df, ["roll"])

        # Original should be unchanged
        pd.testing.assert_series_equal(df["roll"], original_roll)

        # Result should be different
        assert result["roll"].iloc[2] != df["roll"].iloc[2]
