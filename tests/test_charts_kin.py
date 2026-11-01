"""The kinematics charts build from synthetic oar data."""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "tests")
from test_kinematics import synthetic_oar
from srow.analysis import kinematics as K
from srow.analysis.config import DEFAULT
from srow.analysis.frame import build_sensor_frame
from srow.boats import resolve
from web import charts


def _kin():
    out = {}
    for seat, seed in (("Seat 1", 1), ("Seat 2", 2)):
        t, accel, gyro, euler, A, catches = synthetic_oar(seconds=50, seed=seed)
        fr = build_sensor_frame(t, accel, gyro, DEFAULT)
        out[seat] = K.oar_kinematics(seat, t, accel, gyro, euler, fr, catches)
    return out


def test_loop_arc_and_side_charts_build():
    kin = _kin()
    flags = {"Seat 1": {0, 1}}
    loop = charts.loop_fig(kin, flags, y="rate")
    assert loop and "xaxis2" in loop["layout"] and any(tr["name"] == "Seat 1 flagged" for tr in loop["data"])
    assert any(tr["name"] == "Seat 2 mean" for tr in loop["data"])
    side = charts.loop_fig(kin, flags, y="height", rig=resolve("2x"))
    assert side and side["layout"]["shapes"] and any("drive" in tr["name"] for tr in side["data"])
    arc = charts.arc_fig(kin)
    assert arc and len(arc["layout"]["annotations"]) == 2 and "arc 7" in arc["layout"]["annotations"][0]["text"]
    summ = charts.kin_summary(kin)
    assert len(summ) == 2 and summ[0]["arc"].endswith("°")


def test_stroke_time_and_feather_charts_build():
    kin = _kin()
    g = kin["Seat 2"].grid
    hull = {"Cox": (g, 2.0 * np.sin(2 * np.pi * (g - g[0]) / 2.5))}
    fig = charts.stroke_time_fig(kin, hull, "Seat 2")
    assert fig and any("hull acceleration" in tr.get("name", "") for tr in fig["data"]) and any(a["text"] == "release" for a in fig["layout"]["annotations"])
    assert charts.stroke_time_fig(kin, None, "Seat 2")       # oars alone still draw
    f = charts.feather_fig(kin)
    assert f and len(f["data"]) == 4


def test_attitude_and_gps_stats():
    t = np.arange(0, 40, 0.04) + 1.7e9
    att = K.hull_attitude("Cox", t, np.random.default_rng(0).normal(0, 1, (t.size, 3)),
                          np.column_stack([3 + np.sin(t), -1 + np.cos(t), 100 + 0 * t]))
    fig = charts.attitude_fig(att, [1.7e9 + 5, 1.7e9 + 7.5])
    assert fig and [tr["name"] for tr in fig["data"]][:2] == ["roll (deg)", "pitch (deg)"]
    assert len(charts.attitude_traces(att, 1.7e9 + 10, 1.7e9 + 20, 400)) == 2
    gdf = pd.DataFrame({"time": pd.date_range("2026-10-24", periods=120, freq="1s", tz="UTC"),
                        "latitude": 25.0 + np.arange(120) * 3.0 / 111320.0, "longitude": 55.0, "accuracy": 5.0})
    st = charts.gps_stats(gdf)
    assert st and 340 < st["distance_m"] < 370 and st["split_text"].startswith("2:4")
    assert charts.gps_stats(None) is None


def test_loop_chart_hides_flagged_by_default_carries_stroke_ids_and_draws_a_crew_mean_for_four():
    kin = _kin()
    fig = charts.loop_fig(kin, {"Seat 1": {0}}, y="rate")
    flagged = [tr for tr in fig["data"] if tr["meta"]["kind"] == "flagged"]
    assert flagged and flagged[0]["visible"] is False
    strokes = [tr for tr in fig["data"] if tr["meta"]["kind"] == "strokes"][0]
    assert strokes["customdata"][0][0] == 1 and strokes["customdata"][0][1] > 1.7e9     # [stroke index, catch time]
    assert not any(tr["meta"]["kind"] == "crew" for tr in fig["data"])
    four = {f"Seat {i}": kin["Seat 1" if i % 2 else "Seat 2"] for i in range(1, 5)}
    fig4 = charts.loop_fig(four, None, y="rate")
    assert sum(tr["meta"]["kind"] == "crew" for tr in fig4["data"]) == 4 and "yaxis4" in fig4["layout"]
    assert fig4["layout"]["yaxis3"]["domain"][1] < fig4["layout"]["yaxis"]["domain"][0]       # second row below the first
    assert charts.kin_summary(kin, {"Seat 1": {0, 2}})[0]["flagged"] == 2
