"""Interval names: read from and written to the interval_meta measurement, shown in the sidebar labels."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from srow.services.influx_service import InfluxService


def _service(records_by_tag=None, names_records=None, live_ids=None):
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
        elif "interval_meta" in flux:
            recs = [SimpleNamespace(values={"intervalId": iv}, get_value=lambda v=val: v) for iv, val in (names_records or [])]
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
