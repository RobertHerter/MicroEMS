"""Tests für die Betriebsdiagnose der Plan-Ausführung (_audit_execution).

Kernpunkt: die Netzleistung ist ein Bilanz-Residuum (Netz = Last - PV + Akku).
Weicht PV/Last von der Prognose ab (z.B. PV über Prognose im Peak, der E3DC
speist den Überschuss ein), darf das KEINEN Ausführungsfehler auslösen.
"""
from __future__ import annotations

import time as _time

import pandas as pd
import pytest

import ems.main as _m
from ems.local_history import (read_execution_audits, write_actuals,
                               write_execution_audit, write_execution_plan)
from ems.main import _audit_execution
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
TS = pd.Timestamp("2026-07-21 12:00", tz=TZ)


@pytest.fixture(autouse=True)
def _past_process_start(monkeypatch):
    """Startup-Karenz für die Standard-Audit-Tests umgehen (Prozessstart weit in
    der Vergangenheit); der Karenz-Test setzt sie gezielt selbst."""
    monkeypatch.setattr(_m, "_PROCESS_START", _time.monotonic() - 36000.0)


def _cfg(tmp_path):
    cfg = make_config()
    cfg.general.timezone = TZ
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    return cfg


def _plan(cfg):
    # Peak-Slot: Akku lädt mit 2000 W, keine Einspeisung geplant.
    table = pd.DataFrame([{
        "grid_import_w": 0.0, "grid_export_w": 0.0,
        "batt_dc_charge_w": 2000.0, "batt_ac_charge_w": 0.0,
        "batt_discharge_w": 0.0, "mode": "peak",
        "batt_charge_limit_w": 5000.0, "batt_discharge_limit_w": 0.0,
        "batt_grid_charge_w": 0.0, "house_soc_percent": 50.0,
    }], index=[TS])
    write_execution_plan(cfg.e3dc_rscp.history_db_path, TS, table,
                         initial_soc_percent=50.0)


class _EnergyLink:
    def __init__(self, meter):
        self.meter = meter
        self.calls = []

    def read_energy_15min(self, start, end):
        self.calls.append((pd.Timestamp(start), pd.Timestamp(end)))
        return {pd.Timestamp(start).tz_convert("UTC").isoformat(): self.meter}


def _completed_plan(cfg, export_limit=None, execution_path="rscp"):
    rows = [{
        "grid_import_w": 0.0, "grid_export_w": 0.0,
        "batt_dc_charge_w": 2000.0, "batt_ac_charge_w": 0.0,
        "batt_discharge_w": 0.0, "mode": "peak", "pv_w": 2500.0,
        "house_load_w": 500.0, "pv_curtail_w": 0.0,
        "batt_charge_limit_w": 5000.0, "batt_discharge_limit_w": 0.0,
        "batt_grid_charge_w": 0.0, "house_soc_percent": 52.0,
        "export_line_w": export_limit, "execution_path": execution_path,
        "execution_label": execution_path,
    }, {
        "grid_import_w": 0.0, "grid_export_w": 0.0,
        "batt_dc_charge_w": 0.0, "batt_ac_charge_w": 0.0,
        "batt_discharge_w": 0.0, "mode": "auto", "pv_w": 0.0,
        "house_load_w": 500.0, "pv_curtail_w": 0.0,
        "house_soc_percent": 52.0,
    }]
    table = pd.DataFrame(rows, index=[TS, TS + pd.Timedelta(minutes=15)])
    write_execution_plan(cfg.e3dc_rscp.history_db_path, TS, table,
                         initial_soc_percent=50.0)


def test_grid_surplus_export_is_not_an_execution_failure(tmp_path):
    """PV über Prognose im Peak -> großer Netz-Export, Akku folgt aber dem Plan.
    Standard: Netz triggert NICHT -> ok."""
    cfg = _cfg(tmp_path)
    _plan(cfg)
    live = {"grid_w": -4000.0, "battery_w": 2000.0, "soc_percent": 50.0}
    audit = _audit_execution(cfg, TS, live)
    assert audit is not None
    assert audit["ok"] is True
    # Netz-Abweichung bleibt informativ sichtbar
    assert audit["deviations"]["grid_w"] == -4000.0


