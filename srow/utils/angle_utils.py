"""Angle processing utilities.

Provides functions for unwrapping angle data to remove discontinuities
at +/- 180 degree boundaries.
"""

import pandas as pd


def unwrap_angle_series(series: pd.Series) -> pd.Series:
    """Unwrap a single angle series to remove discontinuities.

    Handles jumps greater than 180 degrees by adding/subtracting 360.
    This is useful for orientation data (roll, pitch, yaw) where values
    wrap around at +/- 180 degrees.

    Args:
        series: Pandas Series of angle values in degrees.

    Returns:
        Unwrapped angle series with continuous values.

    Example:
        >>> s = pd.Series([170, 175, -175, -170])  # Wraps at 180
        >>> unwrap_angle_series(s)
        0    170
        1    175
        2    185  # -175 + 360
        3    190  # -170 + 360
        dtype: float64
    """
    if series.empty:
        return series

    out = series.copy()
    prev = out.iloc[0]

    for i in range(1, len(out)):
        val = out.iloc[i]
        if pd.isna(val) or pd.isna(prev):
            prev = val
            continue

        diff = val - prev
        while diff > 180:
            val -= 360
            diff = val - prev
        while diff < -180:
            val += 360
            diff = val - prev

        out.iloc[i] = val
        prev = val

    return out


def unwrap_angles(df: pd.DataFrame, fields: list[str], group_col: str = "source") -> pd.DataFrame:
    """Unwrap angle fields in a DataFrame, grouped by source.

    Applies angle unwrapping per group to maintain continuous angle
    progression for each sensor independently.

    Args:
        df: DataFrame with angle columns.
        fields: List of column names to unwrap (e.g., ["roll", "pitch", "yaw"]).
        group_col: Column to group by before unwrapping. Defaults to "source".

    Returns:
        DataFrame with unwrapped angles (copy of input).

    Example:
        >>> df = pd.DataFrame({
        ...     "source": ["A", "A", "A", "B", "B", "B"],
        ...     "yaw": [170, 175, -175, 10, 15, 20],
        ... })
        >>> unwrap_angles(df, ["yaw"])
           source  yaw
        0      A  170
        1      A  175
        2      A  185
        3      B   10
        4      B   15
        5      B   20
    """
    if df.empty:
        return df

    df_copy = df.copy()

    for field in fields:
        if field not in df_copy.columns:
            continue
        if group_col in df_copy.columns:
            df_copy[field] = (
                df_copy.groupby(group_col)[field]
                .transform(unwrap_angle_series)
            )
        else:
            df_copy[field] = unwrap_angle_series(df_copy[field])

    return df_copy
