"""Run setup in the web app: boat class, profile and conditions on a run; the boats page."""
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from srow.boats import BoatProfile


@pytest.fixture
def client(monkeypatch):
    from web import main
    fake = MagicMock()
    fake.fetch_interval_meta.return_value = {"Interval_1": {"boat": "2x", "profile": "p1", "wind_ms": "3", "name": "October piece"}}
    fake.fetch_boat_profiles.return_value = (
        {"p1": BoatProfile(id="p1", name="Our double", cls="2x", overrides={"pin_height_m": 0.31}),
         "p2": BoatProfile(id="p2", name="Old quad", cls="4x", overrides={}, deleted=True)},
        {"2x": "p1"})
    monkeypatch.setattr(main, "influx", fake)
    monkeypatch.setattr(main, "USERS", {"u": "p"})
    c = TestClient(main.app, base_url="https://testserver")   # the session cookie is https-only
    c.post("/login", data={"username": "u", "password": "p"})
    return c, fake


def test_run_setup_resolves_the_profile_on_top_of_the_class(client):
    from web import main
    s = main._run_setup("Interval_1")
    assert s["cls"] == "2x" and s["profile"].id == "p1" and s["rig"].pin_height_m == 0.31 and s["rig_source"] == "profile"
    assert [p.id for p in s["profiles"]] == ["p1"]          # the deleted quad is not offered
    s2 = main._run_setup("Interval_unknown")
    assert s2["cls"] is None and s2["rig"].cls == "2x" and "assumed" in s2["rig_source"]


def test_posting_the_meta_form_writes_one_point_and_returns_the_header(client):
    c, fake = client
    r = c.post("/interval/meta", data={"tag": "intervalId", "value": "Interval_1", "n": "10", "boat": "4x", "profile": "",
                                        "wind_ms": "abc", "wind_dir": "head", "stream": "nope", "water_temp_c": "26.5", "conditions": " choppy "})
    assert r.status_code == 200
    fake.set_interval_meta.assert_called_once_with("Interval_1", boat="4x", profile="", wind_ms="", wind_dir="head",
                                                   stream="", water_temp_c="26.5", conditions="choppy")
    assert "Our double" in r.text and "conditions" in r.text


def test_boats_page_lists_live_profiles_and_saves_a_new_one(client):
    c, fake = client
    r = c.get("/boats")
    assert r.status_code == 200 and "Our double" in r.text and "Old quad" not in r.text
    r = c.post("/boats/save", data={"name": "New pair", "cls": "2-", "oar_len_m": "3.70", "inboard_m": "", "make_default": "1"}, follow_redirects=False)
    assert r.status_code == 303
    saved = fake.save_boat_profile.call_args.args[0]
    assert saved.name == "New pair" and saved.cls == "2-" and saved.overrides == {"oar_len_m": 3.7} and saved.id.startswith("p")
    fake.set_default_profiles.assert_called_once()
    assert fake.set_default_profiles.call_args.args[0]["2-"] == saved.id


def test_deleting_a_profile_hides_it_and_clears_its_default(client):
    c, fake = client
    c.post("/boats/delete", data={"id": "p1"}, follow_redirects=False)
    assert fake.save_boat_profile.call_args.args[0].deleted is True
    fake.set_default_profiles.assert_called_once_with({})


def test_unauthenticated_requests_are_refused(client):
    from web import main
    c = TestClient(main.app, base_url="https://testserver")   # the session cookie is https-only
    assert c.post("/interval/meta", data={"tag": "t", "value": "v"}).status_code == 401
    assert c.get("/boats", follow_redirects=False).status_code == 303
