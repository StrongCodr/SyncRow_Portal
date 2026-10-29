"""Tests for the analysis engine — including synthetic GROUND TRUTH.

The pipeline test injects known per-seat time offsets through arbitrary (random)
sensor mountings and asserts the engine recovers them with the right sign and
magnitude. This is what makes the offsets defensible: on data where we know the
answer, the math returns it. (Real-hardware artifacts — differing waveform shapes
per mount, BLE jitter — still require a bench test; see RESEARCH §4, §9.)
"""

import dataclasses
import numpy as np
from srow.analysis.config import DEFAULT

from srow.analysis.crosssensor import xcorr_lag, is_rowing
from srow.analysis.detect import detect_catches
from srow.analysis.engine import analyze_interval
from srow.analysis.frame import _axis, build_sensor_frame

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


# ─── xcorr_lag: sign + magnitude ──────────────────────────────────────────

def test_gaussian_lag_recovers_known_delay():
    t = np.arange(500) / FS
    a = np.sin(2 * np.pi * 1.0 * t)
    # b is `a` delayed (later) by 50 ms -> convention says POSITIVE lag.
    b_late = np.sin(2 * np.pi * 1.0 * (t - 0.05))
    est = xcorr_lag(a, b_late, FS, max_lag_s=0.2)
    assert est is not None and abs(est.lag_s - 0.05) < 0.005, est
    assert est.rho > 0.95   # near-identical waveforms => high match confidence

    # b earlier by 30 ms -> NEGATIVE lag.
    b_early = np.sin(2 * np.pi * 1.0 * (t + 0.03))
    est2 = xcorr_lag(a, b_early, FS, max_lag_s=0.2)
    assert est2 is not None and abs(est2.lag_s + 0.03) < 0.005, est2

    # uncorrelated noise => low rho (would flag low_confidence).
    rng = np.random.default_rng(7)
    est3 = xcorr_lag(a, rng.normal(0, 1, t.size), FS, max_lag_s=0.2)
    assert est3 is not None and abs(est3.rho) < 0.5, est3


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


# ─── one seat spaces out; the rest of the boat survives ─────────────────────

def test_degraded_seat_isolated_not_boat_wide():
    """A sensor freezing (zero-order-hold) for a stretch must degrade only THAT
    seat's strokes in that stretch — never the other seats, never the whole boat."""
    names = [f"Seat {i} (Seat {i})" for i in (1, 2, 3, 4)]
    injected = {n: 0.0 for n in names}
    axes = {names[0]: [0.3, 0.8, 0.5], names[1]: [-0.6, 0.2, 0.7],
            names[2]: [0.5, -0.4, 0.7], names[3]: [0.2, 0.9, -0.3]}
    grav = {n: [0.0, 0.0, 1.0] for n in names}
    by_source = _make_crew(injected, axes, grav, seed=3)

    # freeze Seat 2's gyro for an ~8 s stretch in the middle: the sensor spaces out.
    victim = "Seat 2 (Seat 2)"
    g = by_source[victim]["gyro"]
    n = g.shape[0]
    a, b = n // 2 - 400, n // 2 + 400
    g[a + 1:b] = g[a]                     # zero-order hold -> held mask True

    res = analyze_interval(by_source).cross
    assert res.reference == "Seat 4 (Seat 4)"
    assert victim in res.rowers          # still a rower (good majority)

    vstatus = [st.seat_status.get(victim) for st in res.strokes]
    assert "degraded_signal" in vstatus  # the frozen stretch is caught
    assert "ok" in vstatus               # its good strokes are still kept

    # a co-rower stays overwhelmingly OK straight through Seat 2's bad stretch
    other = "Seat 3 (Seat 3)"
    ok_other = sum(st.seat_status.get(other) == "ok" for st in res.strokes)
    assert ok_other > 0.7 * len(res.strokes), ok_other

    # and Seat 2's degraded strokes are clumped in the frozen middle, not scattered
    deg_times = [st.stroke_time_s for st in res.strokes
                 if st.seat_status.get(victim) == "degraded_signal"]
    frozen_t0 = by_source[victim]["times_s"][a]
    frozen_t1 = by_source[victim]["times_s"][b]
    inside = sum(frozen_t0 - 2 <= t <= frozen_t1 + 2 for t in deg_times)
    assert inside >= 0.8 * len(deg_times), (inside, len(deg_times))