def test_grid_can_still_trigger_when_flag_enabled(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.monitoring.execution_audit_grid = True
    _plan(cfg)
    live = {"grid_w": -4000.0, "battery_w": 2000.0, "soc_percent": 50.0}
    audit = _audit_execution(cfg, TS, live)
    assert audit["ok"] is False and "grid_w" in [
        k for k in audit["deviations"]] and "Netz" in audit["message"]


def test_battery_deviation_still_fails(tmp_path):
    """Der echte Ausführungsfehler (Akku folgt dem Befehl nicht) wird erkannt."""
    cfg = _cfg(tmp_path)
    _plan(cfg)
    live = {"grid_w": 0.0, "battery_w": -3000.0, "soc_percent": 50.0}  # entlädt statt laden
    audit = _audit_execution(cfg, TS, live)
    assert audit["ok"] is False and "Akku" in audit["message"]


def test_completed_slot_uses_meter_energy_and_classifies_device_error(tmp_path):
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    # Statt 500 Wh Laden wurden 500 Wh entladen.
    link = _EnergyLink({"pv_wh": 625.0, "load_wh": 1125.0,
                        "bat_in_wh": 0.0, "bat_out_wh": 500.0,
                        "grid_import_wh": 0.0, "grid_export_wh": 0.0})
    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=75),
        {"soc_percent": 48.0}, e3dc=link)
    assert audit["ok"] is False
    assert audit["cause"] == "device"
    assert audit["deviations"]["battery_energy_kwh"] == -1.0
    assert audit["battery_action"] == {
        "planned": "laden", "actual": "entladen", "ok": False}
    assert link.calls == [(TS, TS + pd.Timedelta(minutes=15))]
    # Der aktuelle Live-SoC (75 min spaeter) darf nicht dem alten Slot
    # zugeschrieben werden, wenn kein historischer SoC archiviert ist.
    assert "soc" not in audit["deviations"]


def test_completed_slot_separates_forecast_deviation(tmp_path):
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    # Akku folgt exakt; deutlich mehr PV/Last verändert nur die Netzbilanz.
    link = _EnergyLink({"pv_wh": 1375.0, "load_wh": 875.0,
                        "bat_in_wh": 500.0, "bat_out_wh": 0.0,
                        "grid_import_wh": 0.0, "grid_export_wh": 0.0})
    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=75),
        {"soc_percent": 52.0}, e3dc=link)
    assert audit["ok"] is True
    assert audit["cause"] == "forecast"
    assert audit["deviations"]["battery_energy_kwh"] == 0.0


class _MultiWindowEnergyLink:
    """read_energy_15min liefert EIN Aggregat je 15-min-Fenster; ein 60-min-Slot
    umfasst 4 Fenster. Gibt je Fenster denselben Teilbeitrag zurück."""
    def __init__(self, per_window):
        self.per_window = per_window

    def read_energy_15min(self, start, end):
        out, t = {}, pd.Timestamp(start)
        while t < pd.Timestamp(end):
            out[t.tz_convert("UTC").isoformat()] = dict(self.per_window)
            t += pd.Timedelta(minutes=15)
        return out


def test_meter_audit_sums_all_windows_for_wide_slots(tmp_path):
    """Regression: bei slot_minutes=60 umfasst ein Plan-Slot 4 Zählerfenster.
    Alle müssen summiert werden – vorher zählte nur das erste Viertel, sodass
    gemessene Leistung/Energie 4× zu niedrig war (falsche Geräteabweichungen)."""
    cfg = _cfg(tmp_path)
    cfg.general.slot_minutes = 60
    _completed_plan(cfg)                       # Slot TS: geplant Laden 2000 W
    # Ist: je 15-min-Fenster 500 Wh Ladung -> 4×500 = 2000 Wh -> 2000 W über 1 h.
    link = _MultiWindowEnergyLink({"pv_wh": 625.0, "load_wh": 125.0,
                                   "bat_in_wh": 500.0, "bat_out_wh": 0.0,
                                   "grid_import_wh": 0.0, "grid_export_wh": 0.0})
    audit = _audit_execution(cfg, TS + pd.Timedelta(minutes=75),
                             {"soc_percent": 52.0}, e3dc=link)
    assert audit["actual"]["battery_w"] == 2000.0    # summiert, nicht 500
    assert audit["deviations"]["battery_energy_kwh"] == 0.0
    assert audit["ok"] is True


