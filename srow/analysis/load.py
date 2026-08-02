"""Memory-bounded streaming loader for raw per-sensor IMU (RESEARCH.md §6.1).

The non-streaming InfluxService.load_* materializes all Flux records + a
pivot_table and OOMs on a full raw interval (~GBs). This streams with
query_stream, accumulates plain float lists per (source, field), and assembles
numpy arrays — peak memory is the final data size (tens of MB), not the record
overhead. Only accel+gyro are fetched (the engine's synthetic frame needs no
Euler angles).
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict

import certifi
import influxdb_client
import numpy as np

from srow.services.influx_service import InfluxService

_FIELDS = ("ax", "ay", "az", "wx", "wy", "wz")
_QUERY_TIMEOUT_MS = 120_000


def _rfc3339(t: _dt.datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_raw_by_source(settings, tag: str, value: str,
                       start: _dt.datetime, stop: _dt.datetime) -> dict[str, dict]:
    """Stream raw accel+gyro for one interval, grouped by sensor.

    Returns {source: {"times_s": (N,), "accel": (N,3), "gyro": (N,3)}}.
    """
    esc = str(value).replace("\\", "\\\\").replace('"', '\\"')
    bucket = settings.bucket.replace("\\", "\\\\").replace('"', '\\"')
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {_rfc3339(start)}, stop: {_rfc3339(stop)})
  |> filter(fn: (r) => r._measurement == "imu")
  |> filter(fn: (r) => r["{tag}"] == "{esc}")
  |> filter(fn: (r) => contains(value: r._field, set: ["ax","ay","az","wx","wy","wz"]))
  |> sort(columns: ["_time"])
'''
    client = influxdb_client.InfluxDBClient(
        url=settings.url, token=settings.token, org=settings.effective_org(),
        ssl_ca_cert=certifi.where(), timeout=_QUERY_TIMEOUT_MS,
    )
    # source -> field -> (times[], values[])
    acc: dict[str, dict[str, tuple[list, list]]] = defaultdict(
        lambda: {f: ([], []) for f in _FIELDS}
    )
    try:
        for rec in client.query_api().query_stream(flux, org=settings.effective_org()):
            v = rec.values
            field = v.get("_field")
            if field not in _FIELDS:
                continue
            src = InfluxService._source_label(v)
            t = v.get("_time")
            acc[src][field][0].append(t.timestamp())
            acc[src][field][1].append(float(v.get("_value")))
    finally:
        client.close()

    out: dict[str, dict] = {}
    for src, fields in acc.items():
        base_t = np.asarray(fields["ax"][0], dtype=float)
        n = base_t.size
        if n < 16:
            continue

        def col(name: str) -> np.ndarray:
            t = np.asarray(fields[name][0], dtype=float)
            x = np.asarray(fields[name][1], dtype=float)
            if t.size == n:
                return x
            if t.size >= 2:  # a field dropped samples — align by time; NaN outside
                return np.interp(base_t, t, x, left=np.nan, right=np.nan)
            return np.full(n, np.nan)

        out[src] = {
            "times_s": base_t,
            "accel": np.column_stack([col("ax"), col("ay"), col("az")]),
            "gyro": np.column_stack([col("wx"), col("wy"), col("wz")]),
        }
    return out
