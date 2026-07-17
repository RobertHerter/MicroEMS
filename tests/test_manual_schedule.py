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
