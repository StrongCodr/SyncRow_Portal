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
    axis_window_s: float = 60.0          # trailing window the axis PCA runs over (10 s broke on BLE-holey data)
    axis_refresh_s: float = 0.5          # recompute the axis this often
    # Feather-flip rejection (sculling units): the fast +-90 deg roll about the shaft puts
    # 500-800 deg/s spikes on one gyro axis and dominates a plain PCA (even below 2 Hz: a flip
    # is a 0.25 s pulse). The axis carrying the fast content (PC1 above `flip_hp_hz`, if its
    # p99 rate is at least `flip_min_deg_s`) is removed before the sweep PCA, which then
    # runs on the < `axis_lp_hz` content. The
    # projected sweep signal is low-passed at `signal_lp_hz` before detection / x-corr.
    # 0 disables each step.
    flip_hp_hz: float = 2.0
    flip_min_deg_s: float = 60.0         # accept the flip axis if p99 of its > flip_hp_hz rate is at least this (deg/s)
    axis_lp_hz: float = 2.5
    signal_lp_hz: float = 3.0
    filter_grid_hz: float = 50.0         # uniform grid the filters run on (native data is irregular)

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
    max_fresh_gap_s: float = 0.30        # seat 'degraded_signal' if the stroke window has a gap
                                         # without fresh samples (held rows or a BLE hole) longer than this


DEFAULT = AnalysisConfig()
