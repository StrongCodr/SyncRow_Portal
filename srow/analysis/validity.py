"""Shared stroke-validity spec (RESEARCH.md §8.4).

The ONE definition of "this is real rowing" that both the portal (this code) and
the phone (Kotlin) must agree on. Keep the criteria and thresholds here so there
is a single citable source; if the phone displays a stroke the portal discards,
trust breaks. Algorithm *tuning* knobs live in `config.py`; validity *criteria*
live here.
"""

from __future__ import annotations

# Rowing stroke band: 12–45 spm = 0.2–0.75 Hz. Anything periodic here is a stroke;
# the feather/square roll and warm-up chaos live outside it.
STROKE_BAND_HZ: tuple[float, float] = (0.2, 0.75)

# Minimum sweep amplitude (std of the projected sweep signal, deg/s, BEFORE
# z-scoring) for a sensor to count as "rowing" vs. paddling/positioning/cox.
MIN_SWEEP_ENERGY_DEG_S: float = 10.0

# Physical sanity bounds on a single stroke's rate (flag, don't drop).
SPM_MIN: float = 10.0
SPM_MAX: float = 50.0


def in_stroke_band(hz: float) -> bool:
    lo, hi = STROKE_BAND_HZ
    return lo <= hz <= hi


def spm_flag(spm: float) -> str | None:
    """'out_of_range' if the per-stroke SPM is physically implausible."""
    if spm < SPM_MIN or spm > SPM_MAX:
        return "out_of_range"
    return None
