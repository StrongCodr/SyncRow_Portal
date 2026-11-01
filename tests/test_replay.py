"""The replay payload builds from synthetic kinematics and serialises to JSON."""
import json
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, "tests")
from test_kinematics import synthetic_oar
from srow.analysis import kinematics as K
from srow.analysis.config import DEFAULT
from srow.analysis.frame import build_sensor_frame
from srow.boats import resolve
from web import replay


def _bundle():
    kin = {}
    for seat, seed in (("Seat 1", 1), ("Seat 2", 2)):
        t, accel, gyro, euler, A, catches = synthetic_oar(seconds=50, seed=seed)
        fr = build_sensor_frame(t, accel, gyro, DEFAULT)
        kin[seat] = K.oar_kinematics(seat, t, accel, gyro, euler, fr, catches)
    g = kin["Seat 2"].grid
    hull_accel = -3.0 * np.cos(2 * np.pi * (g - kin["Seat 2"].strokes[0].t0) / 2.5)   # check (minimum) at the catch
    strokes = [SimpleNamespace(stroke_time_s=s.t0, offsets_ms={"Seat 1": 12.0}, corr={"Seat 1": 0.97}, seat_status={"Seat 1": "ok"},
                               is_piece_stroke=lambda: True) for s in kin["Seat 2"].strokes]
    cross = SimpleNamespace(reference="Seat 2", rowers=["Seat 1", "Seat 2"], strokes=strokes)
    att = K.hull_attitude("Cox", g, np.random.default_rng(3).normal(0, 1, (g.size, 3)), np.column_stack([np.sin(g), np.cos(g), 0 * g]))
    return {"kin": kin, "flags": {"Seat 1": {0}}, "cross": cross, "hull": {"Cox": (g, hull_accel)},
            "fused": {"Cox": (g, 2.6 + 0.3 * np.sin(2 * np.pi * g / 2.5))}, "attitude": att}


def _gdf(t0):
    n = 50
    return pd.DataFrame({"time": pd.to_datetime(t0 + np.arange(n), unit="s", utc=True), "latitude": 25.0 + np.arange(n) * 3.0 / 111320.0,
                         "longitude": 55.0, "accuracy": 5.0, "speed": 3.0})


def test_replay_payload_is_complete_and_json_serialisable():
    b = _bundle()
    doc = replay.replay_payload(b, _gdf(b["kin"]["Seat 2"].grid[0]), resolve("2x"))
    assert doc and doc["ref"] == "Seat 2" and doc["grid"]["hz"] == 25 and doc["grid"]["n"] > 1000
    assert [o["name"] for o in doc["oars"]] == ["Seat 1", "Seat 2"]
    o = doc["oars"][0]
    assert len(o["angle"]) == len(o["rate"]) == len(o["height"]) == doc["grid"]["n"]
    assert o["strokes"][0]["flagged"] and not o["strokes"][1]["flagged"]
    assert doc["hull"]["accel"][:10] and doc["hull"]["roll"] is not None
    s = doc["strokes"][1]
    assert s["dt"] == {"Seat 1": 12.0} and s["rho"]["Seat 1"] == 0.97 and s["ok"]["Seat 1"] and s["speed"] and s["split"] and s["dv"]
    assert doc["gps"] and doc["gps"][-1][3] > 100          # distance so far on the last fix
    json.dumps(doc)                                         # no NaN, no numpy types


def test_hull_sign_is_set_by_the_catch_check():
    g = np.arange(0, 60, 0.04) + 1.7e9
    catches = 1.7e9 + np.arange(0, 60, 2.5)
    check_first = -np.cos(2 * np.pi * (g - 1.7e9) / 2.5)   # minimum at each catch
    assert replay.hull_sign_by_catch_check(g, check_first, catches) == 1.0
    assert replay.hull_sign_by_catch_check(g, -check_first, catches) == -1.0


def test_replay_payload_without_oars_is_none():
    assert replay.replay_payload({}, None, None) is None
