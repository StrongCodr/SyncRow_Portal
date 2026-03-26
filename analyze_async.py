#!/usr/bin/env python3
"""Empirical Rowing Asynchronicity Analyzer.

Standalone CLI script that:
1. Downloads all interval data from InfluxDB via local Parquet cache
2. Detects active rowing and individual strokes per sensor
3. Measures per-stroke catch timing offset (asynchronicity) across sensors
4. Matches with GPS speed at each stroke
5. Produces empirical tables bucketed by SPM and async range
6. Compares against theoretical model predictions

Usage:
    python3 analyze_async.py [--env .env] [--skip-download]
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import certifi
import influxdb_client
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from srow.config import load_settings
from srow.services import CacheService, InfluxService, LocationService

# ─── Constants ────────────────────────────────────────────────────────────────

MIN_ACTIVE_DURATION_SEC = 10
ACTIVE_GAP_MERGE_SEC = 2.0
ACTIVE_VARIANCE_FRAC = 0.05  # fraction of max variance as threshold

MIN_STROKES_PER_INTERVAL = 50
MIN_STROKES_PER_BUCKET = 5

MAX_SPM = 36  # physical limit — even elite scullers rarely sustain above 38

MIN_SENSORS_FOR_ASYNC = 3
CATCH_MATCH_TOLERANCE = 0.5  # fraction of stroke period

GPS_MATCH_TOLERANCE_SEC = 2.0
GPS_ACCURACY_MAX_M = 10.0

STABLE_SPM_TOLERANCE = 2.0  # max SPM deviation from run mean to stay in run
MIN_RUN_LENGTH = 6           # minimum strokes per piece

SPM_BUCKETS = [
    (18, 22), (22, 26), (26, 30), (30, 34), (34, 36),
]

MAX_ASYNC_MS = 400  # above this, treat as detection error and discard

ASYNC_BUCKETS_MS = [
    (0, 20), (20, 50), (50, 100), (100, 150), (150, 200), (200, 400),
]

# Theoretical model: speed_loss = k * delta (linear in phase offset)
# Calibrated: 100ms at 30 SPM -> 1.5% speed loss
# omega = 2*pi*30/60 = pi rad/s, delta = pi * 0.1 = 0.314
# 0.015 = k * 0.314 => k ~= 0.0477
THEORY_K = 0.015 / (2 * np.pi * 30 / 60 * 0.100)

DOWNLOAD_TIMEOUT_MS = 120_000  # 2 minutes per query (default is 10s)
MAX_DOWNLOAD_RETRIES = 3


# ─── Signal Processing ───────────────────────────────────────────────────────


def estimate_sample_rate(times: pd.Series) -> float:
    """Estimate sample rate from timestamps."""
    dt = times.diff().dt.total_seconds().dropna()
    if dt.empty or dt.median() <= 0:
        return 0.0
    return 1.0 / dt.median()


def compute_gyro_mag(df_source: pd.DataFrame, sample_rate: float) -> np.ndarray:
    """Compute angular velocity magnitude for stroke detection.

    Uses raw angular velocity (wx, wy, wz in deg/s) when available.
    Falls back to differentiating Euler angles (roll, pitch, yaw) if not.
    Returns the vector magnitude in rad/s.
    """
    # Prefer raw angular velocity columns (WT9011DCL outputs deg/s)
    gyro_cols = [c for c in ("wx", "wy", "wz") if c in df_source.columns]
    if gyro_cols:
        components = []
        for col in gyro_cols:
            vals = df_source[col].astype(float).interpolate(limit_direction="both").values
            components.append(np.deg2rad(vals))
        stacked = np.column_stack(components)
        return np.sqrt(np.sum(stacked**2, axis=1))

    # Fallback: differentiate Euler angles
    dt = 1.0 / sample_rate
    components = []
    for col in ("roll", "pitch", "yaw"):
        if col not in df_source.columns:
            continue
        vals = df_source[col].astype(float).interpolate(limit_direction="both").values
        unwrapped = np.unwrap(np.deg2rad(vals))
        components.append(np.gradient(unwrapped, dt))

    if not components:
        return np.zeros(len(df_source))

    stacked = np.column_stack(components)
    return np.sqrt(np.sum(stacked**2, axis=1))


def detect_active_windows(
    gyro_mag: np.ndarray, sample_rate: float
) -> list[tuple[int, int]]:
    """Find index ranges where rowing is active based on rolling gyro variance.

    Returns list of (start_idx, end_idx) tuples.
    """
    window_samples = max(int(0.5 * sample_rate), 1)

    variance = (
        pd.Series(gyro_mag)
        .rolling(window_samples, center=True, min_periods=1)
        .var()
        .values
    )

    max_var = np.nanmax(variance)
    if max_var == 0 or np.isnan(max_var):
        return []
    threshold = max_var * ACTIVE_VARIANCE_FRAC

    active = variance > threshold

    gap_samples = int(ACTIVE_GAP_MERGE_SEC * sample_rate)
    min_samples = int(MIN_ACTIVE_DURATION_SEC * sample_rate)

    windows = []
    in_window = False
    start = 0
    gap_count = 0

    for i in range(len(active)):
        if active[i]:
            if not in_window:
                start = i
                in_window = True
            gap_count = 0
        elif in_window:
            gap_count += 1
            if gap_count > gap_samples:
                end = i - gap_count
                if end - start >= min_samples:
                    windows.append((start, end))
                in_window = False
                gap_count = 0

    if in_window:
        end = len(active)
        if end - start >= min_samples:
            windows.append((start, end))

    return windows


def _select_rowing_channel(
    src_df: pd.DataFrame, sample_rate: float
) -> str | None:
    """Identify which orientation channel (pitch, roll, or yaw) carries the
    dominant rowing oscillation for this sensor.

    The sensor may be taped to the oar at any rotation, so the rowing motion
    could appear in any channel.  We pick the one with the highest IQR
    (interquartile range) after light smoothing — this is robust to outliers
    and does not assume which axis the motion lands on.

    Returns the column name, or None if no channel has meaningful oscillation.
    """
    smooth_n = max(int(0.1 * sample_rate), 3)
    if smooth_n % 2 == 0:
        smooth_n += 1

    best_col: str | None = None
    best_iqr = 0.0

    for col in ("pitch", "roll", "yaw"):
        if col not in src_df.columns:
            continue
        vals = src_df[col].astype(float).interpolate(limit_direction="both").values
        if len(vals) < int(2 * sample_rate):
            continue
        smoothed = (
            pd.Series(vals).rolling(smooth_n, center=True, min_periods=1).mean().values
        )
        iqr = float(np.percentile(smoothed, 75) - np.percentile(smoothed, 25))
        if iqr > best_iqr:
            best_iqr = iqr
            best_col = col

    # Require at least 5° IQR to be considered meaningful oscillation
    if best_iqr < 5.0:
        return None
    return best_col


def find_rowing_crossings(
    src_df: pd.DataFrame,
    times_sec: np.ndarray,
    sample_rate: float,
    channel: str | None = None,
) -> tuple[list[float], list[float], list[float], list[float], str] | None:
    """Find median-crossing times in the dominant orientation channel.

    Automatically selects the orientation axis (pitch, roll, or yaw) with
    the strongest oscillation, so the algorithm works regardless of how the
    sensor is mounted on the oar.

    Median crossings give far better timing precision than peak/trough
    detection because the signal moves fastest through its midpoint.
    At extrema the signal lingers (especially on plateau waveforms),
    shifting the detected position by hundreds of ms.

    Parameters
    ----------
    src_df : DataFrame for a single sensor window.
    times_sec : epoch timestamps aligned to src_df rows.
    sample_rate : estimated sample rate (Hz).
    channel : force a specific column; auto-detected if None.

    Returns (up_times, down_times, gyro_after_up, gyro_after_down, channel)
    or None if the signal is too short/flat.
    The caller aggregates across windows and decides which direction = catch.
    """
    if channel is None:
        channel = _select_rowing_channel(src_df, sample_rate)
    if channel is None or channel not in src_df.columns:
        return None

    signal = src_df[channel].astype(float).interpolate(limit_direction="both").values

    if len(signal) < int(2 * sample_rate):
        return None

    # Light smoothing: 0.1s removes sensor jitter without shifting transitions
    smooth_n = max(int(0.1 * sample_rate), 3)
    if smooth_n % 2 == 0:
        smooth_n += 1
    signal_smooth = (
        pd.Series(signal).rolling(smooth_n, center=True, min_periods=1).mean().values
    )

    # Check signal has meaningful oscillation
    iqr = np.percentile(signal_smooth, 75) - np.percentile(signal_smooth, 25)
    if iqr < 5.0:
        return None

    median_val = np.median(signal_smooth)
    above = signal_smooth > median_val

    up_times: list[float] = []
    down_times: list[float] = []
    up_indices: list[int] = []
    down_indices: list[int] = []

    for i in range(1, len(above)):
        if above[i] == above[i - 1]:
            continue
        denom = signal_smooth[i] - signal_smooth[i - 1]
        if abs(denom) < 1e-6:
            continue
        # Linear interpolation for sub-sample precision
        frac = (median_val - signal_smooth[i - 1]) / denom
        t = times_sec[i - 1] + frac * (times_sec[i] - times_sec[i - 1])

        if above[i]:  # crossing upward
            up_times.append(t)
            up_indices.append(i)
        else:  # crossing downward
            down_times.append(t)
            down_indices.append(i)

    if len(up_times) < 3 or len(down_times) < 3:
        return None

    # Gyro magnitude after each crossing (for phase determination by caller)
    gyro_mag = compute_gyro_mag(src_df, sample_rate)
    lookahead = max(int(0.3 * sample_rate), 1)

    def _gyro_after(indices: list[int]) -> list[float]:
        vals = []
        for idx in indices:
            end = min(idx + lookahead, len(gyro_mag))
            vals.append(float(np.mean(gyro_mag[idx:end])) if end > idx else 0.0)
        return vals

    return (
        up_times,
        down_times,
        _gyro_after(up_indices),
        _gyro_after(down_indices),
        channel,
    )


# ─── Per-Interval Analysis ───────────────────────────────────────────────────


def _searchsorted_nearest(sorted_arr: np.ndarray, value: float) -> tuple[int, float]:
    """Find the index and distance of the nearest element in a sorted array.

    Returns (index, abs_distance). Returns (-1, inf) if array is empty.
    """
    if len(sorted_arr) == 0:
        return -1, float("inf")
    idx = np.searchsorted(sorted_arr, value)
    best_idx = -1
    best_diff = float("inf")
    for candidate in (idx - 1, idx):
        if 0 <= candidate < len(sorted_arr):
            diff = abs(sorted_arr[candidate] - value)
            if diff < best_diff:
                best_diff = diff
                best_idx = candidate
    return best_idx, best_diff


def process_interval(
    imu_df: pd.DataFrame,
    gps_df: pd.DataFrame | None,
    interval_label: str,
) -> pd.DataFrame:
    """Process one interval: detect strokes, measure async, match GPS speed.

    Returns DataFrame with per-stroke rows containing:
        interval, stroke_num, timestamp, spm, async_ms, speed_ms,
        seat1_catch .. seat4_catch, n_sensors
    """
    if imu_df.empty or "source" not in imu_df.columns:
        return pd.DataFrame()

    sources = sorted(imu_df["source"].unique())
    if len(sources) < 2:
        return pd.DataFrame()

    # ── Step 1: Collect median crossings for all sensors ──

    # Store both crossing directions per source for later phase alignment
    sensor_crossings: dict[str, tuple[list[float], list[float], float, float]] = {}
    # Store rowing-channel signal + times per source for per-stroke range
    # filtering (Step 5).  The channel may differ per sensor depending on
    # how it is mounted on the oar.
    source_rowing_signal: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    for source in sources:
        src_df = (
            imu_df[imu_df["source"] == source].sort_values("time").reset_index(drop=True)
        )

        src_rate = estimate_sample_rate(src_df["time"])
        if src_rate < 10 or np.isnan(src_rate):
            continue
        if len(src_df) < int(2 * src_rate):
            continue

        gyro_mag = compute_gyro_mag(src_df, src_rate)

        active_windows = detect_active_windows(gyro_mag, src_rate)
        if not active_windows:
            continue

        times_epoch = src_df["time"].values.astype("datetime64[ns]").astype("int64") / 1e9

        # Select the dominant orientation channel once per sensor using
        # the full signal, then reuse it for every active window.
        rowing_channel = _select_rowing_channel(src_df, src_rate)

        if rowing_channel is not None and rowing_channel in src_df.columns:
            source_rowing_signal[source] = (
                times_epoch,
                src_df[rowing_channel]
                .astype(float)
                .interpolate(limit_direction="both")
                .values,
            )

        all_up: list[float] = []
        all_down: list[float] = []
        gyro_up: list[float] = []
        gyro_down: list[float] = []

        for ws, we in active_windows:
            result = find_rowing_crossings(
                src_df.iloc[ws:we], times_epoch[ws:we], src_rate,
                channel=rowing_channel,
            )
            if result is None:
                continue
            up_t, down_t, ug, dg, _ch = result
            all_up.extend(up_t)
            all_down.extend(down_t)
            gyro_up.extend(ug)
            gyro_down.extend(dg)

        if not gyro_up or not gyro_down:
            continue

        sensor_crossings[source] = (
            all_up, all_down, float(np.mean(gyro_up)), float(np.mean(gyro_down))
        )

    if len(sensor_crossings) < MIN_SENSORS_FOR_ASYNC:
        return pd.DataFrame()

    # ── Step 2: Pick reference sensor, determine its phase via gyro ──

    ref_source = max(
        sensor_crossings,
        key=lambda s: len(sensor_crossings[s][0]) + len(sensor_crossings[s][1]),
    )
    ref_up, ref_down, ref_ug, ref_dg = sensor_crossings[ref_source]
    ref_catches = np.array(sorted(ref_up if ref_ug > ref_dg else ref_down))

    if len(ref_catches) < 2:
        return pd.DataFrame()

    # ── Step 3: Align other sensors by matching to reference catches ──

    source_catches: dict[str, np.ndarray] = {ref_source: ref_catches}

    for source, (s_up, s_down, _, _) in sensor_crossings.items():
        if source == ref_source:
            continue
        up_arr = np.array(sorted(s_up))
        down_arr = np.array(sorted(s_down))

        # For a sample of reference catches, measure how close each
        # direction's crossings are. Pick the direction with tighter alignment.
        up_dists = [_searchsorted_nearest(up_arr, rc)[1] for rc in ref_catches]
        down_dists = [_searchsorted_nearest(down_arr, rc)[1] for rc in ref_catches]

        if np.median(up_dists) <= np.median(down_dists):
            source_catches[source] = up_arr
        else:
            source_catches[source] = down_arr

    if len(source_catches) < MIN_SENSORS_FOR_ASYNC:
        return pd.DataFrame()

    # ── Step 4: Remove double-crossings (adaptive minimum interval) ──

    for source in list(source_catches):
        arr = source_catches[source]
        if len(arr) < 3:
            continue
        median_gap = np.median(np.diff(arr))
        min_gap = median_gap * 0.5
        filtered = [arr[0]]
        for i in range(1, len(arr)):
            if arr[i] - filtered[-1] >= min_gap:
                filtered.append(arr[i])
        source_catches[source] = np.array(filtered)

    # ── Step 5: Filter drill strokes by per-stroke signal range ──
    # Drills (arms only, body swing, half slide) have shorter arcs in the
    # dominant orientation channel.  For each sensor, compute the 75th
    # percentile of per-stroke range and discard catches whose stroke has
    # range < 60% of that reference.

    for source in list(source_catches):
        if source not in source_rowing_signal:
            continue
        s_times, s_pitch = source_rowing_signal[source]
        arr = source_catches[source]
        if len(arr) < 4:
            continue

        # Compute pitch range per stroke
        stroke_ranges = []
        for i in range(len(arr) - 1):
            mask = (s_times >= arr[i]) & (s_times < arr[i + 1])
            if mask.any():
                p = s_pitch[mask]
                stroke_ranges.append(float(np.ptp(p)))
            else:
                stroke_ranges.append(0.0)

        if not stroke_ranges:
            continue

        ref_range = np.percentile(stroke_ranges, 75)
        if ref_range < 5.0:
            continue
        min_range = ref_range * 0.6

        # Keep only catches whose following stroke meets the range threshold
        keep = [i for i, r in enumerate(stroke_ranges) if r >= min_range]
        # Always keep the last catch (no following stroke to measure)
        keep_set = set(keep) | {len(arr) - 1}
        source_catches[source] = arr[sorted(keep_set)]

    # ── Reference sensor & seat mapping ──

    all_sorted_sources = sorted(source_catches.keys())
    if len(ref_catches) < 2:
        return pd.DataFrame()

    seat_map = {s: i + 1 for i, s in enumerate(all_sorted_sources[:4])}
    seat_to_source = {sn: s for s, sn in seat_map.items()}
    other_sources = [s for s in all_sorted_sources if s != ref_source]

    # ── Prepare GPS speed lookup (sorted arrays for searchsorted) ──

    gps_times: np.ndarray | None = None
    gps_speeds: np.ndarray | None = None
    if gps_df is not None and not gps_df.empty and "speed" in gps_df.columns:
        gps_clean = gps_df.dropna(subset=["speed"])
        if "accuracy" in gps_clean.columns:
            gps_clean = gps_clean[gps_clean["accuracy"] <= GPS_ACCURACY_MAX_M]
        if len(gps_clean) >= 2:
            gps_clean = gps_clean.sort_values("time")
            gps_times = gps_clean["time"].values.astype("datetime64[ns]").astype("int64") / 1e9
            gps_speeds = gps_clean["speed"].values.astype(float)

    # ── Match catches across sensors (1:1 greedy, searchsorted) ──

    rows = []
    consumed: dict[str, set[int]] = {s: set() for s in other_sources}

    for i in range(len(ref_catches) - 1):
        ref_t = ref_catches[i]
        stroke_period = ref_catches[i + 1] - ref_t

        if stroke_period <= 0:
            continue

        spm = 60.0 / stroke_period
        if spm < 10 or spm > MAX_SPM:
            continue

        tolerance = stroke_period * CATCH_MATCH_TOLERANCE

        catch_epochs_matched: dict[str, float] = {ref_source: ref_t}

        for other in other_sources:
            o_arr = source_catches[other]
            idx = np.searchsorted(o_arr, ref_t)

            best_idx = -1
            best_diff = float("inf")
            for candidate in (idx - 1, idx):
                if (
                    0 <= candidate < len(o_arr)
                    and candidate not in consumed[other]
                ):
                    diff = abs(o_arr[candidate] - ref_t)
                    if diff < best_diff:
                        best_diff = diff
                        best_idx = candidate

            if best_idx >= 0 and best_diff <= tolerance:
                catch_epochs_matched[other] = o_arr[best_idx]
                consumed[other].add(best_idx)

        if len(catch_epochs_matched) < MIN_SENSORS_FOR_ASYNC:
            continue

        epochs = list(catch_epochs_matched.values())
        async_ms = (max(epochs) - min(epochs)) * 1000

        # GPS speed via searchsorted
        speed_ms = np.nan
        if gps_times is not None:
            gps_idx, gps_diff = _searchsorted_nearest(gps_times, ref_t)
            if gps_idx >= 0 and gps_diff < GPS_MATCH_TOLERANCE_SEC:
                speed_ms = float(gps_speeds[gps_idx])

        row: dict = {
            "interval": interval_label,
            "stroke_num": i + 1,
            "timestamp": pd.Timestamp(ref_t, unit="s", tz="UTC"),
            "spm": round(spm, 1),
            "async_ms": round(async_ms, 1),
            "speed_ms": round(speed_ms, 3) if not np.isnan(speed_ms) else np.nan,
            "n_sensors": len(catch_epochs_matched),
        }

        for seat_num in range(1, 5):
            src = seat_to_source.get(seat_num)
            if src and src in catch_epochs_matched:
                row[f"seat{seat_num}_catch"] = pd.Timestamp(
                    catch_epochs_matched[src], unit="s", tz="UTC"
                )
            else:
                row[f"seat{seat_num}_catch"] = pd.NaT

        rows.append(row)

    return pd.DataFrame(rows)


# ─── Theoretical Model ───────────────────────────────────────────────────────


def theoretical_speed_loss(spm: float, async_ms: float) -> float:
    """Compute theoretical speed loss fraction for given SPM and async offset.

    Linear model: speed_loss = k * delta, where delta = omega * dt.
    Returns fractional loss (e.g. 0.015 = 1.5%).
    """
    omega = 2 * np.pi * spm / 60  # stroke angular frequency (rad/s)
    delta = omega * async_ms / 1000  # phase offset (rad)
    return THEORY_K * delta


# ─── Per-Piece Normalization ─────────────────────────────────────────────────


def find_stable_spm_runs(df: pd.DataFrame) -> pd.DataFrame:
    """Find stable-SPM pieces and compute per-piece normalized speed.

    A "piece" is a maximal run of consecutive strokes within a single interval
    where every stroke's SPM is within ±STABLE_SPM_TOLERANCE of the running mean.

    Adds columns: run_id, run_mean_speed, v_rel, delta, delta_piece_mean.
    Returns only strokes belonging to valid runs (≥MIN_RUN_LENGTH strokes).
    """
    result_parts = []

    for interval_label, int_df in df.groupby("interval"):
        int_df = int_df.sort_values("timestamp").reset_index(drop=True)
        interval_hash = str(abs(hash(interval_label)))[:8]

        # Build runs: greedy sequential grouping
        runs: list[list[int]] = []  # list of lists of row indices
        current_run: list[int] = [0]
        current_spm_sum = int_df.loc[0, "spm"]

        for i in range(1, len(int_df)):
            run_mean = current_spm_sum / len(current_run)
            stroke_spm = int_df.loc[i, "spm"]
            if abs(stroke_spm - run_mean) <= STABLE_SPM_TOLERANCE:
                current_run.append(i)
                current_spm_sum += stroke_spm
            else:
                runs.append(current_run)
                current_run = [i]
                current_spm_sum = stroke_spm
        runs.append(current_run)

        # Filter to runs with enough strokes
        run_num = 0
        for run_indices in runs:
            if len(run_indices) < MIN_RUN_LENGTH:
                continue

            run_df = int_df.iloc[run_indices].copy()

            # Mean speed for this piece (ignoring NaN)
            speeds = run_df["speed_ms"].dropna()
            if len(speeds) == 0:
                continue
            run_mean_speed = speeds.mean()
            if run_mean_speed <= 0 or np.isnan(run_mean_speed):
                continue

            run_id = f"{interval_hash}_{run_num}"
            run_num += 1

            # δ per stroke (phase offset in radians)
            omega = 2 * np.pi * run_df["spm"].values / 60
            delta = omega * run_df["async_ms"].values / 1000

            run_df["run_id"] = run_id
            run_df["run_mean_speed"] = run_mean_speed
            run_df["v_rel"] = run_df["speed_ms"] / run_mean_speed
            run_df["delta"] = delta
            run_df["delta_piece_mean"] = np.nanmean(delta)

            result_parts.append(run_df)

    if not result_parts:
        return pd.DataFrame()

    return pd.concat(result_parts, ignore_index=True)


# ─── Output ──────────────────────────────────────────────────────────────────


def print_results(all_strokes: pd.DataFrame, total_strokes: int):
    """Print analysis results using per-piece normalization."""
    n_intervals = all_strokes["interval"].nunique()
    n_in_pieces = len(all_strokes)
    n_runs = all_strokes["run_id"].nunique()

    print()
    print("=" * 64)
    print("  EMPIRICAL ROWING ASYNCHRONICITY ANALYSIS")
    print(f"  Double scull (2x) — {n_intervals} intervals, {total_strokes} total strokes")
    print(f"  In pieces: {n_in_pieces} strokes across {n_runs} stable-SPM pieces")
    print("=" * 64)

    has_vrel = all_strokes["v_rel"].notna().sum() > 0

    for spm_lo, spm_hi in SPM_BUCKETS:
        spm_mask = (all_strokes["spm"] >= spm_lo) & (all_strokes["spm"] < spm_hi)
        bucket_df = all_strokes[spm_mask]

        if len(bucket_df) < 5:
            continue

        n_pieces = bucket_df["run_id"].nunique()
        print(
            f"\n=== {spm_lo}-{spm_hi} SPM "
            f"(N={len(bucket_df)} strokes in {n_pieces} pieces) ==="
        )

        if has_vrel:
            print(
                f"\n  {'Δt(ms)':<10}| {'Count':>5} | "
                f"{'Empirical':>9} | {'±SE':>7} | "
                f"{'Theory':>7} | {'t(SEs off)':>10} | {'p(noise?)':>9}"
            )
            print(
                f"  {'-' * 10}|{'-' * 7}|"
                f"{'-' * 11}|{'-' * 9}|"
                f"{'-' * 9}|{'-' * 12}|{'-' * 10}"
            )
        else:
            print(
                f"\n  {'Δt(ms)':<10}| {'Count':>5} | "
                f"{'Med Δt':>7}"
            )
            print(f"  {'-' * 10}|{'-' * 7}|{'-' * 9}")

        for async_lo, async_hi in ASYNC_BUCKETS_MS:
            async_mask = (bucket_df["async_ms"] >= async_lo) & (
                bucket_df["async_ms"] < async_hi
            )
            async_df = bucket_df[async_mask]
            if len(async_df) < MIN_STROKES_PER_BUCKET:
                continue

            count = len(async_df)
            label = f"{int(async_lo)}-{int(async_hi)}"

            if has_vrel:
                vrel_valid = async_df.dropna(subset=["v_rel"])
                if len(vrel_valid) == 0:
                    continue

                mean_vrel = vrel_valid["v_rel"].mean()
                empirical_pct = (mean_vrel - 1.0) * 100
                se_pct = vrel_valid["v_rel"].std() / np.sqrt(len(vrel_valid)) * 100

                # Per-stroke theoretical v_rel: (1 - k·δ) / (1 - k·δ̄_piece)
                k = THEORY_K
                theory_vrel_per_stroke = (
                    (1 - k * vrel_valid["delta"])
                    / (1 - k * vrel_valid["delta_piece_mean"])
                )
                theory_pct = (theory_vrel_per_stroke.mean() - 1.0) * 100

                # One-sample t-test: is mean(v_rel - v_rel_theory) != 0?
                residuals = vrel_valid["v_rel"].values - theory_vrel_per_stroke.values
                t_stat, p_val = scipy_stats.ttest_1samp(residuals, 0.0)

                sig = ""
                if p_val < 0.01:
                    sig = "**"
                elif p_val < 0.05:
                    sig = "*"

                print(
                    f"  {label:<10}| {count:>5} | "
                    f"{empirical_pct:>+8.1f}% | {se_pct:>6.1f}% | "
                    f"{theory_pct:>+6.1f}% | {t_stat:>+9.1f} | "
                    f"{p_val:>8.3f}{sig}"
                )
            else:
                med_async = async_df["async_ms"].median()
                print(
                    f"  {label:<10}| {count:>5} | "
                    f"{med_async:>6.0f}ms"
                )

    # ── Summary stats ──
    print("\n" + "=" * 64)
    print("  SUMMARY")
    print("=" * 64)
    print(f"  Total intervals analyzed: {n_intervals}")
    print(f"  Total strokes:            {total_strokes}")
    print(f"  Strokes in pieces:        {n_in_pieces} ({n_runs} pieces)")
    print(
        f"  SPM range:                "
        f"{all_strokes['spm'].min():.0f} – {all_strokes['spm'].max():.0f}"
    )
    print(
        f"  Async range:              "
        f"{all_strokes['async_ms'].min():.0f} – {all_strokes['async_ms'].max():.0f} ms"
    )
    print(f"  Mean async:               {all_strokes['async_ms'].mean():.0f} ms")
    print(f"  Median async:             {all_strokes['async_ms'].median():.0f} ms")

    if has_vrel:
        valid = all_strokes.dropna(subset=["v_rel"])
        if len(valid) > 0:
            print(f"\n  Speed data points:        {len(valid)}")
            print(f"  Mean speed:               {valid['speed_ms'].mean():.2f} m/s")
            print(f"  Mean v_rel:               {valid['v_rel'].mean():.4f}")

            if len(valid) > 20:
                _compute_r_squared(valid)

    # ── Interpretation ──
    print()
    print("─" * 64)
    print("  HOW TO READ THIS TABLE")
    print("─" * 64)
    print("""
  Each stroke's GPS speed is normalized by its piece mean (v_rel),
  removing confounds like wind, current, and effort. v_rel > 1 means
  faster than the piece average; v_rel < 1 means slower.

  Empirical = (mean v_rel - 1) x 100%. Negative = speed loss.
  ±SE       = standard error of the empirical mean.
  Theory    = predicted v_rel deviation from the linear model
              speed_loss = k * delta, where delta = omega * async.
  t         = t-statistic testing H0: empirical = theory.
              |t| > 2 suggests the model doesn't fit that bucket.
  p         = p-value for the t-test. * = p<0.05, ** = p<0.01.

  If p > 0.05, the empirical and theory values are statistically
  consistent — we cannot reject the model for that bucket. If
  p < 0.05, the model under- or over-predicts at that async level.

  R² (fixed k)  = goodness-of-fit using the simulator-calibrated k.
  R² (fitted k) = goodness-of-fit with k fitted to the data.
  If fitted k >> fixed k, the real coupling is stronger than the
  simulator predicts; the model shape may still be correct.
