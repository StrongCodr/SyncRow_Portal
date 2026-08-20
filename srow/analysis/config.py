"""Tunable algorithm parameters (previously magic numbers scattered in code).

One place to see and change every knob. Detection/geometry tuning lives here;
the shared "what is a valid rowing stroke" spec stays in `validity.py` because
the phone must cite the same definition.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisConfig:
    # --- synthetic frame / adaptive axis (frame.py) ---
    # The axis is recomputed over a TRAILING window at refresh anchors, so a mid-
    # session sensor re-orientation re-locks the axis instead of one whole-interval
    # PCA blending two mountings. Mirrors the phone (WINDOW_MS / AXIS_REFRESH_MS).
    axis_window_s: float = 10.0          # trailing window the axis PCA runs over
    axis_refresh_s: float = 0.5          # recompute the axis this often

    # --- catch detection (detect.py) ---
    smooth_frac: float = 1.0 / 6.0       # smoothing window as fraction of stroke period
    detrend_periods: float = 2.0         # running-median window = this * period
    hysteresis_sigma: float = 0.4        # Schmitt threshold, in robust-sigma of signal
    refractory_frac: float = 0.55        # min inter-catch gap as fraction of period

    # --- cross-sensor (crosssensor.py) ---
    xcorr_grid_hz: float = 100.0         # shared resample grid for windowed x-corr
    xcorr_window_frac: float = 0.5       # half-window = this * local stroke period
    xcorr_max_lag_frac: float = 0.30     # max searched lag as fraction of period
    align_grid_hz: float = 50.0          # grid for sign alignment / coarse lag

    # --- quality flags (per seat, per stroke — a bad seat never voids the boat) ---
    drill_amp_frac: float = 0.6          # stroke flagged 'drill' if amp < this * median
    min_corr: float = 0.5                # seat 'low_confidence' if x-corr peak rho below this
    max_held_run_frac: float = 0.30      # seat 'degraded_signal' if longest zero-order-hold
                                         # run in the window exceeds this fraction of a stroke


DEFAULT = AnalysisConfig()
