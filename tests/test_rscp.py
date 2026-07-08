"""Tests der RSCP-Anbindung mit gemocktem E3DC-Client (kein pye3dc/Hardware)."""
from __future__ import annotations

from ems.rscp import E3DCLink
from tests.test_synthetic import make_config


class FakeE3DC:
    # Struktur wie pye3dc 0.10 poll() gegen echte Hardware verifiziert:
    # Akku steht unter consumption.battery (+ = Laden), PV unter production.solar.
    def __init__(self):
        self.limits = None
        self.last_power = None
        self.power_calls = []
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

    def set_power_limits(self, enable=None, max_charge=None, max_discharge=None,
                         keepAlive=False):
        self.limits = {"enable": enable, "max_charge": max_charge,
                       "max_discharge": max_discharge}

    def sendRequest(self, req, retries=3, keepAlive=False):
        # Erwartet SET_POWER-Container -> Mode/Value mitschreiben
        tag, typ, val = req
        rec = {}
        if isinstance(val, list):
            for c in val:
                ct, cty, cv = c
                rec[getattr(ct, "name", str(ct))] = cv
        self.last_power = (rec.get("EMS_REQ_SET_POWER_MODE"),
                           rec.get("EMS_REQ_SET_POWER_VALUE"))
        self.power_calls.append(self.last_power)
        from e3dc._rscpTags import RscpTag, RscpType
        return (RscpTag.EMS_SET_POWER, RscpType.Uint32, rec.get("EMS_REQ_SET_POWER_VALUE", 0))


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
    link.apply_control({"batt_charge_limit_w": 1000, "batt_discharge_limit_w": 1000})
    assert link._e3dc.limits is None and not link._e3dc.power_calls


def test_control_grid_charge_uses_mode4():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    link.apply_control({"batt_grid_charge_w": 3000, "batt_dc_charge_w": 2000,
                        "batt_ac_charge_w": 3000, "batt_grid_discharge_w": 0,
                        "batt_charge_limit_w": hb.max_dc_charge_w,
                        "batt_discharge_limit_w": hb.max_discharge_w})
    # Mode 4 (grid_charge), Wert = dc+gc = 5000; Limits deaktiviert (Mode regelt)
    fake = link._e3dc
    assert fake.last_power == (4, 5000)
    assert fake.limits["enable"] is False
    link.close()   # Watchdog stoppen, zurück auf auto (Mode 0)
    assert fake.power_calls[-1][0] == 0


def test_control_limit_uses_power_limits_not_mode3():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    # Ladelimit < Hardware-Max, kein Netzladen -> auto + persistentes Limit,
    # KEIN Mode-3-Befehl.
    link.apply_control({"batt_charge_limit_w": 1200, "batt_grid_charge_w": 0,
                        "batt_discharge_limit_w": hb.max_discharge_w,
                        "batt_grid_discharge_w": 0})
    assert link._e3dc.limits["enable"] is True
    assert link._e3dc.limits["max_charge"] == 1200
    assert not link._e3dc.power_calls        # kein SET_POWER/Mode 3


def test_control_grid_discharge_uses_mode2():
    """Netz-Entladen -> Mode 2 (discharge), Wert = Entladeleistung."""
    cfg, link = _link(control_enabled=True)
    cfg.optimization.allow_grid_discharge = True
    hb = cfg.house_battery
    link.apply_control({"batt_grid_charge_w": 0, "batt_grid_discharge_w": 3000,
                        "batt_discharge_w": 3000,
                        "batt_charge_limit_w": hb.max_dc_charge_w,
                        "batt_discharge_limit_w": hb.max_discharge_w})
    assert link._e3dc.last_power == (2, 3000)
    assert link._e3dc.limits["enable"] is False


def test_control_grid_discharge_blocked_when_not_allowed():
    """Ohne allow_grid_discharge kein Mode 2 (fällt auf auto/Limits)."""
    cfg, link = _link(control_enabled=True)
    cfg.optimization.allow_grid_discharge = False
    hb = cfg.house_battery
    link.apply_control({"batt_grid_charge_w": 0, "batt_grid_discharge_w": 3000,
                        "batt_discharge_w": 3000,
                        "batt_charge_limit_w": hb.max_dc_charge_w,
                        "batt_discharge_limit_w": hb.max_discharge_w})
    assert all(m != 2 for m, _ in link._e3dc.power_calls)


def test_control_free_running_disables_limit():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    link.apply_control({"batt_charge_limit_w": hb.max_dc_charge_w,
                        "batt_discharge_limit_w": hb.max_discharge_w,
                        "batt_grid_charge_w": 0, "batt_grid_discharge_w": 0})
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


def test_actuals_roundtrip_and_routing(tmp_path):
    import pandas as pd
    from ems.local_history import (write_actuals, read_actual, read_actual_signal)
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    tz = "Europe/Berlin"
    t0 = pd.Timestamp("2026-07-01 12:00", tz=tz)
    for i in range(4):
        write_actuals(cfg.e3dc_rscp.history_db_path, t0 + pd.Timedelta(minutes=15*i),
                      {"pv_w": 1000 + i, "house_load_w": 500 + i, "grid_w": -100 - i,
                       "battery_w": 600 + i, "soc_percent": 50 + i})
    s = read_actual(cfg.e3dc_rscp.history_db_path, "pv_w", t0,
                    t0 + pd.Timedelta(hours=1), tz)
    assert len(s) == 4 and s.iloc[0] == 1000 and str(s.index.tz) == tz

    class Repo:  # InfluxDB darf im Standalone nicht angefragt werden
        def read_slots(self, *a, **k):
            raise AssertionError("InfluxDB nicht nutzen, wenn history_source")

    cfg.e3dc_rscp.history_source = True
    g = read_actual_signal(cfg, Repo(), "grid_power", t0,
                           t0 + pd.Timedelta(hours=1))
    assert g.iloc[0] == -100
    # nicht-natives Signal (Preis) geht weiter an die InfluxDB
    import pytest
    with pytest.raises(AssertionError):
        read_actual_signal(cfg, Repo(), "electricity_price", t0, t0)


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
