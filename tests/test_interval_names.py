"""Interval names: read from and written to the interval_meta measurement, shown in the sidebar labels."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from srow.services.influx_service import InfluxService


def _service(records_by_tag=None, names_records=None, live_ids=None, profile_records=None):
    """An InfluxService with a fake client: tagValues queries answer from records_by_tag,
    the interval_meta query answers names_records."""
    settings = SimpleNamespace(url="http://x", token="t", org="o", org_id="oid", bucket="b", query_lookback="-30d",
                               effective_org=lambda: "oid")
    svc = InfluxService.__new__(InfluxService)
    svc.settings = settings
    svc._client = MagicMock()
    query_api = MagicMock()

    def query(flux, org=None):
        if "distinct(column: \"intervalId\")" in flux:
            recs = [SimpleNamespace(values={}, get_value=lambda v=iv: v) for iv in (live_ids if live_ids is not None else [])]
        elif "boat_profile" in flux:
            recs = [SimpleNamespace(values={"profile": pid}, get_value=lambda v=doc: v) for pid, doc in (profile_records or [])]
        elif "interval_meta" in flux:
            recs = [SimpleNamespace(values={"intervalId": iv, "_field": (rec[2] if len(rec) > 2 else "name")}, get_value=lambda v=rec[1]: v)
                    for rec in (names_records or []) for iv in [rec[0]]]
        else:
            tag = flux.split('tag: "')[1].split('"')[0]
            recs = [SimpleNamespace(get_value=lambda v=val: v) for val in (records_by_tag or {}).get(tag, [])]
        return [SimpleNamespace(records=recs)]
    query_api.query.side_effect = query
    svc.query_api = lambda: query_api
    return svc


def test_fetch_interval_names_keeps_latest_non_empty():
    svc = _service(names_records=[("Interval_1", "First"), ("Interval_2", "  "), ("Interval_3", " Third ")])
    assert svc.fetch_interval_names() == {"Interval_1": "First", "Interval_3": "Third"}


def test_fetch_interval_tags_labels_named_intervals_with_their_name():
    svc = _service(records_by_tag={"intervalId": ["Interval_1792808575572", "Interval_1791605460000"]},
                   names_records=[("Interval_1792808575572", "October piece")],
                   live_ids=["Interval_1792808575572", "Interval_1791605460000"])
    items = {i["value"]: i for i in svc.fetch_interval_tags()}
    assert items["Interval_1792808575572"]["name"] == "October piece"
    assert items["Interval_1792808575572"]["label"] == "2. October piece"
    assert items["Interval_1791605460000"]["name"] == ""
    assert items["Interval_1791605460000"]["label"].startswith("1. 2026-10-10")
    assert items["Interval_1792808575572"]["date"].startswith("2026-10-24")


def test_set_interval_name_writes_one_point_tagged_with_the_id():
    svc = _service()
    write_api = MagicMock(); svc._client.write_api.return_value = write_api
    svc.set_interval_name("Interval_1792808575572", "  October piece ")
    (kwargs,) = [c.kwargs for c in write_api.write.call_args_list]
    assert kwargs["bucket"] == "b" and kwargs["org"] == "oid"
    line = kwargs["record"].to_line_protocol()
    assert line.startswith('interval_meta,intervalId=Interval_1792808575572 name="October piece" ')


def test_fetch_interval_meta_collects_every_field_and_drops_blanks():
    svc = _service(names_records=[("Interval_1", "First", "name"), ("Interval_1", "2x", "boat"), ("Interval_1", " ", "profile"),
                                  ("Interval_1", 3.5, "wind_ms"), ("Interval_1", "x", "not_a_field")])
    assert svc.fetch_interval_meta() == {"Interval_1": {"name": "First", "boat": "2x", "wind_ms": 3.5}}


def test_set_interval_meta_writes_only_known_fields_and_skips_none():
    svc = _service()
    write_api = MagicMock(); svc._client.write_api.return_value = write_api
    svc.set_interval_meta("Interval_1", boat="2x", profile=None, wind_ms="4", bogus=1)
    line = write_api.write.call_args.kwargs["record"].to_line_protocol()
    assert line.startswith('interval_meta,intervalId=Interval_1 boat="2x",wind_ms="4" ')
    svc.set_interval_meta("Interval_1", profile=None)
    assert write_api.write.call_count == 1


def test_boat_profiles_round_trip_through_json_documents():
    import json
    from srow.boats import BoatProfile
    svc = _service(profile_records=[("p1", json.dumps({"name": "Our double", "cls": "2x", "overrides": {"pin_height_m": 0.3}})),
                                    ("_defaults", json.dumps({"2x": "p1"})), ("bad", "{not json")])
    profiles, defaults = svc.fetch_boat_profiles()
    assert list(profiles) == ["p1"] and profiles["p1"].overrides == {"pin_height_m": 0.3} and defaults == {"2x": "p1"}
    write_api = MagicMock(); svc._client.write_api.return_value = write_api
    svc.save_boat_profile(BoatProfile(id="p2", name="Quad", cls="4x", overrides={}))
    line = write_api.write.call_args.kwargs["record"].to_line_protocol()
    assert line.startswith('boat_profile,profile=p2 json="') and 'Quad' in line
    svc.set_default_profiles({"4x": "p2"})
    assert write_api.write.call_args.kwargs["record"].to_line_protocol().startswith('boat_profile,profile=_defaults json="')


def test_set_interval_name_empty_clears():
    svc = _service()
    write_api = MagicMock(); svc._client.write_api.return_value = write_api
    svc.set_interval_name("Interval_1", "")
    assert 'name="" ' in write_api.write.call_args.kwargs["record"].to_line_protocol()


def test_fetch_interval_tags_drops_ids_whose_rows_were_deleted():
    svc = _service(records_by_tag={"intervalId": ["Interval_1792808575572", "Interval_1786560782938"]},
                   live_ids=["Interval_1792808575572"])
    assert [i["value"] for i in svc.fetch_interval_tags()] == ["Interval_1792808575572"]


def test_fetch_interval_tags_keeps_everything_when_the_live_query_fails():
    svc = _service(records_by_tag={"intervalId": ["Interval_1", "Interval_2"]})
    svc.fetch_live_interval_ids = lambda: None
    assert len(svc.fetch_interval_tags()) == 2
