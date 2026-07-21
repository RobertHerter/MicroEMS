"""Tests der RSCP-Anbindung mit gemocktem E3DC-Client (kein pye3dc/Hardware)."""
from __future__ import annotations

from ems.rscp import E3DCLink
from tests.test_synthetic import make_config


def test_house_history_refresh_window_waits_and_overlaps():
    import pandas as pd
    from ems.main import _house_history_refresh_window
    now = pd.Timestamp("2026-07-16 15:30", tz="Europe/Berlin")
    last = now - pd.Timedelta(minutes=15)  # möglicherweise unreifer Nullwert
    start, end = _house_history_refresh_window(now, last, 15, 60, 3)
    assert end == now - pd.Timedelta(minutes=60)
    assert start == end - pd.Timedelta(hours=3)


def test_house_history_refresh_window_initial_read_is_one_day():
    import pandas as pd
    from ems.main import _house_history_refresh_window
    now = pd.Timestamp("2026-07-16 15:37", tz="Europe/Berlin")
    start, end = _house_history_refresh_window(now, None, 15, 60, 3)
    assert end == pd.Timestamp("2026-07-16 14:30", tz="Europe/Berlin")
    assert end - start == pd.Timedelta(days=1)


class FakeE3DC:
    # Struktur wie pye3dc 0.10 poll() gegen echte Hardware verifiziert:
    # Akku steht unter consumption.battery (+ = Laden), PV unter production.solar.
    def __init__(self):
        self.limits = None
        self.limit_calls = []          # Verlauf aller set_power_limits(enable=…)
        self.limits_rc = 0             # Rückgabecode (0=ok, 1=angepasst, -1=Fehler)
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
        self.last_db_ts = startTimestamp
        # Energie Wh je 15-min-Fenster (konstant für den Test):
        # Last = solar + bat_out + grid_in(import) - bat_in - grid_out(export)
        #      = 500 + 300 + 250 - 100 - 50 = 900 Wh -> *4 = 3600 W
        return {"solarProduction": 500.0, "bat_power_out": 300.0,
                "grid_power_out": 250.0, "bat_power_in": 100.0,
                "grid_power_in": 50.0, "stateOfCharge": 50.0}

    def get_system_info(self):
        return {"model": "S10X", "maxAcPower": 12000,
                "maxBatChargePower": 12480, "maxBatDischargePower": 12120}

    def get_power_settings(self, keepAlive=False):
        limits = self.limits or {}
        return {
            "dischargeStartPower": 100, "powerSaveEnabled": True,
            "powerLimitsUsed": bool(limits.get("enable", False)),
            "maxChargePower": limits.get("max_charge", 12480),
            "maxDischargePower": limits.get("max_discharge", 12120),
        }

    def set_power_limits(self, enable=None, max_charge=None, max_discharge=None,
                         keepAlive=False):
        self.limits = {"enable": enable, "max_charge": max_charge,
                       "max_discharge": max_discharge}
        self.limit_calls.append(bool(enable))
        return self.limits_rc

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
    status = link.apply_control({
        "batt_charge_limit_w": 1200, "batt_grid_charge_w": 0,
        "batt_discharge_limit_w": hb.max_discharge_w,
        "batt_grid_discharge_w": 0})
    assert link._e3dc.limits["enable"] is True
    assert link._e3dc.limits["max_charge"] == 1200
    assert not link._e3dc.power_calls        # kein SET_POWER/Mode 3
    assert status["ok"] is True
    assert status["actual"]["max_charge_w"] == 1200


def test_control_limit_readback_mismatch_is_failure():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    link._e3dc.get_power_settings = lambda keepAlive=False: {
        "powerLimitsUsed": True, "maxChargePower": 2500,
        "maxDischargePower": hb.max_discharge_w,
    }
    status = link.apply_control({
        "batt_charge_limit_w": 1200, "batt_grid_charge_w": 0,
        "batt_discharge_limit_w": hb.max_discharge_w,
        "batt_grid_discharge_w": 0})
    assert status["ok"] is False and status["state"] == "mismatch"
    assert "Laden Soll 1200 W, Ist 2500 W" in status["message"]


def test_control_limit_readback_failure_is_alarm_state():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery

    def fail(keepAlive=False):
        raise RuntimeError("RSCP nicht erreichbar")

    link._e3dc.get_power_settings = fail
    status = link.apply_control({
        "batt_charge_limit_w": 1200, "batt_grid_charge_w": 0,
        "batt_discharge_limit_w": hb.max_discharge_w,
        "batt_grid_discharge_w": 0})
    assert status["ok"] is False and status["state"] == "readback_failed"
    assert "nicht zurückgelesen" in status["message"]


