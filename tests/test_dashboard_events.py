import pandas as pd

from ems.local_history import read_dashboard_events, write_dashboard_event


def test_dashboard_events_roundtrip_and_newest_first(tmp_path):
    path = str(tmp_path / "events.sqlite")
    first = pd.Timestamp("2026-07-22 10:00", tz="Europe/Berlin")
    write_dashboard_event(path, "recalc", "Neuberechnung angefordert",
                          details={"queued": True}, ts=first)
    write_dashboard_event(path, "mode", "Modus geändert", level="warning",
                          ts=first + pd.Timedelta(minutes=1))

    events = read_dashboard_events(path, "Europe/Berlin", limit=10)

    assert [event["kind"] for event in events] == ["mode", "recalc"]
    assert events[0]["level"] == "warning"
    assert events[1]["details"] == {"queued": True}
    assert events[1]["ts"].startswith("2026-07-22T10:00")
