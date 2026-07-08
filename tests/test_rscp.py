"""Tests der RSCP-Anbindung mit gemocktem E3DC-Client (kein pye3dc/Hardware)."""
from __future__ import annotations

from ems.rscp import E3DCLink
from tests.test_synthetic import make_config


class FakeE3DC:
    # Struktur wie pye3dc 0.10 poll() gegen echte Hardware verifiziert:
    # Akku steht unter consumption.battery (+ = Laden), PV unter production.solar.
    def __init__(self):
        self.limits = None
        self._poll = {
            "stateOfCharge": 63.0,
            "production": {"solar": 4200.0, "add": 0.0, "grid": -1500.0},
            "consumption": {"house": 1800.0, "wallbox": 0.0, "battery": 900.0},
        }

    def poll(self):
        return self._poll

    def get_db_data_timestamp(self, startTimestamp, timespanSeconds, keepAlive=False):
        # Energie Wh je 15-min-Fenster (konstant für den Test):
        # Last = solar + bat_out + grid_in(import) - bat_in - grid_out(export)
        #      = 500 + 300 + 250 - 100 - 50 = 900 Wh -> *4 = 3600 W
        return {"solarProduction": 500.0, "bat_power_out": 300.0,
                "grid_power_out": 250.0, "bat_power_in": 100.0,
                "grid_power_in": 50.0, "stateOfCharge": 50.0}

    def set_power_limits(self, enable=None, max_charge=None, max_discharge=None):
        self.limits = {"enable": enable, "max_charge": max_charge,
                       "max_discharge": max_discharge}


def _link(**rscp):
    cfg = make_config()
    for k, v in rscp.items():
        setattr(cfg.e3dc_rscp, k, v)
    link = E3DCLink(cfg)
    link._e3dc = FakeE3DC()          # echten Verbindungsaufbau umgehen
    return cfg, link


def test_read_live_mapping():
    cfg, link = _link()
    live = link.read_live()
    assert live["soc_percent"] == 63.0
    assert live["pv_w"] == 4200.0
    assert live["house_load_w"] == 1800.0
    assert live["grid_w"] == -1500.0     # grid_sign 1.0
    assert live["battery_w"] == 900.0


def test_read_live_sign_flip_and_cache():
    cfg, link = _link(grid_sign=-1.0, batt_sign=-1.0)
    live = link.read_live()
    assert live["grid_w"] == 1500.0
    assert live["battery_w"] == -900.0
    # Cache: zweiter Aufruf ohne force liefert dasselbe Objekt
    assert link.read_live() is live


def test_read_live_graceful_failure():
    cfg, link = _link()

    class Boom:
        def poll(self):
            raise RuntimeError("keine Verbindung")
    link._e3dc = Boom()
    assert link.read_live() is None


def test_control_disabled_is_noop():
    cfg, link = _link(control_enabled=False)
    assert link.apply_setpoints({"batt_charge_limit_w": 1000,
                                 "batt_discharge_limit_w": 1000}) is False
    assert link._e3dc.limits is None


def test_control_sets_limits_when_deviating():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    # Ladelimit < Hardware-Max -> Begrenzung aktiv
    ok = link.apply_setpoints({"batt_charge_limit_w": 1200,
                               "batt_discharge_limit_w": hb.max_discharge_w,
                               "batt_grid_charge_w": 0})
    assert ok and link._e3dc.limits["enable"] is True
    assert link._e3dc.limits["max_charge"] == 1200


def test_control_free_running_disables_limit():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    # beide Limits = Hardware-Max -> "frei laufen", Begrenzung deaktiviert
    link.apply_setpoints({"batt_charge_limit_w": hb.max_dc_charge_w,
                          "batt_discharge_limit_w": hb.max_discharge_w,
                          "batt_grid_charge_w": 0})
    assert link._e3dc.limits["enable"] is False


def test_house_load_15min_balance_and_keys():
    import pandas as pd
    cfg, link = _link()
    start = pd.Timestamp("2026-07-01 10:00", tz="Europe/Berlin")
    end = start + pd.Timedelta(hours=1)
    data = link.read_house_load_15min(start, end)
    assert len(data) == 4                       # 4 Fenster
    # Bilanz 900 Wh -> 3600 W
    assert all(abs(v - 3600.0) < 1e-6 for v in data.values())
    # Schlüssel = UTC-ISO
    assert all(k.endswith("+00:00") for k in data)


def test_local_history_roundtrip(tmp_path):
    import pandas as pd
    from ems.local_history import (write_house_load, read_house_load,
                                   last_timestamp, count)
    db = str(tmp_path / "h.sqlite")
    tz = "Europe/Berlin"
    idx = pd.date_range("2026-07-01 00:00", periods=8, freq="15min", tz=tz)
    mapping = {t.tz_convert("UTC").isoformat(): 1000.0 + i * 10
               for i, t in enumerate(idx)}
    assert write_house_load(db, mapping) == 8
    assert count(db) == 8
    s = read_house_load(db, idx[0], idx[-1] + pd.Timedelta(minutes=15), tz)
    assert len(s) == 8 and str(s.index.tz) == tz
    assert s.iloc[0] == 1000.0 and s.iloc[-1] == 1070.0
    assert last_timestamp(db).tz_convert(tz) == idx[-1]
    # UPSERT: gleicher Slot überschreibt
    write_house_load(db, {idx[0].tz_convert("UTC").isoformat(): 5.0})
    assert count(db) == 8
    assert read_house_load(db, idx[0], idx[1], tz).iloc[0] == 5.0
