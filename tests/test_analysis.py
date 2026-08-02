"""Tests for the analysis engine — including synthetic GROUND TRUTH.

The pipeline test injects known per-seat time offsets through arbitrary (random)
sensor mountings and asserts the engine recovers them with the right sign and
magnitude. This is what makes the offsets defensible: on data where we know the
answer, the math returns it. (Real-hardware artifacts — differing waveform shapes
per mount, BLE jitter — still require a bench test; see RESEARCH §4, §9.)
"""

import numpy as np

from srow.analysis.crosssensor import gaussian_lag, is_rowing
from srow.analysis.engine import analyze_interval
from srow.analysis.frame import build_sensor_frame

FS = 100.0
DUR = 60.0
F = 0.4  # 24 spm


def _unit(v):
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


def _make_crew(offsets_s, axes, grav, amp=100.0, noise=0.5, seed=1):
    """Synthetic crew: each sensor sees the stroke rotation about its own
    (arbitrary) axis, time-shifted by a known offset, plus its own gravity."""
    rng = np.random.default_rng(seed)
    n = int(FS * DUR)
    t = np.arange(n) / FS
    times_s = t + 1_700_000_000.0
    by_source = {}
    for name, off in offsets_s.items():
        s = amp * np.sin(2 * np.pi * F * (t - off))
        gyro = np.outer(s, _unit(axes[name])) + rng.normal(0, noise, (n, 3))
        accel = np.tile(_unit(grav[name]), (n, 1)) + rng.normal(0, 0.01, (n, 3))
        by_source[name] = {"times_s": times_s, "accel": accel, "gyro": gyro}
    return by_source


# ─── gaussian_lag: sign + magnitude ──────────────────────────────────────────

def test_gaussian_lag_recovers_known_delay():
    t = np.arange(500) / FS
    a = np.sin(2 * np.pi * 1.0 * t)
    # b is `a` delayed (later) by 50 ms -> convention says POSITIVE lag.
    b_late = np.sin(2 * np.pi * 1.0 * (t - 0.05))
    lag = gaussian_lag(a, b_late, FS, max_lag_s=0.2)
    assert lag is not None and abs(lag - 0.05) < 0.005, lag

    # b earlier by 30 ms -> NEGATIVE lag.
    b_early = np.sin(2 * np.pi * 1.0 * (t + 0.03))
    lag2 = gaussian_lag(a, b_early, FS, max_lag_s=0.2)
    assert lag2 is not None and abs(lag2 + 0.03) < 0.005, lag2


# ─── frame: recover a known rotation axis regardless of mounting ─────────────

def test_frame_recovers_known_axis():
    n = int(FS * DUR)
    t = np.arange(n) / FS
    axis = _unit([1.0, 0.0, 0.0])          # perpendicular to gravity for a clean check
    s = 100.0 * np.sin(2 * np.pi * F * t)
    rng = np.random.default_rng(0)
    gyro = np.outer(s, axis) + rng.normal(0, 0.5, (n, 3))
    accel = np.tile([0.0, 0.0, 1.0], (n, 1)) + rng.normal(0, 0.01, (n, 3))
    fr = build_sensor_frame(t + 1e9, accel, gyro)
    assert fr is not None
    assert fr.variance_explained > 0.9
    assert abs(abs(float(np.dot(fr.sweep, axis))) - 1.0) < 0.05   # sweep ∥ axis (±)
    assert abs(fr.dominant_hz - F) < 0.03
    assert is_rowing(fr)


# ─── non-rowing sensor is excluded ──────────────────────────────────────────

def test_flat_sensor_excluded():
    n = int(FS * DUR)
    t = np.arange(n) / FS
    rng = np.random.default_rng(2)
    gyro = rng.normal(0, 0.5, (n, 3))       # noise only, no stroke
    accel = np.tile([0.0, 0.0, 1.0], (n, 1)) + rng.normal(0, 0.01, (n, 3))
    fr = build_sensor_frame(t + 1e9, accel, gyro)
    assert fr is not None
    assert not is_rowing(fr)                 # low sweep energy / no in-band rhythm


# ─── GROUND TRUTH: pipeline recovers injected cross-sensor offsets ──────────

def test_pipeline_recovers_injected_offsets():
    injected = {
        "Seat 4 (Seat 4)": 0.000,   # stroke seat (highest index) = reference
        "Seat 3 (Seat 3)": 0.000,
        "Seat 2 (Seat 2)": 0.050,   # 50 ms LATE
        "Seat 1 (Seat 1)": -0.030,  # 30 ms EARLY
    }
    axes = {  # arbitrary mountings; some point "backwards" to force sign flips
        "Seat 4 (Seat 4)": [0.2, 0.9, -0.3],
        "Seat 3 (Seat 3)": [-0.7, 0.1, 0.6],
        "Seat 2 (Seat 2)": [0.4, -0.5, 0.8],
        "Seat 1 (Seat 1)": [-0.3, -0.8, -0.4],
    }
    grav = {
        "Seat 4 (Seat 4)": [0.0, 0.1, 1.0],
        "Seat 3 (Seat 3)": [0.1, 0.0, 1.0],
        "Seat 2 (Seat 2)": [-0.1, 0.05, 1.0],
        "Seat 1 (Seat 1)": [0.05, -0.1, 1.0],
    }
    by_source = _make_crew(injected, axes, grav)
    res = analyze_interval(by_source).cross

    assert res.reference == "Seat 4 (Seat 4)"
    assert set(res.rowers) == set(injected)
    assert len(res.strokes) > 15

    for name, off_s in injected.items():
        if name == res.reference:
            continue
        vals = np.array([st.offsets_ms[name] for st in res.strokes if name in st.offsets_ms])
        assert vals.size > 15, name
        med = float(np.median(vals))
        assert abs(med - off_s * 1000.0) < 10.0, f"{name}: got {med:.1f}ms, want {off_s*1000:.0f}ms"
        # sign must be right, not just magnitude
        if abs(off_s) > 0.02:
            assert np.sign(med) == np.sign(off_s * 1000.0), name
