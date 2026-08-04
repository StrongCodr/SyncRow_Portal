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
from srow.analysis.load import load_raw_by_source
from srow.analysis.crosssensor import analyze_cross, pairwise_lags


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

    # Streaming, memory-bounded load over a +/-2d window around the name epoch
    # (name/data clocks differ ~9h; a tight window misses the data).
    m = re.search(r"(\d{13})", iv["value"])
    anchor = datetime.datetime.fromtimestamp(
        int(m.group(1)) / 1000, tz=datetime.timezone.utc
    ) if m else datetime.datetime.now(datetime.timezone.utc)
    start = anchor - datetime.timedelta(days=2)
    end = anchor + datetime.timedelta(days=2)

    by_source = load_raw_by_source(settings, iv["tag"], iv["value"], start, end)
    if not by_source:
        print("no raw data")
        return
    total = sum(d["times_s"].size for d in by_source.values())
    print(f"raw samples: {total}  sources: {sorted(by_source)}")
    print()
    print(f"{'sensor':<10} {'rows':>7} {'fs':>6} {'var%':>6} {'domHz':>6} "
          f"{'~spm':>6} {'catch':>6} {'unc_ms':>7} {'feath':>6}")
    print("-" * 72)

    # Full engine: build frames -> sign-align -> detect -> cross-sensor.
    from srow.analysis.engine import analyze_interval
    ia = analyze_interval(by_source)
    frames, catches, res = ia.frames, ia.catches, ia.cross

    for src in sorted(frames):
        frame, cs = frames[src], catches[src]
        rows = by_source[src]["times_s"].size
        spm = frame.dominant_hz * 60.0
        unc_ms = 1000.0 * np.median([c.uncertainty_s for c in cs]) if cs else 0.0
        print(f"{str(src):<10} {rows:>7} {frame.fs:>6.1f} "
              f"{100*frame.variance_explained:>5.1f} {frame.dominant_hz:>6.3f} "
              f"{spm:>6.1f} {len(cs):>6} {unc_ms:>7.1f} "
              f"{'yes' if frame.used_feather_fallback else 'no':>6}")
    if ia.flipped:
        print(f"  (sign-flipped to align with stroke: {ia.flipped})")
    print()
    print(f"reference (stroke): {res.reference}")
    print(f"rowers: {res.rowers}")
    print(f"excluded: {res.excluded}")
    print(f"matched strokes: {len(res.strokes)}")
    if res.strokes:
        seats = [s for s in res.rowers if s != res.reference]
        from collections import Counter
        piece = [st for st in res.strokes if st.is_piece_stroke()]
        print(f"\n  strokes: {len(res.strokes)} total, {len(piece)} piece strokes "
              f"(not drill/out-of-range/ref-void)")
        print("  per-seat offset vs stroke (ms), that seat's OK strokes:  median +/- unc  [p10..p90]  | status mix")
        for s in seats:
            # per-seat, per-stroke: a bad seat here never drops another seat
            ok = [st for st in piece if st.seat_status.get(s) == "ok"]
            vals = np.array([st.offsets_ms[s] for st in ok if s in st.offsets_ms])
            unc = np.array([st.uncertainty_ms[s] for st in ok if s in st.uncertainty_ms])
            mix = Counter(st.seat_status.get(s, "?") for st in piece)
            mixs = " ".join(f"{k}={v}" for k, v in mix.most_common())
            if vals.size:
                print(f"    {s:<18} n={vals.size:>4}  med={np.median(vals):>+7.1f} "
                      f"+/-{np.median(unc):>5.1f}  "
                      f"[{np.percentile(vals,10):>+7.1f} .. {np.percentile(vals,90):>+7.1f}]  | {mixs}")
            else:
                print(f"    {s:<18} n=   0  (no OK strokes)  | {mixs}")
        # crew spread uses only strokes with >=2 usable seats
        spreads = np.array([st.spread_ms for st in piece if st.n_matched >= 2])
        if spreads.size:
            print(f"\n  crew spread over OK seats (ms): median={np.median(spreads):.1f}  "
                  f"p90={np.percentile(spreads,90):.1f}  ({spreads.size} strokes with >=2 OK seats)")

        # Cross-correlation diagnostic: waveform lag (phase-independent) + sign.
        print("\n  x-corr vs stroke (whole-waveform):  corr-sign   lag(ms)")
        lags = pairwise_lags(frames, res.reference, seats)
        for s in seats:
            if s in lags:
                c0, lag = lags[s]
                print(f"    {s:<18} {'+' if c0 >= 0 else '- (ANTIPHASE!)':<16} "
                      f"{lag if lag is not None else float('nan'):>+8.1f}")


if __name__ == "__main__":
    main()
