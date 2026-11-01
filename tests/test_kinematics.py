"""Kinematics on a synthetic oar whose sweep, elevation and feather are known exactly."""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation as R

from srow.analysis import kinematics as K
from srow.analysis.config import DEFAULT
from srow.analysis.frame import build_sensor_frame
from srow.boats import resolve


def synthetic_oar(seconds=60.0, fs=100.0, spm=24.0, arc=70.0, elev_in=-8.0, elev_out=-3.0, perp_heading=90.0, noise=0.5, seed=0):
    """An oar body: x = shaft (blade-ward), rotating about the world vertical by A(t) around the
    perpendicular heading, tilted by E(t) (negative = blade down) and feathered by F(t) (0 = square in
    the drive, 90 in the recovery). Returns sensor-frame gyro (deg/s), accel (g), WitMotion-style
    roll/pitch/yaw (deg), the true angle A(t), and the true catch times."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 1 / fs); T = 60.0 / spm
    phase = 2 * np.pi * t / T
    A = -(arc / 2) * np.cos(phase)                                   # catch at the minimum, t = 0, T, 2T...
    drive = np.sin(phase) > 0
    u = (t % T) / T                                                 # 0 at the catch, 0.5 at the finish, 1 at the next catch
    ramp = 0.06                                                     # feather in and out over 6 % of the cycle (0.15 s)
    F = 90.0 * np.clip((u - 0.5) / ramp, 0, 1) * np.clip((1.0 - u) / ramp, 0, 1)   # 0 in the drive, 90 in the recovery
    in_water = np.clip(u / ramp, 0, 1) * np.clip((0.5 - u) / ramp, 0, 1)           # blade in from the catch to the finish, with ramps
    E = elev_out + (elev_in - elev_out) * in_water
    # rotations: world <- body = Rz(heading) · Ry(-E) · Rx(F)  (y rotation by -E raises the +x blade end for E > 0)
    rot = R.from_euler("ZYX", np.column_stack([np.radians(perp_heading + A), np.radians(-E), np.radians(F)]))
    # body angular velocity from consecutive rotations
    dR = (rot[:-1].inv() * rot[1:])
    w_body = dR.as_rotvec() * fs                                       # rad/s in the body frame
    w_body = np.vstack([w_body, w_body[-1]])
    gyro = np.degrees(w_body) + rng.normal(0, noise, w_body.shape)
    g_world = np.array([0, 0, -1.0])                                   # gravity points down; accel reads +1 g up
    accel = rot.inv().apply(-g_world) + rng.normal(0, 0.01, (t.size, 3))
    eul = rot.as_euler("ZYX", degrees=True)                            # yaw, pitch, roll
    euler = np.column_stack([eul[:, 2], eul[:, 1], eul[:, 0]])          # roll, pitch, yaw as the units report
    catches = t[::int(round(T * fs))]
    return t + 1.7e9, accel, gyro, euler, A, catches + 1.7e9


def test_synthetic_oar_recovers_arc_height_and_feather():
    t, accel, gyro, euler, A, catches = synthetic_oar()
    fr = build_sensor_frame(t, accel, gyro, DEFAULT)
    assert fr is not None and fr.flip is not None
    if np.corrcoef(fr.signal, np.interp(t, t, np.gradient(A)))[0, 1] < 0:
        fr.signal = -fr.signal                                         # the engine sign-aligns to the stroke seat; do it here
    rig = resolve("2x")
    k = K.oar_kinematics("Seat 1", t, accel, gyro, euler, fr, catches)
    assert k is not None and len(k.strokes) >= 18
    arcs = np.array([s.arc_deg for s in k.strokes])
    assert abs(np.median(arcs) - 70.0) < 3.0
    feathers = np.array([s.feather_range_deg for s in k.strokes])
    assert abs(np.median(feathers) - 90.0) < 8.0
    # midpoint reference: the catch sits at about -35° from the middle of the arc
    assert k.angle_reference == "midpoint"
    assert abs(np.median([s.catch_deg for s in k.strokes]) + 35.0) < 4.0
    # blade height: in the water during the drive, out during the recovery (2x rigging: pin 0.24 m, outboard 2.0 m)
    drive = k.sweep_rate > 0
    height = K.blade_height_cm(k.elev_deg, rig)
    h_in, h_out = np.nanmedian(height[drive]), np.nanmedian(height[~drive])
    assert h_in < 0 < h_out, (h_in, h_out)
    assert abs(h_in - 100 * (0.24 + 2.0 * np.sin(np.radians(-8)))) < 4.0
    # feather timing is reported for most strokes
    assert sum(s.square_before_ms is not None for s in k.strokes) > len(k.strokes) * 0.7
    assert k.quality["rate_vs_engine_corr"] > 0.9


def test_without_orientation_falls_back_to_the_engine_axis_and_midpoint():
    t, accel, gyro, euler, A, catches = synthetic_oar(seconds=40)
    fr = build_sensor_frame(t, accel, gyro, DEFAULT)
    k = K.oar_kinematics("Seat 1", t, accel, gyro, None, fr, catches)
    assert k is not None and k.angle_reference == "midpoint" and not k.quality["orientation"]
    assert np.isnan(k.elev_deg).all()
    assert abs(np.median([(s.catch_deg + s.finish_deg) / 2 for s in k.strokes])) < 3.0


def test_hull_attitude_removes_the_mounting_offset():
    t = np.arange(0, 30, 0.04) + 1.7e9
    gyro = np.random.default_rng(1).normal(0, 1, (t.size, 3))
    euler = np.column_stack([5 + 2 * np.sin(2 * np.pi * t / 2.5), -3 + np.sin(2 * np.pi * t / 2.5), 170 + 20 * np.sin(2 * np.pi * t / 60)])
    h = K.hull_attitude("Cox", t, gyro, euler)
    assert h is not None and abs(np.median(h.roll_deg)) < 0.3 and abs(np.median(h.pitch_deg)) < 0.3
    assert np.ptp(h.roll_deg) == pytest.approx(4.0, abs=0.3)


def test_gps_distance_ignores_rewrites_bad_fixes_and_long_gaps():
    t = np.arange(0, 100, 1.0)
    lat = 25.0 + np.cumsum(np.full(100, 3.0 / 111320.0))               # 3 m per second northwards
    lon = np.full(100, 55.0)
    lat[10:15] = lat[9]                                                # the app re-writing the last fix
    acc = np.full(100, 5.0); acc[50:55] = 80.0                         # five bad fixes
    t[70:] += 10                                                       # a 10 s gap
    d = K.gps_distance(t, lat, lon, acc)
    assert 240 < d["distance_m"] < 290
    assert d["split_s_per_500"] == pytest.approx(500 / 3.0, rel=0.03)
    assert K.format_split(d["split_s_per_500"]).startswith("2:4")
    assert K.format_split(None) == "–"