# ─── adaptive axis re-locks after a mid-piece re-orientation (RESEARCH §3) ──────

def test_adaptive_axis_relocks_after_reorientation():
    """A sensor bumped mid-piece: its stroke rotation axis moves ~80° at the midpoint.
    The adaptive per-window axis must re-lock (each half stays a clean 1-D oscillation),
    where a single whole-interval PCA would blend the two mounts and lose variance. The
    phone asserts the same behaviour via getLateness (test in StrokeAnalyzerTest.kt)."""
    fs, freq, dur = 100.0, 0.4, 80.0
    n = int(fs * dur)
    t = np.arange(n) / fs
    times = t + 1_700_000_000.0
    rng = np.random.default_rng(5)
    s = 100.0 * np.sin(2 * np.pi * freq * t)
    mid = n // 2
    ax1, ax2 = _unit([0.9, 0.1, 0.2]), _unit([0.1, 0.9, 0.2])      # ~80° apart
    grav = _unit([0.0, 0.0, 1.0])
    gyro = np.empty((n, 3))
    accel = np.tile(grav, (n, 1)) + rng.normal(0, 0.01, (n, 3))
    gyro[:mid] = np.outer(s[:mid], ax1) + rng.normal(0, 0.5, (mid, 3))
    gyro[mid:] = np.outer(s[mid:], ax2) + rng.normal(0, 0.5, (n - mid, 3))

    cfg = dataclasses.replace(DEFAULT, axis_window_s=10.0)   # mechanism test: short window on an 80 s piece
    fr = build_sensor_frame(times, accel, gyro, cfg)
    assert fr is not None and is_rowing(fr)
    # adaptive frame keeps a clean 1-D axis per window; the whole-interval axis blends.
    whole = _axis(accel, gyro, times, cfg=cfg)
    assert whole is not None
    assert fr.variance_explained > whole.variance_explained + 0.2, \
        (fr.variance_explained, whole.variance_explained)
    # catches are found in BOTH halves (signal stayed periodic through the change).
    cs = detect_catches(fr.times_s, fr.signal, fr.fs, fr.dominant_hz)
    ct = np.array([c.time_s - times[0] for c in cs])
    first = int(((ct > 5) & (ct < dur / 2 - 3)).sum())
    second = int(((ct > dur / 2 + 12) & (ct < dur - 3)).sum())   # after change + 1 window
    assert first >= 5 and second >= 5, (first, second)


# ─── golden vector: the two tiers' offset estimators must stay byte-for-byte equal ─

def test_golden_vector_matches_phone():
    """xcorr_lag on a FIXED deterministic input reproduces these exact numbers. The
    phone test `crossLag matches portal golden vector` asserts the SAME constants, so any
    drift between the two estimators breaks one side. The magnitudes also PIN the
    documented conservative sub-sample bias (an injected +40 ms shift reads ~34.5 ms)."""
    fs, freq, n = 100.0, 0.5, 200

    def wave(shift):
        x = np.sin(2 * np.pi * freq * (np.arange(n) - shift) / fs)
        return np.where(x > 0, 1.8 * x, x)

    ref = wave(0)
    golden = {0: (0.0, 1.0), 4: (34.52485, 0.99948), -7: (-67.92291, 1.0)}
    for shift, (lag_ms, rho) in golden.items():
        est = xcorr_lag(ref, wave(shift), fs, max_lag_s=0.2)
        assert est is not None
        assert abs(est.lag_s * 1000.0 - lag_ms) < 0.02, (shift, est.lag_s * 1000.0, lag_ms)
        assert abs(est.rho - rho) < 0.001, (shift, est.rho, rho)