""")


def _compute_r_squared(valid: pd.DataFrame):
    """Compute R² between empirical and theoretical v_rel deviations per bucket.

    Two R² values:
    - Fixed-k R²: using calibrated THEORY_K with per-stroke v_rel_theory
    - Fitted-k R²: fitting k via least-squares on (δ - δ̄_piece) per bucket
    """
    empirical_devs = []    # mean(v_rel) - 1.0 per bucket
    theory_devs = []       # mean(v_rel_theory) - 1.0 per bucket (fixed k)
    bucket_delta_excess = []  # mean(δ) - mean(δ̄_piece) per bucket

    k = THEORY_K

    for spm_lo, spm_hi in SPM_BUCKETS:
        spm_mask = (valid["spm"] >= spm_lo) & (valid["spm"] < spm_hi)
        spm_df = valid[spm_mask]
        if len(spm_df) < 5:
            continue

        for async_lo, async_hi in ASYNC_BUCKETS_MS:
            a_mask = (spm_df["async_ms"] >= async_lo) & (spm_df["async_ms"] < async_hi)
            a_df = spm_df[a_mask]
            if len(a_df) < MIN_STROKES_PER_BUCKET:
                continue

            vrel_valid = a_df.dropna(subset=["v_rel"])
            if len(vrel_valid) < MIN_STROKES_PER_BUCKET:
                continue

            emp_dev = vrel_valid["v_rel"].mean() - 1.0

            # Per-stroke theoretical v_rel: (1 - k·δ) / (1 - k·δ̄_piece)
            theory_vrel = (
                (1 - k * vrel_valid["delta"])
                / (1 - k * vrel_valid["delta_piece_mean"])
            )
            th_dev = theory_vrel.mean() - 1.0

            # δ excess: how much this bucket's δ exceeds its pieces' means
            delta_excess = (
                vrel_valid["delta"].mean() - vrel_valid["delta_piece_mean"].mean()
            )

            empirical_devs.append(emp_dev)
            theory_devs.append(th_dev)
            bucket_delta_excess.append(delta_excess)

    if len(empirical_devs) < 3:
        return

    emp = np.array(empirical_devs)
    th = np.array(theory_devs)
    dx = np.array(bucket_delta_excess)

    # Fixed-k R²
    ss_res_fixed = np.sum((emp - th) ** 2)
    ss_tot = np.sum((emp - np.mean(emp)) ** 2)
    if ss_tot > 0:
        r2_fixed = 1 - ss_res_fixed / ss_tot
    else:
        r2_fixed = np.nan

    # Fitted-k: v_rel - 1 ≈ -k · (δ - δ̄_piece)
    # Minimize Σ(emp_dev + k_fit · delta_excess)²
    # Closed-form: k_fit = -Σ(emp · dx) / Σ(dx²) = Σ(loss · dx) / Σ(dx²)
    emp_loss = -emp  # loss = 1 - v_rel
    sum_dx2 = np.sum(dx ** 2)
    if sum_dx2 > 0:
        k_fit = np.sum(emp_loss * dx) / sum_dx2
    else:
        k_fit = np.nan

    # Fitted-k R²: predicted dev = -k_fit · delta_excess
    if not np.isnan(k_fit):
        th_fitted = -k_fit * dx
        ss_res_fitted = np.sum((emp - th_fitted) ** 2)
        r2_fitted = 1 - ss_res_fitted / ss_tot if ss_tot > 0 else np.nan
    else:
        r2_fitted = np.nan

    print(f"\n  R² (fixed k={THEORY_K:.4f}):     {r2_fixed:.2f}")
    if not np.isnan(k_fit):
        print(f"  R² (fitted k={k_fit:.4f}):    {r2_fitted:.2f}")
        print(f"  Fitted k:                 {k_fit:.4f}  (theory: {THEORY_K:.4f})")
        ratio = k_fit / THEORY_K if THEORY_K > 0 else np.nan
        print(f"  k ratio (fit/theory):     {ratio:.2f}")


# ─── Main ────────────────────────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    """Format byte count as human-readable KB/MB."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _intervals_from_cache(cache: CacheService) -> list[dict]:
    """Build interval list from local cache metadata (no network calls).

    Only includes intervals whose parquet files actually exist on disk,
    so a stale meta.json entry won't cause a surprise network fetch.
    """
    intervals = []
    for meta in cache.meta["intervals"].values():
        if not meta.get("imu_cached"):
            continue
        interval = {
            "tag": meta["tag"],
            "value": meta["value"],
            "label": meta.get("label", meta["value"]),
        }
        if not cache._imu_path(interval).exists():
            continue
        intervals.append(interval)
    intervals.sort(key=lambda x: x["value"])
    return intervals


