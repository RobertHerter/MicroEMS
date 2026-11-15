import pandas as pd
import pytest

from ems.manual_schedule import ManualScheduleRunner, ManualScheduleStore
from tests.test_synthetic import make_config


class FakeLink:
    def __init__(self, soc=50.0):
        self.soc = soc
        self.calls = []
        self.state = {"active": False, "action": None, "watts": 0}

    def read_live(self, force=False):
        return {"soc_percent": self.soc}

    def manual_power(self, action, watts=0, seconds=0):
        self.calls.append((action, float(watts), float(seconds)))
        active = action not in ("auto", "stop")
        self.state = {"active": active, "action": action if active else None,
                      "watts": float(watts) if active else 0}
        return dict(self.state)

    def manual_status(self):
        return dict(self.state)


def test_store_persists_and_rejects_overlapping_entries(tmp_path):
    store = ManualScheduleStore(str(tmp_path / "history.sqlite"), "Europe/Berlin")
    now = pd.Timestamp.now(tz="Europe/Berlin")
    first = store.add("charge", now + pd.Timedelta(minutes=5), 3000, 60,
                      max_watts=9000, now=now)

    assert first["status"] == "planned"
    assert ManualScheduleStore(
        str(tmp_path / "history.sqlite"), "Europe/Berlin").get(first["id"])

    with pytest.raises(ValueError, match="überschneidet"):
        store.add("discharge", now + pd.Timedelta(minutes=30), 2000, 30,
                  max_watts=9000, now=now)


def test_runner_starts_due_plan_and_cancel_returns_to_auto(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=50.0)
    runner = ManualScheduleRunner(cfg, link, poll_seconds=10)
    row = runner.add({"action": "charge",
                      "start": pd.Timestamp.now(tz=cfg.general.timezone).isoformat(),
                      "watts": 2500, "duration_minutes": 30})

    runner._tick()

    assert runner.store.get(row["id"])["status"] == "running"
    assert link.calls[-1][0:2] == ("charge", 2500.0)
    runner.cancel(row["id"])
    assert link.calls[-1][0] == "auto"
    assert runner.store.get(row["id"])["status"] == "cancelled"


def test_runner_deletes_finished_schedule_but_not_active_one(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    runner = ManualScheduleRunner(cfg, FakeLink(soc=50.0))
    row = runner.add({"action": "charge",
                      "start": pd.Timestamp.now(tz=cfg.general.timezone).isoformat(),
                      "watts": 2500, "duration_minutes": 30})

    with pytest.raises(ValueError, match="zuerst abbrechen"):
        runner.delete(row["id"])

    runner.cancel(row["id"])
    assert runner.delete(row["id"]) == {"id": row["id"], "deleted": True}
    assert runner.store.get(row["id"]) is None


def test_runner_skips_discharge_at_minimum_soc(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=cfg.house_battery.min_soc_percent)
    runner = ManualScheduleRunner(cfg, link)
    row = runner.add({"action": "discharge",
                      "start": pd.Timestamp.now(tz=cfg.general.timezone).isoformat(),
                      "watts": 2000, "duration_minutes": 30})

    runner._tick()

    saved = runner.store.get(row["id"])
    assert saved["status"] == "skipped"
    assert "Mindest-SoC" in saved["note"]
    assert not link.calls


def test_idle_plan_ignores_watts_and_starts_mode(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=50.0)
    runner = ManualScheduleRunner(cfg, link)
    row = runner.add({"action": "idle",
                      "start": pd.Timestamp.now(tz=cfg.general.timezone).isoformat(),
                      "watts": 5000, "duration_minutes": 15})

    assert row["watts"] == 0.0
    runner._tick()
    assert link.calls[-1][0:2] == ("idle", 0.0)


def test_runner_retries_start_when_soc_is_missing_once(tmp_path, monkeypatch):
    """Ein einzelner Abruf ohne SoC darf den Eintrag nicht endgültig verwerfen."""
    from ems import manual_schedule
    monkeypatch.setattr(manual_schedule, "_SOC_RETRY_SECONDS", 0.0)
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=50.0)
    answers = [None, {}, {"soc_percent": 50.0}]
    link.read_live = lambda force=False: answers.pop(0)
    runner = ManualScheduleRunner(cfg, link)
    row = runner.add({"action": "charge",
                      "start": pd.Timestamp.now(tz=cfg.general.timezone).isoformat(),
                      "watts": 2500, "duration_minutes": 30})

    runner._tick()
    assert runner.store.get(row["id"])["status"] == "planned"
    runner._tick()
    assert runner.store.get(row["id"])["status"] == "planned"
    assert not link.calls
    runner._tick()
    assert runner.store.get(row["id"])["status"] == "running"
    assert link.calls[-1][0:2] == ("charge", 2500.0)


def test_runner_throttles_soc_retries(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=50.0)
    reads = []
    link.read_live = lambda force=False: reads.append(1)
    runner = ManualScheduleRunner(cfg, link)
    runner.add({"action": "charge",
                "start": pd.Timestamp.now(tz=cfg.general.timezone).isoformat(),
                "watts": 2500, "duration_minutes": 30})
    runner._tick()
    runner._tick()
    assert len(reads) == 1          # zweiter Tick liegt in der Wartezeit