def test_meter_audit_skips_and_falls_back_to_live_for_sub_15min_slots(tmp_path):
    """Bei slot_minutes=5 lässt sich ein Plan-Slot nicht aus 15-min-Zählern
    rekonstruieren -> KEIN dauerhaftes data_waiting, sondern Live-Prüfung."""
    cfg = _cfg(tmp_path)
    cfg.general.slot_minutes = 5
    _plan(cfg)                                 # Live-Plan-Slot bei TS
    link = _EnergyLink({"pv_wh": 0.0, "load_wh": 0.0, "bat_in_wh": 0.0,
                        "bat_out_wh": 0.0, "grid_import_wh": 0.0,
                        "grid_export_wh": 0.0})
    live = {"grid_w": 0.0, "battery_w": -3000.0, "soc_percent": 50.0}
    audit = _audit_execution(cfg, TS, live, e3dc=link)
    assert audit is not None
    assert audit["state"] != "data_waiting"          # nicht hängen bleiben
    assert audit["ok"] is False and "Akku" in audit["message"]  # Live-Prüfung greift


def test_delayed_meter_audit_uses_historical_end_soc(tmp_path):
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    write_actuals(cfg.e3dc_rscp.history_db_path,
                  TS + pd.Timedelta(minutes=15), {"soc_percent": 52.0})
    link = _EnergyLink({"pv_wh": 625.0, "load_wh": 125.0,
                        "bat_in_wh": 500.0, "bat_out_wh": 0.0,
                        "grid_import_wh": 0.0, "grid_export_wh": 0.0})

    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=75),
        {"soc_percent": 99.0}, e3dc=link)

    assert audit["actual"]["soc"] == 52.0
    assert audit["deviations"]["soc"] == 0.0


def test_delayed_meter_audit_is_not_suppressed_after_restart(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    link = _EnergyLink({"pv_wh": 625.0, "load_wh": 125.0,
                        "bat_in_wh": 500.0, "bat_out_wh": 0.0,
                        "grid_import_wh": 0.0, "grid_export_wh": 0.0})
    monkeypatch.setattr(_m, "_PROCESS_START", _time.monotonic())

    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=75),
        {"soc_percent": 99.0}, e3dc=link)

    assert audit["state"] != "startup"
    assert link.calls == [(TS, TS + pd.Timedelta(minutes=15))]


def test_startup_grace_suppresses_audit(tmp_path, monkeypatch):
    """Direkt nach dem (Neu-)Start (innerhalb der Karenz) wird das Audit
    ausgesetzt - keine falsche Akku-Abweichung, während die Steuerung erst
    wieder gesetzt/eingependelt wird. Nach der Karenz greift es normal."""
    cfg = _cfg(tmp_path)
    cfg.monitoring.execution_audit_startup_grace_minutes = 5.0
    _plan(cfg)
    live = {"grid_w": 0.0, "battery_w": -3000.0, "soc_percent": 50.0}  # Abweichung
    # innerhalb der Karenz -> neutrale "einpendeln"-Kachel (kein Alarm), keine
    # Abweichung, aber sofort sichtbar (nicht None).
    monkeypatch.setattr(_m, "_PROCESS_START", _time.monotonic())
    audit = _audit_execution(cfg, TS, live)
    assert audit is not None and audit["ok"] is True and audit["state"] == "startup"
    assert "Akku" not in audit["message"] and audit["deviations"] == {}
    # nach der Karenz -> echte Abweichung wird erkannt
    monkeypatch.setattr(_m, "_PROCESS_START", _time.monotonic() - 3600.0)
    audit = _audit_execution(cfg, TS, live)
    assert audit is not None and audit["ok"] is False and "Akku" in audit["message"]


