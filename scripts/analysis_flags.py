#!/usr/bin/env python3
"""Per-seat, per-stroke quality distribution.

Under the per-seat model a stroke is not simply "valid" or "flagged" — each seat
has its own status (ok / degraded_signal / low_confidence / reference_bad). This
shows, per seat, where the good and bad strokes fall, plus how many usable seats
we have per stroke (the thing that actually limits crew-spread coverage).

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

CH = {"ok": ".", "degraded_signal": "d", "low_confidence": "c", "reference_bad": "x"}


def main():
    settings = load_settings()
    influx = InfluxService(settings)
    intervals = influx.fetch_interval_tags()
    if not intervals:
        print("no intervals"); return
    target = sys.argv[1] if len(sys.argv) > 1 else intervals[0]["value"]
    iv = next((i for i in intervals if i["value"] == target), intervals[0])

    m = re.search(r"(\d{13})", iv["value"])
    anchor = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=datetime.timezone.utc)
    by_source = load_raw_by_source(
        settings, iv["tag"], iv["value"],
        anchor - datetime.timedelta(days=2), anchor + datetime.timedelta(days=2),
    )
    res = analyze_interval(by_source).cross
    strokes = res.strokes
    if not strokes:
        print("no strokes"); return
    n = len(strokes)
    seats = [s for s in res.rowers if s != res.reference]

    print(f"interval {iv['value']}   ref={res.reference}   strokes={n}")
    piece = [st for st in strokes if st.is_piece_stroke()]
    print(f"stroke-wide flags: {dict(Counter(f for st in strokes for f in st.flags))}  "
          f"({len(piece)}/{n} piece strokes)\n")

    # per-seat status timeline + counts (one bad seat never voids the others)
    print("per-seat status timeline (. ok  d degraded_signal  c low_confidence  x ref_bad):")
    for s in seats:
        line = "".join(CH.get(st.seat_status.get(s, "?"), "?") for st in strokes)
        cnt = Counter(st.seat_status.get(s) for st in strokes)
        ok = cnt.get("ok", 0)
        print(f"\n  {s}   ok={ok} ({100*ok/n:.0f}%)  {dict(cnt)}")
        for i in range(0, n, 100):
            print(f"    {i:>4}: {line[i:i+100]}")

    # usable seats per stroke — how often do we actually get a crew spread?
    print("\nusable (OK) seats per stroke:")
    counts = Counter(st.n_matched for st in piece)
    for k in sorted(counts, reverse=True):
        frac = counts[k] / len(piece) if piece else 0
        print(f"  {k} seats: {counts[k]:>4}  {100*frac:>4.0f}%  {'#' * int(30*frac)}")

    # is degradation clumped or scattered, per seat?
    print("\nper-seat OK-run structure (clumped good runs vs scattered):")
    for s in seats:
        ok = [st.seat_status.get(s) == "ok" for st in strokes]
        runs, cur, v = [], 1, ok[0]
        for b in ok[1:]:
            if b == v: cur += 1
            else: runs.append((v, cur)); v, cur = b, 1
        runs.append((v, cur))
        okrun = [l for val, l in runs if val]
        print(f"  {s:<18} transitions={len(runs)-1:>3}  longest OK run={max(okrun) if okrun else 0:>3}")


if __name__ == "__main__":
    main()
