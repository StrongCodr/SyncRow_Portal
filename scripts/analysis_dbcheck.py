#!/usr/bin/env python3
"""Go STRAIGHT to InfluxDB (no loader, no interp, no axis, no z-score) and measure
whether the bow sensor's raw rows are actually degraded, so we can be 100% sure the
staircase is in the DATA and not manufactured by our pipeline.

Per sensor, in a centered ~40s window, it reports:
  - timestamp spacing  (are samples evenly 10ms apart, or gapped = dropped?)
  - identical-consecutive-value runs on wz & wx  (held/repeated samples?)

If a bow sensor shows long runs of the exact same float while the stroke seat's
values change every sample, the degradation is in the data, full stop.

    python scripts/analysis_dbcheck.py [intervalId]
"""

import datetime as dt
import re
import sys
from collections import defaultdict

import certifi
import influxdb_client
import numpy as np

from srow.config import load_settings
from srow.services import InfluxService
from srow.services.influx_service import InfluxService as _IS


def runs_of_equal(x: np.ndarray):
    """Longest and mean run-length of exactly-equal consecutive values, and the
    fraction of samples identical to their predecessor."""
    if x.size < 2:
        return 0, 0.0, 0.0
    same = x[1:] == x[:-1]
    # run lengths of identical values
    runs, cur = [], 1
    for s in same:
        if s:
            cur += 1
        else:
            runs.append(cur); cur = 1
    runs.append(cur)
    runs = np.array(runs)
    return int(runs.max()), float(runs.mean()), float(same.mean())


def main():
    settings = load_settings()
    influx = InfluxService(settings)
    intervals = influx.fetch_interval_tags()
    target = sys.argv[1] if len(sys.argv) > 1 else intervals[0]["value"]
    iv = next((i for i in intervals if i["value"] == target), intervals[0])
    tag, value = iv["tag"], iv["value"]

    m = re.search(r"(\d{13})", value)
    anchor = dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt.timezone.utc)
    start = anchor - dt.timedelta(days=2)
    stop = anchor + dt.timedelta(days=2)

    esc = str(value).replace("\\", "\\\\").replace('"', '\\"')
    bucket = settings.bucket.replace("\\", "\\\\").replace('"', '\\"')
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {start.strftime("%Y-%m-%dT%H:%M:%SZ")}, stop: {stop.strftime("%Y-%m-%dT%H:%M:%SZ")})
  |> filter(fn: (r) => r._measurement == "imu")
  |> filter(fn: (r) => r["{tag}"] == "{esc}")
  |> filter(fn: (r) => r._field == "wx" or r._field == "wz")
  |> sort(columns: ["_time"])
'''
    client = influxdb_client.InfluxDBClient(
        url=settings.url, token=settings.token, org=settings.effective_org(),
        ssl_ca_cert=certifi.where(), timeout=120_000,
    )
    # source -> field -> (times[], values[])
    acc = defaultdict(lambda: {"wx": ([], []), "wz": ([], [])})
    try:
        for rec in client.query_api().query_stream(flux, org=settings.effective_org()):
            v = rec.values
            f = v.get("_field")
            if f not in ("wx", "wz"):
                continue
            src = _IS._source_label(v)
            acc[src][f][0].append(v.get("_time").timestamp())
            acc[src][f][1].append(float(v.get("_value")))
    finally:
        client.close()

    # pick a common center time across sources
    all_t = np.concatenate([np.asarray(d["wz"][0]) for d in acc.values() if d["wz"][0]])
    center = float(np.median(all_t))
    t0, t1 = center - 20, center + 20

    print(f"interval {value}   window = 40s centered at median sample time\n")
    print(f"{'sensor':<20} {'n':>5} {'dt_med':>7} {'dt_p99':>7} {'gaps':>5} "
          f"{'wz_maxrun':>9} {'wz_%dup':>7} {'wx_%dup':>7}")
    print("-" * 78)
    for src in sorted(acc):
        tw = np.asarray(acc[src]["wz"][0]); zw = np.asarray(acc[src]["wz"][1])
        xw_t = np.asarray(acc[src]["wx"][0]); xw = np.asarray(acc[src]["wx"][1])
        mask = (tw >= t0) & (tw <= t1)
        t = tw[mask]; z = zw[mask]
        xmask = (xw_t >= t0) & (xw_t <= t1)
        x = xw[xmask]
        if t.size < 8:
            print(f"{src:<20} {t.size:>5}  (too few in window)")
            continue
        d = np.diff(t)
        dt_med = np.median(d)
        gaps = int((d > 1.8 * dt_med).sum())
        z_maxrun, _, z_dup = runs_of_equal(z)
        _, _, x_dup = runs_of_equal(x)
        print(f"{src:<20} {t.size:>5} {1000*dt_med:>6.0f}m {1000*np.percentile(d,99):>6.0f}m "
              f"{gaps:>5} {z_maxrun:>9} {100*z_dup:>6.0f}% {100*x_dup:>6.0f}%")

    print("\nread: dt_med≈10ms = 100Hz.  gaps = # of dt jumps >1.8x (dropped samples).")
    print("wz_maxrun = longest run of the EXACT same value; %dup = samples equal to prev.")
    print("clean sensor -> maxrun tiny, %dup ~0.  held/degraded -> long runs, high %dup.")


if __name__ == "__main__":
    main()