def test_live_interpolation_bridges_only_short_gaps():
    short = [
        (TS, {"battery_w": 0.0}),
        (TS + pd.Timedelta(seconds=10), {"battery_w": 100.0}),
    ]
    assert _m._LiveExecutionMonitor._interpolated_median(
        short, "battery_w", 60.0, 5.0, 10.0) == pytest.approx(50.0)

    # Die 20-s-Lücke darf nicht mit erfundenen Zwischenwerten aufgefüllt
    # werden. Es zählen nur die tatsächlich vorhandenen Randpunkte.
    long = [
        (TS, {"battery_w": 0.0}),
        (TS + pd.Timedelta(seconds=20), {"battery_w": 1000.0}),
        (TS + pd.Timedelta(seconds=25), {"battery_w": 1000.0}),
    ]
    assert _m._LiveExecutionMonitor._interpolated_median(
        long, "battery_w", 60.0, 5.0, 10.0) == pytest.approx(1000.0)


def test_live_energy_does_not_integrate_across_missing_data():
    samples = [
        (TS, {"battery_w": 1000.0}),
        (TS + pd.Timedelta(seconds=5), {"battery_w": 1000.0}),
        (TS + pd.Timedelta(seconds=25), {"battery_w": 1000.0}),
    ]
    energy = _m._LiveExecutionMonitor._window_energy_wh(
        samples, "battery_w", 10.0)
    assert energy == pytest.approx(1000.0 * 5.0 / 3600.0)


class _LiveLink:
    def __init__(self, battery_w):
        self.battery_w = battery_w

    def read_live(self, force=False):
        assert force is True
        return {"battery_w": self.battery_w, "grid_w": 0.0,
                "soc_percent": 50.0}


class _BrokenLiveLink:
    def read_live(self, force=False):
        raise ConnectionError("temporär nicht erreichbar")


class _Alerts:
    def __init__(self):
        self.items = []

    def publish_alert(self, level, message):
        self.items.append((level, message))


def test_live_monitor_warns_early_after_repeated_deviation(tmp_path):
    cfg = _cfg(tmp_path)
    _plan(cfg)
    cfg.monitoring.execution_live_settle_seconds = 5.0
    cfg.monitoring.execution_live_window_seconds = 20.0
    cfg.monitoring.execution_live_sample_seconds = 5.0
    cfg.monitoring.execution_live_max_gap_seconds = 10.0
    cfg.monitoring.execution_live_consecutive = 2
    cfg.monitoring.execution_battery_tolerance_w = 500.0
    alerts = _Alerts()
    monitor = _m._LiveExecutionMonitor(cfg, alerts, _LiveLink(-2000.0))

    monitor.sample(TS)
    monitor.sample(TS + pd.Timedelta(seconds=5))
    monitor.sample(TS + pd.Timedelta(seconds=10))

    assert len(alerts.items) == 1
    assert alerts.items[0][0] == "warning"
    assert "Vorläufige EMS-Live-Abweichung" in alerts.items[0][1]
    audit = read_execution_audits(cfg.e3dc_rscp.history_db_path, 1)[0]
    assert audit["state"] == "live_deviation"
    assert audit["ok"] is False
    assert audit["actual"]["battery_w"] == -2000.0


def test_missing_live_data_never_breaks_execution_loop(tmp_path):
    cfg = _cfg(tmp_path)
    _plan(cfg)
    monitor = _m._LiveExecutionMonitor(cfg, _Alerts(), _BrokenLiveLink())
    assert monitor.sample(TS) is None
    assert read_execution_audits(cfg.e3dc_rscp.history_db_path, 1) == []


def test_confirmed_alarm_ignores_provisional_live_rows(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.monitoring.execution_alert_consecutive = 2
    for minutes in (0, 15):
        write_execution_audit(cfg.e3dc_rscp.history_db_path,
                              TS + pd.Timedelta(minutes=minutes), {
            "ok": False, "state": "live_deviation",
            "message": "nur vorläufig",
        })
    alerts = _Alerts()
    monkey_audit = {"ok": False, "state": "deviation",
                    "message": "noch nicht bestätigt"}
    previous = _m._execution_alarm["failed"]
    try:
        _m._execution_alarm["failed"] = False
        _m._publish_execution_alarm(alerts, cfg, monkey_audit)
        assert alerts.items == []
        assert _m._execution_alarm["failed"] is False
    finally:
        _m._execution_alarm["failed"] = previous
