#!/usr/bin/env python3
"""Diagnose the temporal distribution of flagged vs valid strokes.

Answers: are discarded strokes clumped (warm-up) or scattered (threshold
misfiring)? Prints a per-stroke timeline, run-length stats, positional deciles,
and SPM start-vs-piece.

    python scripts/analysis_flags.py [intervalId]
"""

import datetime
import re
import sys
from collections import Counter

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
    strokes = analyze_interval(by_source).cross.strokes
    if not strokes:
        print("no strokes")
        return

    n = len(strokes)
    valid = [not st.flags for st in strokes]

    def ch(st):
        if not st.flags:
            return "."
        if "out_of_range" in st.flags:
            return "r"
        if "drill" in st.flags:
            return "d"
        if "low_confidence" in st.flags:
            return "c"
        return "?"

    timeline = "".join(ch(st) for st in strokes)
    print(f"interval {iv['value']}   strokes={n}  valid={sum(valid)} ({100*sum(valid)/n:.0f}%)")
    print("flag counts:", dict(Counter(f for st in strokes for f in st.flags)))

    print("\ntimeline (. valid  d drill  r out-of-range  c low-conf):")
    for i in range(0, n, 100):
        print(f"  {i:>4}: {timeline[i:i+100]}")

    # run-lengths: clumped => few long runs; scattered => many short runs
    runs = []
    cur, length = valid[0], 1
    for v in valid[1:]:
        if v == cur:
            length += 1
        else:
            runs.append((cur, length))
            cur, length = v, 1
    runs.append((cur, length))
    vlen = [l for v, l in runs if v]
    flen = [l for v, l in runs if not v]
    print(f"\nruns: {len(runs)}  (transitions={len(runs)-1})")
    print(f"  valid runs: n={len(vlen):>3}  longest={max(vlen) if vlen else 0:>3}  median={int(np.median(vlen)) if vlen else 0}")
    print(f"  flag  runs: n={len(flen):>3}  longest={max(flen) if flen else 0:>3}  median={int(np.median(flen)) if flen else 0}")

    print("\nflagged fraction by position (deciles):")
    fidx = np.array([not v for v in valid], dtype=float)
    for d in range(10):
        seg = fidx[d * n // 10:(d + 1) * n // 10]
        frac = seg.mean() if seg.size else 0.0
        print(f"  {d*10:>3}-{(d+1)*10:>3}%  {frac*100:>5.0f}%  {'#' * int(30 * frac)}")

    spm = np.array([st.spm for st in strokes])
    print(f"\nspm: first-10% med={np.median(spm[:max(n//10,1)]):.1f}  "
          f"overall med={np.median(spm):.1f}  "
          f"last-10% med={np.median(spm[-max(n//10,1):]):.1f}")


if __name__ == "__main__":
    main()
