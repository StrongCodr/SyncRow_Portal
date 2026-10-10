#!/usr/bin/env python3
"""Engine sanity summary for one or more intervals: per-seat offset median / IQR / sigma, OK-stroke
counts, and 2-minute medians of the first non-reference seat. Usage: engine_check.py <intervalId> ..."""
import datetime as dt, re, sys
import numpy as np
from srow.config import load_settings
from srow.analysis.load import load_raw_by_source
from srow.analysis.engine import analyze_interval

def main():
    settings = load_settings()
    for iv in sys.argv[1:]:
        m = re.search(r"(\d{13})", iv); anchor = dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt.timezone.utc)
        by_source = load_raw_by_source(settings, "intervalId", iv, anchor - dt.timedelta(days=2), anchor + dt.timedelta(days=2))
        res = analyze_interval(by_source); cross = res.cross
        print(f"=== {iv}: reference {cross.reference}, rowers {cross.rowers}, excluded {cross.excluded}, strokes {len(cross.strokes)}")
        for fr_name, fr in getattr(res, "frames", {}).items() if hasattr(res, "frames") else []:
            print(f"  frame {fr_name}: var_explained {fr.variance_explained:.2f} dominant {fr.dominant_hz:.2f} Hz sweep_energy {fr.sweep_energy:.0f} feather_fallback {fr.used_feather_fallback} axis_ok {fr.stroke_axis_ok}")
        t0 = min(s.stroke_time_s for s in cross.strokes) if cross.strokes else 0
        for seat in cross.rowers:
            if seat == cross.reference: continue
            ok = [(s.stroke_time_s - t0, s.offsets_ms[seat], s.corr.get(seat, np.nan)) for s in cross.strokes if str(s.seat_status.get(seat)).lower() == "ok" and seat in s.offsets_ms]
            allv = [s.offsets_ms[seat] for s in cross.strokes if seat in s.offsets_ms]
            if not ok: print(f"  {seat}: no OK strokes (of {len(allv)})"); continue
            a = np.array(ok); v = a[:, 1]
            print(f"  {seat} vs {cross.reference}: OK {len(v)}/{len(allv)}  median {np.median(v):+.0f} ms  IQR {np.subtract(*np.percentile(v, [75, 25])):.0f}  sigma {v.std():.0f}  rho median {np.nanmedian(a[:, 2]):.2f}")
            bins = (a[:, 0] // 120).astype(int)
            print("    per 2 min (median/n): " + "  ".join(f"{b*2}m {np.median(v[bins == b]):+.0f}/{(bins == b).sum()}" for b in np.unique(bins)))

if __name__ == "__main__":
    main()
