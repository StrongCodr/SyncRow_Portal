#!/usr/bin/env python3
"""Probe the per-sensor analysis path on a real interval (raw resolution).

Usage:
    python scripts/analysis_probe.py [intervalId]

Pulls raw IMU from InfluxDB, builds the synthetic frame per sensor, detects
catches, and prints diagnostics. No writes — read-only validation.
"""

import datetime
import re
import sys

import numpy as np

from srow.config import load_settings
from srow.services import InfluxService
from srow.analysis import build_sensor_frame, detect_catches


def main():
    settings = load_settings()
    influx = InfluxService(settings)
    intervals = influx.fetch_interval_tags()
    if not intervals:
        print("no intervals")
        return
    target = sys.argv[1] if len(sys.argv) > 1 else intervals[0]["value"]
    iv = next((i for i in intervals if i["value"] == target), intervals[0])
    print(f"interval: {iv['value']}  ({iv.get('label')})")

    # Bounded query: derive the window from the interval name (Interval_<epoch_ms>)
    # so Influx scans hours, not the whole 10-year bucket.
    m = re.search(r"(\d{13})", iv["value"])
    if m:
        start = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=datetime.timezone.utc)
        start -= datetime.timedelta(seconds=60)
        end = start + datetime.timedelta(hours=3)
        df = influx.load_time_range(iv["tag"], iv["value"], start, end)
    else:
        df = influx.load_interval(tag_name=iv["tag"], interval_value=iv["value"])
    if df is None or df.empty:
        print("no raw data")
        return
    print(f"raw rows: {len(df)}  sources: {sorted(df['source'].unique())}")
    print()
    print(f"{'sensor':<10} {'rows':>6} {'fs':>6} {'var%':>6} {'domHz':>6} "
          f"{'~spm':>6} {'catch':>6} {'unc_ms':>7} {'feath':>6}")
    print("-" * 72)

    for src, g in df.groupby("source"):
        g = g.sort_values("time")
        need = ("ax", "ay", "az", "wx", "wy", "wz")
        if not all(c in g.columns for c in need):
            print(f"{src:<10}  missing accel/gyro columns")
            continue
        times_s = g["time"].astype("datetime64[ns]").astype("int64").to_numpy() / 1e9
        accel = g[["ax", "ay", "az"]].astype(float).to_numpy()
        gyro = g[["wx", "wy", "wz"]].astype(float).to_numpy()

        frame = build_sensor_frame(times_s, accel, gyro)
        if frame is None:
            print(f"{src:<10} {len(g):>6}  frame build failed")
            continue
        catches = detect_catches(frame.times_s, frame.signal, frame.fs, frame.dominant_hz)
        spm = frame.dominant_hz * 60.0
        unc_ms = 1000.0 * np.median([c.uncertainty_s for c in catches]) if catches else 0.0
        print(f"{str(src):<10} {len(g):>6} {frame.fs:>6.1f} "
              f"{100*frame.variance_explained:>5.1f} {frame.dominant_hz:>6.3f} "
              f"{spm:>6.1f} {len(catches):>6} {unc_ms:>7.1f} "
              f"{'yes' if frame.used_feather_fallback else 'no':>6}")


if __name__ == "__main__":
    main()