def test_hull_surge_finds_forward_axis_and_sign():
    """A surge of known shape along a random mounting axis comes back on that axis, drive positive."""
    from srow.analysis.hull import hull_surge, G0
    rng = np.random.default_rng(3)
    fs, dur, f = 50.0, 120.0, 0.4
    t = np.arange(0, dur, 1 / fs); phase = (t * f) % 1.0
    surge = np.where(phase < 0.4, 3.0 * np.sin(np.pi * phase / 0.4), -1.0)   # m/s^2: short push, long weak pull
    fwd = _unit(rng.normal(size=3)); down = _unit(np.cross(fwd, rng.normal(size=3)))
    acc = (np.outer(surge, fwd) + np.outer(np.ones_like(t), down) * G0) / G0 + rng.normal(scale=0.01, size=(t.size, 3))
    catches = list(np.arange(0, dur, 1 / f))
    g, s = hull_surge(t + 1.7e9, acc, [c + 1.7e9 for c in catches])
    assert g is not None
    ref = np.interp(g - 1.7e9, t, surge)
    assert np.corrcoef(s, ref)[0, 1] > 0.95
    assert s[(g - 1.7e9) % (1 / f) < 0.2].mean() > 0            # drive positive
    g2, s2 = hull_surge(t + 1.7e9, acc, None)                   # sign from skew alone
    assert np.corrcoef(s2, ref)[0, 1] > 0.95


def test_decimate_keeps_shape_within_budget():
    from web.charts import _decimate
    t = np.arange(10000.0); y = np.sin(t / 50.0)
    tt, yy = _decimate(t, y, 500)
    assert len(tt) <= 500 and abs(yy.max() - 1.0) < 0.05 and abs(yy.min() + 1.0) < 0.05
    t2, y2 = _decimate(t[:100], y[:100], 500)
    assert len(t2) == 100


def test_gps_windows_only_where_gps_is_steady():
    """Fixes split into steady spans at gaps over 3 s; poor-accuracy and short spans are dropped."""
    from srow.analysis.hull import gps_windows
    t = np.r_[np.arange(0, 40.0), np.arange(50, 100.0), np.arange(110, 118.0)]      # 40 s, gap, 50 s, gap, 8 s
    v = np.full(t.size, 3.0); acc = np.full(t.size, 5.0); acc[(t >= 60) & (t < 70)] = 500.0   # cell-tower stretch
    w = gps_windows(t, v, acc)
    assert w == [(0.0, 39.0), (70.0, 99.0)]        # 50..59 is only 10 s: too short; 110.. is 8 s
    assert gps_windows(t, v, np.full(t.size, 900.0)) == []


def test_fused_speed_recovers_stroke_swing_and_gates_on_gps():
    """GPS at 1 Hz (slow part only) plus the surge gives the full within-stroke speed; no GPS, no speed."""
    from srow.analysis.hull import fused_speed
    fs, dur, f = 25.0, 200.0, 0.4
    g = np.arange(0, dur, 1 / fs) + 1.7e9
    slow = 3.0 + 0.3 * np.sin(2 * np.pi * (g - g[0]) / 60.0)
    stroke = 0.6 * np.sin(2 * np.pi * f * (g - g[0]))
    true_v = slow + stroke
    surge = np.gradient(true_v, 1 / fs)
    gt = np.arange(0, 120.0) + 1.7e9                                 # GPS only for the first 120 s
    gv = np.interp(gt, g, slow) + np.random.default_rng(1).normal(scale=0.1, size=gt.size)
    _, v, wins, lag = fused_speed(g, surge, gt, gv, np.full(gt.size, 5.0))
    assert len(wins) == 1 and abs(lag) <= 0.2
    inside = (g >= 1.7e9 + 10) & (g <= 1.7e9 + 110)
    assert np.isfinite(v[inside]).all() and np.isnan(v[g > 1.7e9 + 125]).all()
    assert abs(np.nanmean(v[inside]) - true_v[inside].mean()) < 0.1
    err = v[inside] - true_v[inside]
    assert np.sqrt(np.mean(err ** 2)) < 0.12                           # the 0.6 m/s swing is in, GPS noise mostly out