def test_control_alarm_is_throttled_and_recovery_is_reported():
    import ems.main as main
    cfg = make_config()
    cfg.e3dc_rscp.control_alarm_repeat_minutes = 60

    class Publisher:
        def __init__(self):
            self.alerts = []

        def publish_alert(self, level, message):
            self.alerts.append((level, message))

    pub = Publisher()
    main._control_alarm.update(failed=False, key=None, last=0.0)
    failed = {"ok": False, "state": "mismatch", "message": "Limit falsch"}
    main._publish_control_alarm(pub, cfg, failed)
    main._publish_control_alarm(pub, cfg, failed)
    assert len(pub.alerts) == 1 and pub.alerts[0][0] == "error"
    main._publish_control_alarm(
        pub, cfg, {"ok": True, "state": "confirmed", "message": "bestätigt"})
    assert pub.alerts[-1][0] == "info"
    assert "wieder bestätigt" in pub.alerts[-1][1]


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


def test_close_releases_active_power_limits():
    """Persistente Lade-/Entlade-Limits haben keinen Watchdog -> close() muss sie
    ausdrücklich freigeben (EMS_POWER_LIMITS_USED=false), sonst bleibt der Akku
    nach dem Beenden unbegrenzt gedrosselt."""
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    link.apply_control({"batt_charge_limit_w": 1200, "batt_grid_charge_w": 0,
                        "batt_discharge_limit_w": hb.max_discharge_w,
                        "batt_grid_discharge_w": 0})
    fake = link._e3dc                                # close() setzt _e3dc = None
    assert fake.limits["enable"] is True and link._limits_active
    link.close()
    assert fake.limit_calls[-1] is False            # Freigabe beim Beenden
    assert link._limits_active is False


def test_close_without_active_limit_leaves_limits_untouched():
    """Lief der E3DC frei (kein Limit aktiv), gibt close() nichts frei."""
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    link.apply_control({"batt_charge_limit_w": hb.max_dc_charge_w,
                        "batt_discharge_limit_w": hb.max_discharge_w,
                        "batt_grid_charge_w": 0, "batt_grid_discharge_w": 0})
    fake = link._e3dc                                # close() setzt _e3dc = None
    calls_before = list(fake.limit_calls)            # [False] (Frei-Lauf)
    assert not link._limits_active
    link.close()
    assert fake.limit_calls == calls_before          # close() fügt nichts hinzu


def test_set_limits_flags_rejected_limit(caplog):
    """Lehnt der E3DC das Limit ab (rc=-1), wird gewarnt statt still zu scheitern."""
    import logging
    cfg, link = _link(control_enabled=True)
    link._e3dc.limits_rc = -1
    with caplog.at_level(logging.WARNING, logger="ems.rscp"):
        rc = link._set_limits(True, 1200, 3000)
    assert rc == -1
    assert any("NICHT übernommen" in r.message for r in caplog.records)


def test_manual_power_charge_and_revert():
    """Dashboard-Handeingriff: charge -> Mode 4 mit Wert; _manual_revert -> auto."""
    cfg, link = _link(control_enabled=False)   # manuell wirkt auch ohne control_enabled
    res = link.manual_power("charge", 3000.0, seconds=0)   # kein Timer
    assert res["mode"] == 4 and res["watts"] == 3000
    assert link._e3dc.last_power == (4, 3000)
    assert link._e3dc.limits["enable"] is False            # etwaige Limits raus
    link._manual_revert()
    assert link._e3dc.last_power == (0, 0)


def test_manual_power_discharge_clamped_and_auto():
    cfg, link = _link(control_enabled=False)
    hb = cfg.house_battery
    link.manual_power("discharge", 1e9, seconds=0)         # über Grenze -> geclampt
    m, v = link._e3dc.last_power
    assert m == 2 and v == int(hb.max_discharge_w)
    link.manual_power("auto")
    assert link._e3dc.last_power == (0, 0)


def test_manual_power_pv_charge_and_idle_modes():
    _cfg, link = _link(control_enabled=False)
    assert link.manual_power("pv_charge", 2400, seconds=0)["mode"] == 3
    assert link._e3dc.last_power == (3, 2400)
    assert link.manual_power("idle", 9999, seconds=0)["mode"] == 1
    assert link._e3dc.last_power == (1, 0)
    link.manual_power("auto")


