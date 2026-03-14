"""Data table component with virtual scrolling.

Uses Panel's Tabulator widget for efficient rendering of large datasets
with virtual scrolling, sorting, and filtering.
"""

import io
import panel as pn
import param
import pandas as pd

from srow.state import AppState


class DataTableComponent(pn.viewable.Viewer):
    """Data table with virtual scrolling for large datasets.

    Uses Tabulator for efficient rendering of up to millions of rows
    through virtual scrolling (only renders visible rows).
    """

    state = param.ClassSelector(class_=AppState, doc="Application state")
    page_size = param.Integer(default=50, doc="Number of rows per page")

    def __init__(self, state: AppState, **params):
        """Initialize the data table component.

        Args:
            state: Application state to watch.
        """
        params["state"] = state
        super().__init__(**params)

        # Initialize with empty DataFrame
        self._table = pn.widgets.Tabulator(
            pd.DataFrame(),
            pagination="remote",
            page_size=self.page_size,
            sizing_mode="stretch_width",
            min_height=400,
            show_index=False,
            selectable=True,
            theme="simple",
            configuration={
                "columnDefaults": {
                    "headerFilter": True,
                },
            },
        )

        # Export button
        self._export_btn = pn.widgets.Button(
            name="Export CSV",
            button_type="primary",
            width=120,
        )
        self._export_btn.on_click(self._on_export_click)

        # Row count display
        self._row_count = pn.pane.Markdown("")

        # Watch for data changes
        state.param.watch(self._update_table, ["imu_data", "selected_sources"])

    def _update_table(self, event=None):
        """Update the table when data changes."""
        df = self.state.get_filtered_data()

        if df.empty:
            self._table.value = pd.DataFrame()
            self._row_count.object = "No data"
            return

        # Format time column if present
        if "time" in df.columns:
            df = df.copy()
            df["time"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]

        self._table.value = df
        self._row_count.object = f"**{len(df):,} rows**"

    def _on_export_click(self, event):
        """Handle export button click."""
        df = self.state.get_filtered_data()

        if df.empty:
            return

        # Create CSV in memory
        buffer = io.StringIO()
        df.to_csv(buffer, index=False)
        csv_content = buffer.getvalue()

        # Trigger download
        # Note: This requires the FileDownload widget approach
        # For now, we'll just show an alert
        pn.state.notifications.info("Export functionality - CSV data ready")

    def get_download_widget(self) -> pn.widgets.FileDownload:
        """Get a file download widget for exporting data.

        Returns:
            FileDownload widget configured for CSV export.
        """
        def get_csv():
            df = self.state.get_filtered_data()
            if df.empty:
                return ""
            buffer = io.StringIO()
            df.to_csv(buffer, index=False)
            return buffer.getvalue()

        return pn.widgets.FileDownload(
            callback=get_csv,
            filename="srow_data.csv",
            button_type="primary",
            label="Download CSV",
        )

    def __panel__(self):
        """Return the Panel layout for this component."""
        download = self.get_download_widget()

        return pn.Column(
            pn.Row(
                self._row_count,
                pn.layout.HSpacer(),
                download,
            ),
            self._table,
            sizing_mode="stretch_both",
        )


class SummaryTableComponent(pn.viewable.Viewer):
    """Summary statistics table for interval data."""

    state = param.ClassSelector(class_=AppState, doc="Application state")

    def __init__(self, state: AppState, **params):
        """Initialize the summary table component."""
        params["state"] = state
        super().__init__(**params)

        self._table = pn.widgets.Tabulator(
            pd.DataFrame(),
            sizing_mode="stretch_width",
            min_height=150,
            show_index=False,
            theme="simple",
        )

        state.param.watch(self._update_summary, "imu_data")

    def _update_summary(self, event=None):
        """Update summary statistics when data changes."""
        df = self.state.imu_data

        if df.empty or "time" not in df.columns or "source" not in df.columns:
            self._table.value = pd.DataFrame()
            return

        # Calculate summary per source
        def summarize(group):
            t_start = group["time"].min()
            t_end = group["time"].max()
            duration_sec = (t_end - t_start).total_seconds()
            n_samples = len(group)
            hz = n_samples / duration_sec if duration_sec > 0 else float("nan")

            return pd.Series({
                "Start": t_start.strftime("%H:%M:%S") if hasattr(t_start, "strftime") else str(t_start),
                "End": t_end.strftime("%H:%M:%S") if hasattr(t_end, "strftime") else str(t_end),
                "Duration": f"{duration_sec:.1f}s",
                "Samples": f"{n_samples:,}",
                "Freq (Hz)": f"{hz:.1f}",
            })

        summary = df.groupby("source").apply(summarize).reset_index()
        summary = summary.rename(columns={"source": "Source"})

        self._table.value = summary

    def __panel__(self):
        """Return the Panel layout."""
        return pn.Column(
            pn.pane.Markdown("### Summary"),
            self._table,
            sizing_mode="stretch_width",
        )
