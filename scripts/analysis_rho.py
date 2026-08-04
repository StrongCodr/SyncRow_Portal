#!/usr/bin/env python3
"""Distribution of the per-stroke, per-seat cross-correlation peak rho.

Tells us whether `low_confidence` (rho < min_corr) is a threshold artifact or a
genuine signal: what does rho actually look like on real strokes, and how does
the flagged fraction move as the threshold moves?

    python scripts/analysis_rho.py [intervalId]
"""

import datetime
import re
import sys

import numpy as np

from srow.config import load_settings
from srow.services import InfluxService
from srow.analysis.load import load_raw_by_source
from srow.analysis.engine import analyze_interval


def main():
    settings = load_settings()
    influx = InfluxService(settings)
    intervals = influx.fetch_interval_tags()
    if not intervals:
        print("no intervals")
        return
    target = sys.argv[1] if len(sys.argv) > 1 else intervals[0]["value"]
    iv = next((i for i in intervals if i["value"] == target), intervals[0])

    m = re.search(r"(\d{13})", iv["value"])
    anchor = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=datetime.timezone.utc)
    by_source = load_raw_by_source(
        settings, iv["tag"], iv["value"],
        anchor - datetime.timedelta(days=2), anchor + datetime.timedelta(days=2),
    )
    res = analyze_interval(by_source).cross
    if not res.strokes:
        print("no strokes")
        return
    ref = res.reference
    seats = [s for s in res.rowers if s != ref]
    print(f"interval {iv['value']}  ref={ref}  seats={seats}  strokes={len(res.strokes)}")

    # per-seat rho arrays
    print("\nper-seat x-corr peak rho distribution:")
    print(f"{'seat':<20} {'n':>4} {'p10':>6} {'p25':>6} {'med':>6} {'p75':>6} {'p90':>6}")
    all_rho = []
    for s in seats:
        r = np.array([st.corr[s] for st in res.strokes if s in st.corr])
        all_rho.append(r)
        if r.size:
            print(f"{s:<20} {r.size:>4} "
                  f"{np.percentile(r,10):>6.2f} {np.percentile(r,25):>6.2f} "
                  f"{np.median(r):>6.2f} {np.percentile(r,75):>6.2f} "
                  f"{np.percentile(r,90):>6.2f}")

    # min-over-seats rho per stroke (this is what drives the flag)
    per_stroke_min = np.array([
        min(st.corr[s] for s in seats if s in st.corr)
        for st in res.strokes if any(s in st.corr for s in seats)
    ])
    print(f"\nmin-over-seats rho per stroke (drives low_confidence): "
          f"med={np.median(per_stroke_min):.2f}")
    print("flagged fraction vs threshold:")
    for thr in (0.3, 0.4, 0.5, 0.6, 0.7):
        frac = float(np.mean(per_stroke_min < thr))
        print(f"  min_corr={thr:.1f}  -> {frac*100:>5.1f}% flagged  {'#' * int(30*frac)}")

    # do LOW-rho strokes cluster at extreme offsets (i.e. is rho a good gate)?
    off = []
    rho = []
    for st in res.strokes:
        for s in seats:
            if s in st.corr and s in st.offsets_ms:
                rho.append(st.corr[s]); off.append(abs(st.offsets_ms[s]))
    rho = np.array(rho); off = np.array(off)
    hi = off[rho >= 0.5]; lo = off[rho < 0.5]
    print(f"\n|offset| ms:  rho>=0.5  med={np.median(hi) if hi.size else 0:.0f} "
          f"(n={hi.size})   rho<0.5  med={np.median(lo) if lo.size else 0:.0f} (n={lo.size})")


if __name__ == "__main__":
    main()
