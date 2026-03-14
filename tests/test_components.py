"""Tests for Panel UI components."""

import pytest
import pandas as pd

from srow.state import AppState
from srow.components import SidebarComponent


class TestAppState:
    """Tests for AppState class."""

    def test_initial_state(self, app_state):
        """Test initial state values."""
        assert app_state.connected is False
        assert app_state.intervals == []
        assert app_state.selected_interval is None
        assert app_state.imu_data.empty
        assert app_state.loading is False
        assert app_state.error_message == ""

    def test_has_data_empty(self, app_state):
        """Test has_data property when no data."""
        assert app_state.has_data is False

    def test_has_data_with_data(self, app_state_with_data):
        """Test has_data property when data is loaded."""
        assert app_state_with_data.has_data is True

    def test_updates_available_sources(self, app_state, sample_imu_data):
        """Test that loading data updates available sources."""
        app_state.imu_data = sample_imu_data

        assert "Sensor1" in app_state.available_sources
        assert "Sensor2" in app_state.available_sources

    def test_updates_available_fields(self, app_state, sample_imu_data):
        """Test that loading data updates available fields."""
        app_state.imu_data = sample_imu_data

        assert "ax" in app_state.available_fields
        assert "ay" in app_state.available_fields
        assert "roll" in app_state.available_fields

    def test_get_filtered_data_no_filter(self, app_state_with_data):
        """Test get_filtered_data with no filters applied."""
        df = app_state_with_data.get_filtered_data()

        # Should return all data
        assert len(df) == 200  # 100 samples * 2 sources

    def test_get_filtered_data_by_source(self, app_state_with_data):
        """Test get_filtered_data filtering by source."""
        app_state_with_data.selected_sources = ["Sensor1"]
        df = app_state_with_data.get_filtered_data()

        assert len(df) == 100
        assert df["source"].unique().tolist() == ["Sensor1"]

    def test_clear_data(self, app_state_with_data):
        """Test clearing all data."""
        app_state_with_data.clear_data()

        assert app_state_with_data.imu_data.empty
        assert app_state_with_data.selected_sources == []
        assert app_state_with_data.selected_fields == []

    def test_set_error(self, app_state):
        """Test setting error message."""
        app_state.loading = True
        app_state.set_error("Something went wrong")

        assert app_state.error_message == "Something went wrong"
        assert app_state.loading is False  # Should be cleared

    def test_clear_error(self, app_state):
        """Test clearing error message."""
        app_state.error_message = "Some error"
        app_state.clear_error()

        assert app_state.error_message == ""

    def test_data_summary(self, app_state_with_data):
        """Test data summary property."""
        summary = app_state_with_data.data_summary

        assert summary["row_count"] == 200
        assert summary["source_count"] == 2
        assert summary["field_count"] > 0


class TestSidebarComponent:
    """Tests for SidebarComponent."""

    def test_creates_without_error(self, app_state):
        """Test that sidebar can be created."""
        sidebar = SidebarComponent(app_state)
        assert sidebar is not None

    def test_updates_on_connection_change(self, app_state):
        """Test that sidebar updates when connection state changes."""
        sidebar = SidebarComponent(app_state)

        # Initially disconnected
        assert sidebar._status_indicator.value is False

        # Change connection state
        app_state.connected = True

        # Status should update
        assert sidebar._status_indicator.value is True

    def test_interval_options_empty(self, app_state):
        """Test interval options when no intervals available."""
        sidebar = SidebarComponent(app_state)
        options = sidebar._get_interval_options()

        assert "No intervals available" in options

    def test_interval_options_with_data(self, app_state):
        """Test interval options when intervals are available."""
        app_state.intervals = [
            {"tag": "intervalId", "value": "123", "label": "123"},
            {"tag": "intervalId", "value": "456", "label": "456"},
        ]
        sidebar = SidebarComponent(app_state)
        options = sidebar._get_interval_options()

        assert "123" in options
        assert "456" in options
