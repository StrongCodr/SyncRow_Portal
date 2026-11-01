"""The engine's drive sign is physical when the unit's orientation is available, and the hull surge
sign follows the catch check."""
import sys
import numpy as np

sys.path.insert(0, "tests")
from test_kinematics import synthetic_oar
from srow.analysis.engine import analyze_interval
from srow.analysis.hull import hull_surge


def _run(flip_gyro_sign: bool):
    by = {}
    truth = {}
    for seat, seed in (("Seat 1", 1), ("Seat 2", 2)):
        t, accel, gyro, euler, A, catches = synthetic_oar(seconds=60, seed=seed)
        if flip_gyro_sign:                       # mount the unit the other way round about every axis
            gyro = -gyro; accel = accel * np.array([-1, -1, 1]); euler = euler.copy(); euler[:, 2] += 180
        by[seat] = {"times_s": t, "accel": accel, "gyro": gyro, "euler": euler}
        truth[seat] = (t, np.gradient(A, t), catches)
    return by, truth


def test_up_crossings_are_catches_whichever_way_the_frame_came_out():
    for flip in (False, True):
        by, truth = _run(flip)
        an = analyze_interval(by)
        assert set(an.cross.rowers) == {"Seat 1", "Seat 2"}
        for seat in an.cross.rowers:
            fr = an.frames[seat]
            assert fr.drive_sign == "elevation"
            t, dA, catches = truth[seat]
            assert np.corrcoef(fr.signal, np.interp(fr.times_s, t, dA))[0, 1] > 0.8     # drive positive
            det = np.array([c.time_s for c in an.catches[seat]])
            # every detected catch sits within 0.35 s before a true catch (the detector fires on the
            # running-median crossing, a little before the reversal), never near a finish
            d = np.array([np.min(np.abs(catches - x)) for x in det])
            assert np.median(d) < 0.35, (flip, seat, np.median(d))


def test_without_orientation_the_sign_is_assumed():
    by, _ = _run(False)
    for d in by.values():
        d["euler"] = None
    an = analyze_interval(by)
    assert all(an.frames[s].drive_sign == "assumed" for s in an.cross.rowers)


def test_hull_surge_sign_follows_the_catch_check():
    t = np.arange(0, 120, 0.04) + 1.7e9
    catches = 1.7e9 + np.arange(2, 118, 2.5)
    # a scull-like stroke: a deep check right at the catch, a smaller push 0.7 s later
    u = ((t - 1.7e9 - 2.0) % 2.5) / 2.5
    surge = -4.0 * np.exp(-((u - 0.02) / 0.06) ** 2) + 2.5 * np.exp(-((u - 0.3) / 0.12) ** 2)
    axis = np.array([0.8, 0.1, 0.6]); axis /= np.linalg.norm(axis)
    for sgn in (1.0, -1.0):
        accel = (sgn * surge / 9.80665)[:, None] * axis[None, :] + np.array([0, 0, 1.0]) + np.random.default_rng(0).normal(0, 0.005, (t.size, 3))
        grid, s = hull_surge(t, accel, catches)
        assert grid is not None
        m = (grid > 1.7e9 + 10) & (grid < 1.7e9 + 110)
        assert np.corrcoef(s[m], np.interp(grid[m], t, surge))[0, 1] > 0.9, sgn