def test_runner_marks_window_missed_during_downtime_as_skipped(tmp_path):
    """Ein Fenster, das komplett im Dienststillstand lag, ist nie gelaufen."""
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=50.0)
    runner = ManualScheduleRunner(cfg, link)
    now = pd.Timestamp.now(tz=cfg.general.timezone)
    row = runner.store.add("charge", now - pd.Timedelta(hours=2), 2500, 30,
                           max_watts=5000, now=now - pd.Timedelta(hours=3))

    runner._tick()

    saved = runner.store.get(row["id"])
    assert saved["status"] == "skipped"
    assert "verstrichen" in saved["note"]
    assert not link.calls


def test_runner_completes_plan_that_actually_ran(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=50.0)
    runner = ManualScheduleRunner(cfg, link)
    now = pd.Timestamp.now(tz=cfg.general.timezone)
    row = runner.store.add("charge", now - pd.Timedelta(hours=2), 2500, 30,
                           max_watts=5000, now=now - pd.Timedelta(hours=3))
    runner.store.set_status(row["id"], "running")

    runner._tick()

    assert runner.store.get(row["id"])["status"] == "completed"


def test_store_closes_connection_when_statement_fails(tmp_path, monkeypatch):
    import sqlite3
    store = ManualScheduleStore(str(tmp_path / "history.sqlite"), "Europe/Berlin")
    opened = []

    class Broken:
        closed = False

        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            self.closed = True

    def broken_con():
        opened.append(Broken())
        return opened[-1]

    monkeypatch.setattr(store, "_con", broken_con)
    now = pd.Timestamp.now(tz="Europe/Berlin")
    calls = (lambda: store.get(1), store.active, store.list,
             lambda: store.set_status(1, "running"), lambda: store.delete(1),
             lambda: store.add("charge", now + pd.Timedelta(minutes=5), 3000,
                               60, max_watts=9000, now=now))
    for call in calls:
        with pytest.raises(sqlite3.OperationalError):
            call()
    assert len(opened) == len(calls) and all(c.closed for c in opened)


def test_store_uses_shared_wal_connection(tmp_path):
    store = ManualScheduleStore(str(tmp_path / "history.sqlite"), "Europe/Berlin")
    con = store._con()
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000
    finally:
        con.close()


def _running_plan(tmp_path, monkeypatch, action="discharge"):
    """Läuft bereits; die SoC-Prüfung ist in jedem Tick fällig."""
    from ems import manual_schedule
    monkeypatch.setattr(manual_schedule, "_SOC_CHECK_SECONDS", 0.0)
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    link = FakeLink(soc=50.0)
    runner = ManualScheduleRunner(cfg, link)
    row = runner.add({"action": action,
                      "start": pd.Timestamp.now(tz=cfg.general.timezone).isoformat(),
                      "watts": 2000, "duration_minutes": 30})
    runner._tick()
    assert runner.store.get(row["id"])["status"] == "running"
    return runner, link, row


def test_running_plan_survives_single_missing_soc(tmp_path, monkeypatch):
    """Ein einzelner Lesefehler beendet einen laufenden Eingriff nicht."""
    runner, link, row = _running_plan(tmp_path, monkeypatch)
    answers = [None, {}, {"soc_percent": 50.0}, None, None]
    link.read_live = lambda force=False: answers.pop(0)

    for _ in range(5):          # 2 Fehler, 1 Treffer (Zähler zurück), 2 Fehler
        runner._tick()

    assert runner.store.get(row["id"])["status"] == "running"
    assert [c[0] for c in link.calls] == ["discharge"]      # nie "auto"


def test_running_plan_gives_up_after_repeated_missing_soc(tmp_path, monkeypatch):
    """Ohne SoC ist die Entladegrenze nicht prüfbar: nach _SOC_MAX_MISSES
    Fehlversuchen in Folge zurück auf Auto - als Fehler, nicht als erledigt."""
    from ems import manual_schedule
    runner, link, row = _running_plan(tmp_path, monkeypatch)
    link.read_live = lambda force=False: None

    for _ in range(manual_schedule._SOC_MAX_MISSES - 1):
        runner._tick()
    assert runner.store.get(row["id"])["status"] == "running"
    runner._tick()

    saved = runner.store.get(row["id"])
    assert saved["status"] == "failed"
    assert "SoC wiederholt nicht lesbar" in saved["note"]
    assert link.calls[-1][0] == "auto"
    assert runner._active_id is None


def test_running_plan_still_stops_at_soc_limit(tmp_path, monkeypatch):
    """Die eigentliche Grenzprüfung bleibt: Mindest-SoC erreicht -> beendet."""
    runner, link, row = _running_plan(tmp_path, monkeypatch)
    link.read_live = lambda force=False: None
    runner._tick()                                   # ein Fehlversuch davor
    link.read_live = lambda force=False: {
        "soc_percent": runner.config.house_battery.min_soc_percent}
    runner._tick()

    saved = runner.store.get(row["id"])
    assert saved["status"] == "completed"
    assert "Mindest-SoC" in saved["note"]
    assert link.calls[-1][0] == "auto"
