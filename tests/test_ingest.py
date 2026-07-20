"""Tests der Ingest-API (externe Einspeisung) + No-op-Repository (ohne InfluxDB)."""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from ems import ingest
from ems.influx import NoOpRepository, make_repository
from ems.local_history import (read_actual, read_house_load, read_pv_forecast,
                               read_spot)
from tests.test_synthetic import make_config

UTC = "UTC"
TZ = "Europe/Berlin"


def _cfg(tmp_path):
    c = make_config()
    c.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    return c


def test_live_cache_set_get_and_expiry():
    ingest._live.clear()
    ingest.set_live({"soc_percent": 55.0, "pv_w": 1200.0, "house_load_w": 800.0,
                     "grid_w": -400.0, "battery_w": 400.0, "ignored": 1})
    assert ingest.get_live() == {"soc_percent": 55.0, "pv_w": 1200.0,
                                 "house_load_w": 800.0, "grid_w": -400.0,
                                 "battery_w": 400.0}
    assert ingest.get_live(max_age_s=-1.0) is None      # abgelaufen


def test_ingest_house_load(tmp_path):
    c = _cfg(tmp_path)
    msg = ingest.ingest(c, "house_load", {"values": {
        "2026-07-11T10:00:00+00:00": 500.0, "2026-07-11T10:15:00+00:00": 600.0}})
    assert "2 Fenster" in msg
    s = read_house_load(c.e3dc_rscp.history_db_path,
                        pd.Timestamp("2026-07-11 09:00", tz=UTC),
                        pd.Timestamp("2026-07-11 12:00", tz=UTC), TZ)
    assert len(s) == 2 and s.iloc[0] == 500.0


def test_ingest_actuals_and_pv_forecast(tmp_path):
    c = _cfg(tmp_path)
    db = c.e3dc_rscp.history_db_path
    ingest.ingest(c, "actuals", {"values": {"2026-07-11T10:00:00+00:00": {
        "pv_w": 1000, "house_w": 500, "grid_w": -200, "battery_w": 300, "soc": 60}}})
    a = read_actual(db, "pv_w", pd.Timestamp("2026-07-11 09:00", tz=UTC),
                    pd.Timestamp("2026-07-11 12:00", tz=UTC), TZ)
    assert a.iloc[0] == 1000.0
    ingest.ingest(c, "pv_forecast", {"source": "ext", "values": {
        "2026-07-11T10:00:00+00:00": {"pv_w": 2000, "pv10_w": 1500, "pv90_w": 2500}}})
    pv = read_pv_forecast(db, pd.Timestamp("2026-07-11 09:00", tz=UTC),
                          pd.Timestamp("2026-07-11 11:00", tz=UTC), TZ, 15, "sum", "pv")
    assert pv.dropna().iloc[0] == 2000.0


def test_ingest_normalizes_naive_ts_to_utc(tmp_path):
    c = _cfg(tmp_path)
    ingest.ingest(c, "spot", {"values": {"2026-07-11T10:00:00": 12.5}})
    con = sqlite3.connect(c.e3dc_rscp.history_db_path)
    ts = con.execute("SELECT ts FROM spot_price").fetchone()[0]
    con.close()
    assert ts == "2026-07-11T10:00:00+00:00"


def test_ingest_unknown_kind_raises(tmp_path):
    with pytest.raises(KeyError):
        ingest.ingest(_cfg(tmp_path), "bogus", {})


def test_noop_repository_when_influx_disabled():
    c = make_config()
    c.influxdb.enabled = False
    repo = make_repository(c)
    assert isinstance(repo, NoOpRepository)
    assert repo.signal_available("battery_soc") is False
    assert repo.read_scalar_latest("battery_soc", None, None) is None
    assert repo.read_slots("battery_soc", None, None).empty
    repo.write_frame("control_table", None)          # no-op, kein Fehler
    assert repo.read_slots_output("predicted_state", "x", None, None) is None
    repo.close()


def test_last_control_roundtrip_and_types(tmp_path):
    """Sofort-Reapply: aktueller Steuerbefehl wird gesichert und beim Start
    wieder gelesen (numerische + bool-Felder; genau EINE Zeile)."""
    import pandas as pd
    from ems.local_history import write_last_control, read_last_control
    db = str(tmp_path / "h.sqlite")
    ts = pd.Timestamp("2026-07-20 11:30", tz="Europe/Berlin")
    cmd = {"batt_charge_limit_w": 5000.0, "batt_discharge_limit_w": 12000.0,
           "batt_grid_charge_w": 0.0, "charge_limited": True,
           "discharge_limited": False}
    write_last_control(db, ts, cmd)
    # zweiter Schreibvorgang überschreibt (nur eine Zeile)
    write_last_control(db, ts + pd.Timedelta(minutes=15),
                       {**cmd, "batt_charge_limit_w": 3000.0})
    r_ts, r_cmd = read_last_control(db, "Europe/Berlin")
    assert r_ts == ts + pd.Timedelta(minutes=15)
    assert r_cmd["batt_charge_limit_w"] == 3000.0
    assert r_cmd["charge_limited"] == 1.0 and r_cmd["discharge_limited"] == 0.0


def test_read_last_control_empty(tmp_path):
    from ems.local_history import read_last_control
    ts, cmd = read_last_control(str(tmp_path / "empty.sqlite"), "Europe/Berlin")
    assert ts is None and cmd is None