def test_manual_power_rejects_unknown_action():
    import pytest
    _cfg, link = _link(control_enabled=False)
    with pytest.raises(ValueError):
        link.manual_power("bogus", 1000)


def test_manual_power_has_priority_over_optimizer_control():
    """Ein laufender Handplan darf vom 15-min-Optimierer nicht beendet werden."""
    cfg, link = _link(control_enabled=True)
    link.manual_power("charge", 3000, seconds=0)
    before = list(link._e3dc.power_calls)

    link.apply_control({"batt_grid_charge_w": 0,
                        "batt_grid_discharge_w": 0,
                        "batt_charge_limit_w": cfg.house_battery.max_dc_charge_w,
                        "batt_discharge_limit_w": cfg.house_battery.max_discharge_w})

    assert link._e3dc.power_calls == before
    assert link.manual_status()["active"] is True
    link.manual_power("auto")


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


def test_house_load_15min_queries_local_time():
    """Regression: die E3DC-Historien-DB liegt in ORTSZEIT. read_house_load_15min
    muss den Slot als lokale Wandzeit abfragen (echter UTC-Epoch + Offset), sonst
    landen die Verbrauchswerte um den Zeitzonen-Offset verschoben (Sommer +2 h)."""
    import pandas as pd
    cfg, link = _link()
    start = pd.Timestamp("2026-07-01 06:00", tz="Europe/Berlin")   # Sommer, +2 h
    link.read_house_load_15min(start, start + pd.Timedelta(minutes=15))
    # erwartet: 06:00 Wandzeit als UTC interpretiert = 06:00Z-Epoch
    expected = int(pd.Timestamp("2026-07-01 06:00", tz="UTC").timestamp())
    assert link._e3dc.last_db_ts == expected, \
        "get_db_data_timestamp nicht mit lokaler Wandzeit abgefragt (Offset fehlt)"
    # Speicher-Schlüssel bleibt die ECHTE UTC-Zeit (04:00Z)
    data = link.read_house_load_15min(start, start + pd.Timedelta(minutes=15))
    assert list(data.keys())[0].startswith("2026-07-01T04:00:00")


def test_house_load_15min_skips_unfinished_zero_balance():
    """Unfertige E3DC-Fenster dürfen keine dauerhaften 0-Werte erzeugen."""
    import pandas as pd
    cfg, link = _link()
    link._e3dc.get_db_data_timestamp = lambda **kwargs: {
        "solarProduction": 100.0, "bat_power_out": 0.0,
        "grid_power_out": 0.0, "bat_power_in": 500.0,
        "grid_power_in": 0.0}
    start = pd.Timestamp("2026-07-01 06:00", tz="Europe/Berlin")
    assert link.read_house_load_15min(
        start, start + pd.Timedelta(minutes=15)) == {}


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


def test_read_system_limits_maps_device_fields():
    """Auto-Auslesen: nur die verlässlichen W-Werte aus system_info/power_settings."""
    _cfg, link = _link(enabled=True)
    lim = link.read_system_limits()
    assert lim == {
        "inverter_max_ac_power_w": 12000.0,
        "max_charge_w": 12480.0,
        "max_discharge_w": 12120.0,
        "min_discharge_w": 100.0,
    }


def test_apply_system_limits_overrides_config():
    from ems.main import _apply_system_limits
    cfg = make_config()
    _apply_system_limits(cfg, {
        "capacity_wh": 22344.0, "inverter_max_ac_power_w": 12000.0,
        "max_charge_w": 12480.0, "max_discharge_w": 12120.0, "min_discharge_w": 100.0})
    assert cfg.house_battery.capacity_wh == 22344.0
    assert cfg.inverter.max_ac_power_w == 12000.0
    assert cfg.house_battery.max_discharge_w == 12120.0
    assert cfg.house_battery.max_charge_w == 12480.0
    assert cfg.optimization.min_discharge_w == 100.0


def test_apply_system_limits_leaves_unlisted_fields():
    from ems.main import _apply_system_limits
    cfg = make_config()
    cap_before = cfg.house_battery.capacity_wh
    _apply_system_limits(cfg, {"min_discharge_w": 100.0})   # nur min_discharge
    assert cfg.optimization.min_discharge_w == 100.0
    assert cfg.house_battery.capacity_wh == cap_before      # Rest unberührt
