#!/usr/bin/env python3
"""Draw the raw synthetic signal + detected catches for a short window, so a
human can eyeball whether catch detection is clean (one mark per real stroke) or
double-firing (the over-detection hypothesis).

    python scripts/analysis_plot.py [intervalId] [center_frac]

center_frac (0..1) picks where in the piece to look (default 0.5 = middle).
Writes /tmp/analysis_plot.png.
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


FLAG_COLOR = {"low_confidence": "orange", "drill": "purple", "out_of_range": "red"}


def main():
    settings = load_settings()
    influx = InfluxService(settings)
    intervals = influx.fetch_interval_tags()
    target = sys.argv[1] if len(sys.argv) > 1 else intervals[0]["value"]
    center = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    iv = next((i for i in intervals if i["value"] == target), intervals[0])

    m = re.search(r"(\d{13})", iv["value"])
    anchor = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=datetime.timezone.utc)
    by_source = load_raw_by_source(
        settings, iv["tag"], iv["value"],
        anchor - datetime.timedelta(days=2), anchor + datetime.timedelta(days=2),
    )
    ia = analyze_interval(by_source)
    frames, catches, res = ia.frames, ia.catches, ia.cross
    ref = res.reference
    strokes = res.strokes
    if not strokes:
        print("no strokes"); return

    # window: ~16 strokes centered at center_frac of the piece
    ci = int(center * len(strokes))
    lo, hi = max(0, ci - 8), min(len(strokes), ci + 8)
    t0 = strokes[lo].stroke_time_s - 1.5
    t1 = strokes[hi - 1].stroke_time_s + 1.5

    # rowing seats, stroke on top then descending
    order = sorted(res.rowers, key=lambda s: -(int(re.search(r'(\d+)', s).group(1))))
    fig, ax = plt.subplots(figsize=(16, 8))
    step = 6.0  # vertical spacing between seat traces (signal is z-scored ~[-3,3])

    for row, s in enumerate(order):
        fr = frames[s]
        base = -row * step
        mask = (fr.times_s >= t0) & (fr.times_s <= t1)
        x = fr.times_s[mask] - t0
        y = fr.signal[mask] + base
        is_ref = (s == ref)
        ax.plot(x, y, lw=1.1, color="black" if is_ref else "steelblue",
                alpha=0.9 if is_ref else 0.7)
        ax.axhline(base, color="gray", lw=0.4, alpha=0.3)
        # catch marks for this seat
        for c in catches.get(s, []):
            if t0 <= c.time_s <= t1:
                ax.plot(c.time_s - t0, base + step * 0.42, marker="v",
                        color="crimson", ms=7, mec="k", mew=0.4)
        label = f"{s}{'  <-- STROKE (ref)' if is_ref else ''}"
        ax.text(0, base + step * 0.55, label, fontsize=10,
                fontweight="bold" if is_ref else "normal")

    # shade flagged strokes (band = half a period around the stroke catch)
    seen = set()
    for st in strokes:
        if not (t0 <= st.stroke_time_s <= t1) or not st.flags:
            continue
        per = 60.0 / st.spm if st.spm > 0 else 2.5
        c = FLAG_COLOR.get(st.flags[0], "gray")
        ax.axvspan(st.stroke_time_s - t0 - 0.5 * per, st.stroke_time_s - t0,
                   color=c, alpha=0.12)
        seen.add(st.flags[0])

    handles = [plt.Line2D([], [], marker="v", color="crimson", ls="", label="detected catch"),
               plt.Line2D([], [], color="black", label="stroke seat (reference)"),
               plt.Line2D([], [], color="steelblue", label="other seats")]
    for f in seen:
        handles.append(plt.Rectangle((0, 0), 1, 1, color=FLAG_COLOR.get(f, "gray"),
                                     alpha=0.3, label=f"flagged: {f}"))
    ax.legend(handles=handles, loc="upper right", fontsize=9, framealpha=0.9)
    ax.set_xlabel(f"seconds (window {t1-t0:.0f}s, center={center:.2f} of piece)")
    ax.set_yticks([])
    ax.set_title(f"{iv['value']} — raw synthetic signal + detected catches "
                 f"(one v per detected stroke)")
    fig.tight_layout()
    fig.savefig("/tmp/analysis_plot.png", dpi=110)
    print(f"wrote /tmp/analysis_plot.png  window={t1-t0:.0f}s  strokes {lo}..{hi}")
    # quick text summary of inter-catch gaps on the reference in this window
    rc = np.array([c.time_s for c in catches.get(ref, []) if t0 <= c.time_s <= t1])
    if rc.size > 2:
        gaps = np.diff(rc)
        print(f"ref inter-catch gaps (s): min={gaps.min():.2f} med={np.median(gaps):.2f} "
              f"max={gaps.max():.2f}  -> implied spm {60/np.median(gaps):.1f}, "
              f"{(gaps < 0.6*np.median(gaps)).sum()} suspiciously-short gaps")


if __name__ == "__main__":
    main()
