from srow import boats


def test_every_class_has_every_field():
    for c in boats.CLASSES:
        assert set(boats.DEFAULTS[c]) == set(boats.FIELDS)


def test_resolve_falls_through_from_overrides_to_class_default():
    p = boats.BoatProfile(id="p1", name="Our double", cls="2x", overrides={"pin_height_m": 0.31})
    r = boats.resolve(None, p)
    assert r.cls == "2x" and r.pin_height_m == 0.31 and r.oar_len_m == 2.88
    assert abs(r.outboard_m - 2.00) < 1e-9 and r.profile_name == "Our double"


def test_resolve_without_profile_uses_class_then_fallback():
    assert boats.resolve("8+").hull_len_m == 17.5
    assert boats.resolve("nonsense").cls == "2x"
    assert boats.resolve(None).cls == "2x"


def test_pick_profile_prefers_the_runs_own_then_the_class_default():
    a = boats.BoatProfile(id="a", name="A", cls="2x", overrides={})
    b = boats.BoatProfile(id="b", name="B", cls="2x", overrides={}, deleted=True)
    profiles = {"a": a, "b": b}
    assert boats.pick_profile(profiles, {"2x": "a"}, None, "2x") is a
    assert boats.pick_profile(profiles, {"2x": "b"}, None, "2x") is None      # deleted default is ignored
    assert boats.pick_profile(profiles, {}, "a", "4x") is a
    assert boats.pick_profile(profiles, {}, "zzz", "4x") is None


def test_profile_json_round_trip_drops_unknown_fields():
    p = boats.BoatProfile.from_json("x", {"name": "X", "cls": "4-", "overrides": {"hull_len_m": 13.2, "colour": 3}})
    assert p.overrides == {"hull_len_m": 13.2}
    assert boats.BoatProfile.from_json("x", p.to_json()) == p