def _esc_flux(value: str) -> str:
    """Escape string for Flux queries."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _mark_cached(cache: CacheService, interval: dict, data_type: str, n_rows: int):
    """Update cache meta after download (mirrors CacheService internals)."""
    key = cache._interval_key(interval)
    if key not in cache.meta["intervals"]:
        cache.meta["intervals"][key] = {
            "tag": interval["tag"],
            "value": interval["value"],
            "label": interval.get("label", interval["value"]),
        }
    entry = cache.meta["intervals"][key]
    entry[f"{data_type}_cached"] = True
    entry[f"{data_type}_rows"] = n_rows
    entry["cached_at"] = datetime.now(timezone.utc).isoformat()
    cache._save_meta()


def _download_imu_streaming(
    settings, cache: CacheService, interval: dict, line_prefix: str,
) -> tuple[int, int]:
    """Download IMU data with streaming progress and longer timeout.

    Uses query_stream() to show record count as data flows in.
    Returns (parquet_file_size_bytes, n_raw_records).
    """
    safe_tag = _esc_flux(interval["tag"])
    safe_value = _esc_flux(interval["value"])
    safe_bucket = _esc_flux(settings.bucket)

    flux = f'''
from(bucket: "{safe_bucket}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "imu")
  |> filter(fn: (r) => r["{safe_tag}"] == "{safe_value}")
  |> sort(columns: ["_time"])
'''

    client = influxdb_client.InfluxDBClient(
        url=settings.url,
        token=settings.token,
        org=settings.effective_org(),
        ssl_ca_cert=certifi.where(),
        timeout=DOWNLOAD_TIMEOUT_MS,
    )

    rows = []
    count = 0

    try:
        stream = client.query_api().query_stream(
            flux, org=settings.effective_org()
        )
        for record in stream:
            vals = record.values
            rows.append({
                "time": vals.get("_time"),
                "field": vals.get("_field"),
                "value": vals.get("_value"),
                "source": InfluxService._source_label(vals),
            })
            count += 1
            if count % 500 == 0:
                print(
                    f"\r{line_prefix}{count:,} records\033[K",
                    end="", flush=True,
                )
    finally:
        client.close()

    if not rows:
        _mark_cached(cache, interval, "imu", 0)
        return 0, count

    df_long = pd.DataFrame(rows)
    df_wide = df_long.pivot_table(
        index=["time", "source"],
        columns="field",
        values="value",
    ).reset_index()
    df = df_wide.sort_values(["time", "source"])

    parquet_path = cache._imu_path(interval)
    df.to_parquet(parquet_path, index=False)
    _mark_cached(cache, interval, "imu", len(df))

    return parquet_path.stat().st_size, count


def main():
    parser = argparse.ArgumentParser(
        description="Empirical Rowing Asynchronicity Analyzer"
    )
    parser.add_argument(
        "--env", default=".env", help="Path to .env file (default: .env)"
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading, use cached data only (no network calls)",
    )
    parser.add_argument(
        "--intervals",
        nargs="+",
        help="Only process these interval IDs (e.g. Interval_1771040451989)",
    )
    args = parser.parse_args()

    # ── Phase 1: Setup & Download ──

    print("Loading settings...")
    settings = load_settings(args.env)

    influx = InfluxService(settings)
    location = LocationService(settings)
    cache = CacheService(influx, location, cache_dir=settings.cache_dir)

    if args.skip_download:
        intervals = _intervals_from_cache(cache)
        print(f"Using {len(intervals)} cached intervals (offline mode)\n")
    else:
        print("Syncing interval list...")
        intervals = cache.sync_interval_list()
        print(f"Found {len(intervals)} intervals")
        print("(Re-run to resume if interrupted — cached intervals are skipped)\n")

        total_imu_bytes = 0
        total_gps_bytes = 0
        n_downloaded = 0
        n_cached = 0

        try:
            for i, interval in enumerate(intervals, 1):
                label = interval.get("label", interval["value"])
                imu_path = cache._imu_path(interval)
                gps_path = cache._location_path(interval)

                cached_status = cache.is_cached(interval)
                both_done = cached_status["imu"] and cached_status["location"]

                if both_done:
                    imu_size = imu_path.stat().st_size if imu_path.exists() else 0
                    gps_size = gps_path.stat().st_size if gps_path.exists() else 0
                    total_imu_bytes += imu_size
                    total_gps_bytes += gps_size
                    n_cached += 1
                    print(
                        f"  [{i}/{len(intervals)}] {label} — "
                        f"cached ({_fmt_bytes(imu_size + gps_size)})"
                    )
                    continue

                prefix = f"  [{i}/{len(intervals)}] {label} — "
                print(prefix, end="", flush=True)

                # IMU: streaming download with retry
                imu_size = 0
                n_rec = 0
                if not cached_status["imu"]:
                    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
                        try:
                            imu_size, n_rec = _download_imu_streaming(
                                settings, cache, interval, prefix,
                            )
                            break
                        except Exception as e:
                            print(f"\r{prefix}attempt {attempt} failed\033[K")
                            if attempt < MAX_DOWNLOAD_RETRIES:
                                print(f"    {e}")
                                print(prefix, end="", flush=True)
                            else:
                                print(f"    {e}")
                else:
                    imu_size = imu_path.stat().st_size if imu_path.exists() else 0

                # GPS: small data, no streaming needed
                gps_size = 0
                if not cached_status["location"]:
                    try:
                        cache.get_location_data(interval)
                        gps_size = gps_path.stat().st_size if gps_path.exists() else 0
                    except Exception as e:
                        print(f"\r{prefix}GPS error: {e}\033[K")
                else:
                    gps_size = gps_path.stat().st_size if gps_path.exists() else 0

                total_imu_bytes += imu_size
                total_gps_bytes += gps_size
                n_downloaded += 1

                rec_note = f" ({n_rec:,} records)" if n_rec else ""
                print(
                    f"\r{prefix}"
                    f"{_fmt_bytes(imu_size)} IMU + {_fmt_bytes(gps_size)} GPS"
                    f"{rec_note}\033[K"
                )

        except KeyboardInterrupt:
            total = total_imu_bytes + total_gps_bytes
            print(
                f"\n\n  Total so far: {_fmt_bytes(total)} "
                f"({_fmt_bytes(total_imu_bytes)} IMU, "
                f"{_fmt_bytes(total_gps_bytes)} GPS) "
                f"— {n_downloaded} downloaded, {n_cached} already cached"
            )
            print("  Re-run to resume — cached intervals are skipped.")
            sys.exit(130)

        total = total_imu_bytes + total_gps_bytes
        print(
            f"\n  Total: {_fmt_bytes(total)} "
            f"({_fmt_bytes(total_imu_bytes)} IMU, {_fmt_bytes(total_gps_bytes)} GPS) "
            f"— {n_downloaded} downloaded, {n_cached} already cached"
        )

    if args.intervals:
        allowed = set(args.intervals)
        intervals = [iv for iv in intervals if iv["value"] in allowed]
        print(f"Filtered to {len(intervals)} interval(s): {[iv.get('label', iv['value']) for iv in intervals]}")

    if not intervals:
        print("No intervals found.")
        sys.exit(1)

    # ── Phase 2: Process each interval ──

    print("\n" + "─" * 64)
    print("  Processing intervals...")
    print("─" * 64 + "\n")

    all_stroke_data: list[pd.DataFrame] = []

    for i, interval in enumerate(intervals, 1):
        label = interval.get("label", interval["value"])

        # Only read from cache — never trigger a surprise network download
        imu_path = cache._imu_path(interval)
        if not imu_path.exists():
            continue

        try:
            imu_df = pd.read_parquet(imu_path)
        except Exception:
            imu_df = pd.DataFrame()

        if imu_df.empty:
            continue

        # Trim warmup from Interval_1771040451989 (first 90s is drill/warmup)
        if interval["value"] == "Interval_1771040451989" and "time" in imu_df.columns:
            t0 = imu_df["time"].min()
            cutoff = t0 + pd.Timedelta(seconds=90)
            n_before = len(imu_df)
            imu_df = imu_df[imu_df["time"] >= cutoff].reset_index(drop=True)
            print(f" (trimmed {n_before - len(imu_df)} warmup rows)", end="")

        try:
            gps_df = cache.get_location_data(interval)
        except Exception:
            gps_df = None

        n_gps = len(gps_df) if gps_df is not None and not gps_df.empty else 0
        print(
            f"  [{i}/{len(intervals)}] {label} "
            f"({len(imu_df)} IMU, {n_gps} GPS)...",
            end="",
            flush=True,
        )

        try:
            stroke_df = process_interval(imu_df, gps_df, label)
        except Exception as e:
            print(f" ERROR: {e}")
            continue

        if stroke_df.empty:
            print(" no usable strokes")
            continue

        if len(stroke_df) < MIN_STROKES_PER_INTERVAL:
            print(f" {len(stroke_df)} strokes (need {MIN_STROKES_PER_INTERVAL})")
            continue

        print(
            f" {len(stroke_df)} strokes "
            f"(SPM {stroke_df['spm'].min():.0f}–{stroke_df['spm'].max():.0f}, "
            f"async {stroke_df['async_ms'].median():.0f}ms med)"
        )

        all_stroke_data.append(stroke_df)

    if not all_stroke_data:
        print("\nNo usable stroke data found across any intervals.")
        sys.exit(1)

    all_strokes = pd.concat(all_stroke_data, ignore_index=True)
    total_strokes = len(all_strokes)

    # ── Phase 3: Filter and per-piece normalization ──

    n_before = len(all_strokes)
    all_strokes = all_strokes[all_strokes["async_ms"] <= MAX_ASYNC_MS].reset_index(drop=True)
    n_filtered = n_before - len(all_strokes)
    if n_filtered > 0:
        print(f"  Filtered {n_filtered} strokes with async > {MAX_ASYNC_MS}ms")

    # Remove warmup/drill strokes with low boat speed
    MIN_SPEED_MS = 2.0
    n_before_speed = len(all_strokes)
    slow_mask = all_strokes["speed_ms"].notna() & (all_strokes["speed_ms"] < MIN_SPEED_MS)
    all_strokes = all_strokes[~slow_mask].reset_index(drop=True)
    n_slow = n_before_speed - len(all_strokes)
    if n_slow > 0:
        print(f"  Filtered {n_slow} strokes with speed < {MIN_SPEED_MS} m/s (warmup/drill)")

    all_strokes = find_stable_spm_runs(all_strokes)
    if all_strokes.empty:
        print("\nNo stable-SPM pieces found (need runs of ≥{} strokes).".format(MIN_RUN_LENGTH))
        sys.exit(1)

    n_runs = all_strokes["run_id"].nunique()
    n_in_runs = all_strokes["v_rel"].notna().sum()
    print(f"  Found {n_runs} stable-SPM pieces ({n_in_runs} strokes with speed data)")

    # ── Phase 4: Output ──

    print_results(all_strokes, total_strokes)

    # CSV export
    csv_path = Path("async_analysis_results.csv")
    export_cols = [
        "interval",
        "stroke_num",
        "timestamp",
        "spm",
        "async_ms",
        "speed_ms",
        "run_id",
        "run_mean_speed",
        "v_rel",
        "seat1_catch",
        "seat2_catch",
        "seat3_catch",
        "seat4_catch",
    ]
    existing_cols = [c for c in export_cols if c in all_strokes.columns]
    all_strokes[existing_cols].to_csv(csv_path, index=False)
    print(f"Raw data exported to {csv_path}")


if __name__ == "__main__":
    main()
