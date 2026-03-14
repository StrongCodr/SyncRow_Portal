"""Sidebar component with interval selection and filters.

Provides controls for:
- Connection status display
- Interval selection dropdown
- Source multi-select
- Field multi-select
- Time range slider
"""

import panel as pn
import param

from srow.state import AppState


class SidebarComponent(pn.viewable.Viewer):
    """Sidebar with controls for data selection and filtering.

    Watches the AppState and provides widgets for user interaction.
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")

    def __init__(self, state: AppState, **params):
        """Initialize the sidebar component.

        Args:
            state: Application state to watch and modify.
        """
        params["state"] = state
        super().__init__(**params)

        # Connection status indicator
        self._status_indicator = pn.indicators.BooleanStatus(
            value=state.connected,
            color="success",
            width=20,
            height=20,
        )
        self._status_text = pn.pane.Markdown(
            self._get_status_text(),
            styles={"font-size": "14px"},
        )

        # Interval selector
        self._interval_select = pn.widgets.Select(
            name="Interval",
            options=self._get_interval_options(),
            value=None,
            sizing_mode="stretch_width",
        )
        self._interval_select.param.watch(self._on_interval_change, "value")

        # Source multi-select
        self._source_select = pn.widgets.MultiSelect(
            name="Sources",
            options=state.available_sources,
            value=state.selected_sources,
            size=6,
            sizing_mode="stretch_width",
        )
        self._source_select.param.watch(self._on_source_change, "value")

        # Field multi-select
        self._field_select = pn.widgets.MultiSelect(
            name="Fields",
            options=state.available_fields,
            value=state.selected_fields,
            size=6,
            sizing_mode="stretch_width",
        )
        self._field_select.param.watch(self._on_field_change, "value")

        # Refresh button
        self._refresh_btn = pn.widgets.Button(
            name="Refresh Intervals",
            button_type="default",
            sizing_mode="stretch_width",
        )
        self._refresh_btn.on_click(self._on_refresh_click)

        # Watch state changes
        state.param.watch(self._on_state_connected_change, "connected")
        state.param.watch(self._on_state_intervals_change, "intervals")
        state.param.watch(self._on_state_sources_change, "available_sources")
        state.param.watch(self._on_state_fields_change, "available_fields")
        state.param.watch(self._on_state_selected_sources_change, "selected_sources")
        state.param.watch(self._on_state_selected_fields_change, "selected_fields")

    def _get_status_text(self) -> str:
        """Get the connection status text."""
        if self.state.connected:
            return "**Connected**"
        return "**Disconnected**"

    def _get_interval_options(self) -> dict:
        """Get interval options for the select widget."""
        if not self.state.intervals:
            return {"No intervals available": None}
        return {iv["label"]: iv for iv in self.state.intervals}

    def _on_interval_change(self, event):
        """Handle interval selection change."""
        self.state.selected_interval = event.new

    def _on_source_change(self, event):
        """Handle source selection change."""
        self.state.selected_sources = list(event.new)

    def _on_field_change(self, event):
        """Handle field selection change."""
        self.state.selected_fields = list(event.new)

    def _on_refresh_click(self, event):
        """Handle refresh button click."""
        # This will be connected to the main app's refresh logic
        pass

    def _on_state_connected_change(self, event):
        """Update UI when connection state changes."""
        self._status_indicator.value = event.new
        self._status_text.object = self._get_status_text()

    def _on_state_intervals_change(self, event):
        """Update interval options when intervals change."""
        self._interval_select.options = self._get_interval_options()

    def _on_state_sources_change(self, event):
        """Update source options when available sources change."""
        self._source_select.options = event.new
        self._source_select.value = [s for s in self._source_select.value if s in event.new]

    def _on_state_fields_change(self, event):
        """Update field options when available fields change."""
        self._field_select.options = event.new
        self._field_select.value = [f for f in self._field_select.value if f in event.new]

    def _on_state_selected_sources_change(self, event):
        """Sync source selection with state."""
        if list(self._source_select.value) != event.new:
            self._source_select.value = event.new

    def _on_state_selected_fields_change(self, event):
        """Sync field selection with state."""
        if list(self._field_select.value) != event.new:
            self._field_select.value = event.new

    def on_refresh(self, callback):
        """Register a callback for the refresh button.

        Args:
            callback: Function to call when refresh is clicked.
        """
        self._refresh_btn.on_click(callback)

    def __panel__(self):
        """Return the Panel layout for this component."""
        return pn.Column(
            pn.pane.Markdown("## SyncRow"),
            pn.Row(
                self._status_indicator,
                self._status_text,
            ),
            pn.layout.Divider(),
            self._interval_select,
            self._refresh_btn,
            pn.layout.Divider(),
            pn.pane.Markdown("### Filters"),
            self._source_select,
            self._field_select,
            sizing_mode="stretch_width",
        )
