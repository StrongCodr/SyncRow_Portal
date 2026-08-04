#!/usr/bin/env python3
"""Plot RAW gyro channels (wx,wy,wz, deg/s) for the stroke seat vs a bow seat
over the same window, at full sensor resolution. Settles whether the bow pair's
messy synthetic signal comes from messy RAW DATA (sensor/BLE) or from our
processing (PCA/detrend). No smoothing, no z-score — exactly what arrived.

    python scripts/analysis_raw.py [intervalId] [center_frac] [bow_seat_substr]
"""

import datetime
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from srow.config import load_settings
from srow.services import InfluxService
from srow.analysis.load import load_raw_by_source
from srow.analysis.engine import analyze_interval


def main():
    settings = load_settings()
    influx = InfluxService(settings)
    intervals = influx.fetch_interval_tags()
    target = sys.argv[1] if len(sys.argv) > 1 else intervals[0]["value"]
    center = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    bow_sub = sys.argv[3] if len(sys.argv) > 3 else "Seat 1"
    iv = next((i for i in intervals if i["value"] == target), intervals[0])

    m = re.search(r"(\d{13})", iv["value"])
    anchor = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=datetime.timezone.utc)
    by_source = load_raw_by_source(
        settings, iv["tag"], iv["value"],
        anchor - datetime.timedelta(days=2), anchor + datetime.timedelta(days=2),
    )
    ia = analyze_interval(by_source)
    strokes = ia.cross.strokes
    ref = ia.cross.reference
    ci = int(center * len(strokes))
    lo, hi = max(0, ci - 8), min(len(strokes), ci + 8)
    t0 = strokes[lo].stroke_time_s - 1.5
    t1 = strokes[hi - 1].stroke_time_s + 1.5

    bow = next((s for s in by_source if bow_sub.lower() in s.lower()), None)
    pair = [(ref, "STROKE seat (clean-matching)"), (bow, f"{bow} (poorly-matching)")]

    fig, axes = plt.subplots(2, 1, figsize=(16, 9), sharex=True)
    for ax, (src, title) in zip(axes, pair):
        d = by_source[src]
        t = d["times_s"]
        mask = (t >= t0) & (t <= t1)
        x = t[mask] - t0
        g = d["gyro"][mask]
        for j, (lab, col) in enumerate([("wx", "tab:red"), ("wy", "tab:green"), ("wz", "tab:blue")]):
            ax.plot(x, g[:, j], lw=0.9, color=col, label=lab)
        # dt histogram inset text: are samples evenly spaced (no BLE gaps)?
        dt = np.diff(t[mask])
        gap = (dt > 1.8 * np.median(dt)).sum() if dt.size else 0
        ax.set_title(f"{title}   [n={mask.sum()}  median dt={1000*np.median(dt):.0f}ms  "
                     f"{gap} gaps >1.8x]", fontsize=11)
        ax.axhline(0, color="gray", lw=0.4)
        ax.legend(loc="upper right", ncol=3, fontsize=9)
        ax.set_ylabel("deg/s (raw)")
    axes[-1].set_xlabel(f"seconds (window {t1-t0:.0f}s, center={center:.2f})")
    fig.suptitle(f"{iv['value']} — RAW gyro, stroke vs bow (no processing)", fontsize=13)
    fig.tight_layout()
    fig.savefig("/tmp/analysis_raw.png", dpi=110)
    print(f"wrote /tmp/analysis_raw.png  stroke={ref}  bow={bow}  window={t1-t0:.0f}s")


if __name__ == "__main__":
    main()
