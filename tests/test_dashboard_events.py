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


def test_history_reaches_back_thirty_days_not_five_hundred_rows(tmp_path):
    """Die Zeilengrenze von 500 reichte bei 110-160 Ereignissen pro Tag nur gut
    drei Tage.

    Am 31.08.2026 liess sich deshalb nicht mehr feststellen, wann eine
    Warnserie begonnen hatte: die aelteste Zeile fiel mit dem ersten Alarm
    zusammen und sah faelschlich wie ein Beginn aus.
    """
    path = str(tmp_path / "events.sqlite")
    start = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    # 20 Tage x 40 Ereignisse = 800 Zeilen, alle innerhalb von 30 Tagen.
    for tag in range(20):
        for k in range(40):
            write_dashboard_event(
                path, "recalc", f"Lauf {tag}-{k}",
                ts=start + pd.Timedelta(days=tag, minutes=k))
    events = read_dashboard_events(path, "UTC", limit=2000)
    assert len(events) == 800, f"nur {len(events)} Zeilen gelesen"
    # Der aelteste Eintrag muss noch da sein - genau das war vorher weg.
    assert events[-1]["message"] == "Lauf 0-0"


def test_events_older_than_the_window_are_pruned(tmp_path):
    """Begrenzt wird nach ZEIT, gemessen am juengsten Eintrag - nicht an der
    Wanduhr, sonst loescht ein Nachtrag mit altem Zeitstempel sich selbst."""
    path = str(tmp_path / "events.sqlite")
    alt = pd.Timestamp("2026-05-01 12:00", tz="UTC")
    write_dashboard_event(path, "mode", "sehr alt", ts=alt)
    write_dashboard_event(path, "mode", "im Fenster",
                          ts=alt + pd.Timedelta(days=29))
    assert [e["message"] for e in read_dashboard_events(path, "UTC", limit=10)] \
        == ["im Fenster", "sehr alt"]
    # Jetzt einer, der den alten aus dem 30-Tage-Fenster schiebt.
    write_dashboard_event(path, "mode", "viel spaeter",
                          ts=alt + pd.Timedelta(days=40))
    verbleibend = [e["message"] for e in read_dashboard_events(path, "UTC", limit=10)]
    assert "sehr alt" not in verbleibend, verbleibend
    assert verbleibend == ["viel spaeter", "im Fenster"]


def test_reader_limit_reaches_beyond_the_display(tmp_path):
    """Die Anzeige fragt wenige Zeilen ab, eine Diagnose braucht den Verlauf.

    Die frueher fest eingebaute Obergrenze von 200 Zeilen liess jede Abfrage
    nur rund 1,5 Tage zurueckreichen - auch wenn die Tabelle mehr hielt.
    """
    path = str(tmp_path / "events.sqlite")
    start = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    for k in range(400):
        write_dashboard_event(path, "recalc", f"Lauf {k}",
                              ts=start + pd.Timedelta(minutes=k))
    assert len(read_dashboard_events(path, "UTC", limit=400)) == 400
    # Der Standard bleibt klein, damit die Oberflaeche nicht mehr laedt als noetig.
    assert len(read_dashboard_events(path, "UTC")) == 50
