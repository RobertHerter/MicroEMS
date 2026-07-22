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
from ems.local_history import write_execution_plan
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

    def read_energy_15min(self, start, end):
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
        cfg, TS + pd.Timedelta(minutes=15),
        {"soc_percent": 48.0}, e3dc=link)
    assert audit["ok"] is False
    assert audit["cause"] == "device"
    assert audit["deviations"]["battery_energy_kwh"] == -1.0
    assert audit["battery_action"] == {
        "planned": "laden", "actual": "entladen", "ok": False}


def test_completed_slot_separates_forecast_deviation(tmp_path):
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    # Akku folgt exakt; deutlich mehr PV/Last verändert nur die Netzbilanz.
    link = _EnergyLink({"pv_wh": 1375.0, "load_wh": 875.0,
                        "bat_in_wh": 500.0, "bat_out_wh": 0.0,
                        "grid_import_wh": 0.0, "grid_export_wh": 0.0})
    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=15),
        {"soc_percent": 52.0}, e3dc=link)
    assert audit["ok"] is True
    assert audit["cause"] == "forecast"
    assert audit["deviations"]["battery_energy_kwh"] == 0.0


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
