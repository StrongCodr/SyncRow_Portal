"""Time formatting and parsing utilities."""

from datetime import datetime, timedelta

import pandas as pd


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted string like "5m 30s" or "1h 23m 45s".

    Example:
        >>> format_duration(90)
        '1m 30s'
        >>> format_duration(3661)
        '1h 1m 1s'
    """
    if pd.isna(seconds) or seconds < 0:
        return "N/A"

    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())

    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def format_timestamp(dt: datetime | pd.Timestamp | None, include_ms: bool = False) -> str:
    """Format a datetime as a readable string.

    Args:
        dt: Datetime to format.
        include_ms: Whether to include milliseconds. Defaults to False.

    Returns:
        Formatted string like "2024-01-15 14:30:00" or "N/A" if None.
    """
    if dt is None or pd.isna(dt):
        return "N/A"

    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()

    if include_ms:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_interval_time(value: str | int | float) -> datetime | None:
    """Parse an interval timestamp value.

    Handles multiple formats:
    - Milliseconds since epoch (numeric)
    - ISO format strings
    - Common datetime strings

    Args:
        value: The value to parse.

    Returns:
        Parsed datetime or None if parsing fails.

    Example:
        >>> parse_interval_time(1705329000000)  # ms since epoch
        datetime(2024, 1, 15, 14, 30, 0)
        >>> parse_interval_time("2024-01-15T14:30:00Z")
        datetime(2024, 1, 15, 14, 30, 0)
    """
    if value is None:
        return None

    # Try milliseconds since epoch
    if isinstance(value, (int, float)):
        try:
            parsed = pd.to_datetime(value, unit="ms", errors="coerce", utc=True)
            if pd.notna(parsed):
                return parsed.to_pydatetime()
        except (ValueError, OverflowError):
            pass

    # Try string parsing
    if isinstance(value, str):
        # Try pandas parsing (handles many formats)
        try:
            parsed = pd.to_datetime(value, errors="coerce", utc=True)
            if pd.notna(parsed):
                return parsed.to_pydatetime()
        except (ValueError, OverflowError):
            pass

        # Try as numeric string (milliseconds)
        try:
            ms = float(value)
            parsed = pd.to_datetime(ms, unit="ms", errors="coerce", utc=True)
            if pd.notna(parsed):
                return parsed.to_pydatetime()
        except (ValueError, OverflowError):
            pass

    return None
