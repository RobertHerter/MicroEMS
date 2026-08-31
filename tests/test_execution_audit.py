"""Tests für die Betriebsdiagnose der Plan-Ausführung (_audit_execution).

Kernpunkt: die Netzleistung ist ein Bilanz-Residuum (Netz = Last - PV + Akku).
Weicht PV/Last von der Prognose ab (z.B. PV über Prognose im Peak, der E3DC
speist den Überschuss ein), darf das KEINEN Ausführungsfehler auslösen.
"""
from __future__ import annotations

import sqlite3
import time as _time

import pandas as pd
import pytest

import ems.main as _m
from ems.local_history import (read_execution_audits, read_execution_plan_range,
                               read_optimizer_forecast_origins, write_actuals,
                               write_execution_audit, write_execution_plan,
                               write_optimizer_forecast_archive)
from ems.main import (_audit_execution, _overlay_live_slot_actuals,
                      _signed_plan_total)
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


def test_execution_plan_range_keeps_historic_dashboard_commands(tmp_path):
    cfg = _cfg(tmp_path)
    table = pd.DataFrame([{
        "house_load_w": 500.0, "pv_w": 2500.0,
        "grid_import_w": 0.0, "grid_export_w": 0.0,
        "batt_dc_charge_w": 2000.0, "batt_ac_charge_w": 0.0,
        "batt_discharge_w": 0.0, "mode": "peak",
        "batt_charge_limit_w": 4500.0, "batt_discharge_limit_w": 0.0,
        "batt_grid_charge_w": 0.0, "house_soc_percent": 52.0,
        "decision_reason": "PV-Spitze glätten",
    }], index=[TS])
    write_execution_plan(
        cfg.e3dc_rscp.history_db_path, TS, table,
        initial_soc_percent=50.0,
        dashboard_series={
            "house_load_p10_w": pd.Series([400.0], index=[TS]),
            "house_load_p90_w": pd.Series([700.0], index=[TS]),
        })

    frame = read_execution_plan_range(
        cfg.e3dc_rscp.history_db_path, TS, TS + pd.Timedelta(minutes=15), TZ)
    row = frame.loc[TS]
    assert row["house_load_w"] == 500.0
    assert row["house_load_p10_w"] == 400.0
    assert row["batt_dc_charge_w"] == 2000.0
    assert row["planned_battery_w"] == 2000.0
    assert row["planned_grid_w"] == 0.0
    assert row["mode"] == "peak"
    assert row["decision_reason"] == "PV-Spitze glätten"


def test_execution_plan_does_not_rewrite_started_slot(tmp_path):
    cfg = _cfg(tmp_path)
    index = pd.date_range(TS, periods=2, freq="15min")

    def plan(current_w, future_w):
        return pd.DataFrame({
            "batt_dc_charge_w": [current_w, future_w],
            "batt_ac_charge_w": 0.0,
            "batt_discharge_w": 0.0,
            "grid_import_w": 0.0,
            "grid_export_w": 0.0,
            "mode": "peak",
        }, index=index)

    # Vor dem Zielslot ist TS noch Zukunft und darf aktualisiert werden.
    write_execution_plan(
        cfg.e3dc_rscp.history_db_path, TS - pd.Timedelta(minutes=15),
        plan(100.0, 110.0))
    # Erster tatsächlich im Slot publizierter Plan wird historisches Soll.
    write_execution_plan(
        cfg.e3dc_rscp.history_db_path, TS, plan(200.0, 210.0))
    # Recalc/Restart innerhalb desselben Slots darf TS nicht rückwirkend
    # verändern; der weiterhin zukünftige Folgeslot wird aber aktualisiert.
    write_execution_plan(
        cfg.e3dc_rscp.history_db_path, TS, plan(300.0, 310.0))

    frame = read_execution_plan_range(
        cfg.e3dc_rscp.history_db_path, TS, TS + pd.Timedelta(minutes=30), TZ)
    assert frame.loc[TS, "planned_battery_w"] == 200.0
    assert frame.loc[TS + pd.Timedelta(minutes=15),
                     "planned_battery_w"] == 310.0


def test_historic_forecast_uses_final_origin_within_slot(tmp_path):
    cfg = _cfg(tmp_path)
    index = pd.date_range(TS, periods=2, freq="15min")
    for minute, value in ((1, 450.0), (8, 575.0)):
        write_optimizer_forecast_archive(
            cfg.e3dc_rscp.history_db_path,
            TS + pd.Timedelta(minutes=minute),
            {"house_load_w": pd.Series([value, value + 10], index=index),
             "pv_w": pd.Series([2500.0, 2200.0], index=index)})

    frame = read_optimizer_forecast_origins(
        cfg.e3dc_rscp.history_db_path, TS, TS + pd.Timedelta(minutes=15),
        TZ, 15)
    assert list(frame.index) == [TS]
    assert frame.loc[TS, "house_load_w"] == 575.0
    assert frame.loc[TS, "pv_w"] == 2500.0


def test_missing_battery_plan_stays_unknown_instead_of_zero():
    index = pd.date_range(TS, periods=3, freq="15min")
    frame = pd.DataFrame({
        "batt_dc_charge_w": [None, 800.0, 0.0],
        "batt_ac_charge_w": [None, None, 0.0],
        "batt_discharge_w": [None, 0.0, 500.0],
    }, index=index)
    result = _signed_plan_total(
        frame, ("batt_dc_charge_w", "batt_ac_charge_w"),
        ("batt_discharge_w",))
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == 800.0
    assert result.iloc[2] == -500.0


def test_live_slot_averages_replace_shifted_boundary_snapshots():
    index = pd.date_range(
        "2026-07-28 09:15", periods=3, freq="15min", tz="Europe/Berlin")
    frame = pd.DataFrame({
        # Momentan-Snapshots am Slotanfang: Wirkung jeweils erst eine Zeile
        # später sichtbar.
        "actual_pv_w": [6100.0, 6600.0, 7200.0],
        "actual_battery_w": [4950.0, 5750.0, 5660.0],
        "actual_soc_percent": [22.0, 24.0, 27.0],
    }, index=index)
    live = pd.DataFrame({
        "pv_w": [6658.0, 7171.0, 7633.0],
        "battery_w": [5755.0, 5662.0, 1143.0],
    }, index=index)

    corrected = _overlay_live_slot_actuals(
        frame, live, [True, True, False])

    assert corrected.loc[index[0], "actual_pv_w"] == 6658.0
    assert corrected.loc[index[0], "actual_battery_w"] == 5755.0
    assert corrected.loc[index[1], "actual_battery_w"] == 5662.0
    # Zukunft/unvollständiger Bereich wird nicht mit Livewerten gefüllt.
    assert pd.isna(corrected.loc[index[2], "actual_battery_w"])
    # Zustandswerte sind Slotanfangswerte und werden nicht verschoben.
    assert corrected.loc[index[0], "actual_soc_percent"] == 22.0


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


def test_device_error_message_names_the_deviating_quantity(tmp_path):
    """Die Betriebsdiagnose im Dashboard zeigte nur „Geräteabweichung." - daraus
    war nicht ableitbar, WAS abwich. Die Meldung nennt jetzt Ist- und Sollwert
    und den häufigsten Fall (Überschuss ging ins Netz statt in den Akku)."""
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    # Geplant 2000 W laden (= 500 Wh). Real nur 100 Wh geladen, der Rest
    # (400 Wh) ging ins Netz - genau der reale Fall vom 28.07.
    link = _EnergyLink({"pv_wh": 625.0, "load_wh": 125.0,
                        "bat_in_wh": 100.0, "bat_out_wh": 0.0,
                        "grid_import_wh": 0.0, "grid_export_wh": 400.0})
    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=75), {"soc_percent": 50.0}, e3dc=link)
    assert audit["ok"] is False and audit["cause"] == "device"
    message = audit["message"]
    assert message.startswith("Geräteabweichung")
    # Genannt wird das ERREICHBARE, nicht das Geplante: der Plan ist kein
    # Sollwert fuers Geraet, sondern nur Ladelimit/Entladelimit. Hier erlaubte
    # das Ladelimit 5000 W und der reale Ueberschuss 2000 W - geladen wurden
    # 400 W, also ein echter Befund.
    assert "Akku" in message and "möglich waren" in message
    assert "400 W" in message and "2000 W" in message
    assert "Netz" in message           # Überschuss-Hinweis


def test_battery_following_the_real_load_within_its_limits_is_no_device_error(
        tmp_path):
    """Dem E3DC wird kein Akku-SOLLWERT vorgegeben, nur ein Rahmen.

    Der Plan wollte laden; real lag die Hauslast weit ueber der Prognose, es gab
    keinen Ueberschuss, und der Akku hat entladen - innerhalb des erlaubten
    Entladelimits. Das ist die Fortsetzung des Prognosefehlers, kein
    Geraetefehler.

    Gemessen am 31.08.2026 ueber 14 Tage: 105 der 112 als "Geraeteabweichung"
    gemeldeten Slots waren dieser Fall (5 bis 18 Warnungen taeglich), und alle
    1128 unauffaelligen Slots lagen ebenfalls im Rahmen - die Regel erzeugt dort
    also keine neuen Befunde. Uebrig blieben 7 echte: befohlenes Netzladen, das
    ausblieb.
    """
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    # Entladen erlauben (der Plan der Fixture verbietet es mit Limit 0).
    con = sqlite3.connect(cfg.e3dc_rscp.history_db_path)
    con.execute("UPDATE execution_plan SET discharge_limit_w=12120.0")
    con.commit()
    con.close()
    # Keine PV, hohe Last: 2000 W Defizit, vom Akku gedeckt.
    link = _EnergyLink({"pv_wh": 0.0, "load_wh": 500.0,
                        "bat_in_wh": 0.0, "bat_out_wh": 500.0,
                        "grid_import_wh": 0.0, "grid_export_wh": 0.0})
    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=75), {"soc_percent": 48.0}, e3dc=link)
    assert audit["cause"] == "forecast", audit["message"]
    assert audit["ok"] is True, "im Rahmen des Befehls ist kein Fehlschlag"
    assert "im Rahmen des Befehls" in audit["message"]
    # Die Abweichung bleibt sichtbar - nur eben als Prognose, nicht als Gerät.
    assert audit["deviations"]["battery_w"] != 0.0
    assert audit["battery_action"]["ok"] is False


def test_commanded_grid_charging_that_does_not_happen_stays_a_device_error(
        tmp_path):
    """Der Fall, der uebrig bleibt und wirklich einer ist.

    Netzladen ist ein BEFEHL, kein Nebenprodukt des Ueberschusses: bleibt es
    aus, kann der Prognosefehler es nicht erklaeren. Sieben solche Slots in 14
    Tagen, alle am 17.08.2026 mittags.
    """
    cfg = _cfg(tmp_path)
    _completed_plan(cfg)
    con = sqlite3.connect(cfg.e3dc_rscp.history_db_path)
    con.execute("UPDATE execution_plan SET grid_charge_w=4000.0, "
                "battery_w=4000.0, discharge_limit_w=12120.0")
    con.commit()
    con.close()
    # Kein Ueberschuss, aber 4000 W Netzladen befohlen - der Akku tut nichts.
    link = _EnergyLink({"pv_wh": 0.0, "load_wh": 125.0,
                        "bat_in_wh": 0.0, "bat_out_wh": 0.0,
                        "grid_import_wh": 125.0, "grid_export_wh": 0.0})
    audit = _audit_execution(
        cfg, TS + pd.Timedelta(minutes=75), {"soc_percent": 50.0}, e3dc=link)
    assert audit["cause"] == "device", audit["message"]
    assert audit["ok"] is False
    assert "Netzladen" in audit["message"], audit["message"]


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


# --------------------------------------------------------------------------- #
# Slot-Istwerte aus der 5-s-Historie (settle_actual_slots)
# --------------------------------------------------------------------------- #
def _live_slot(db, slot, *, seconds=900, step=5, pv=lambda s: 1000.0):
    """5-s-Livewerte ueber einen Slot schreiben; pv(s) = Wert nach s Sekunden."""
    from ems.local_history import write_live_sample
    for offset in range(0, seconds + 1, step):
        ts = slot + pd.Timedelta(seconds=offset)
        write_live_sample(db, ts, {"pv_w": pv(offset), "house_load_w": 500.0,
                                   "grid_w": -100.0, "battery_w": 400.0})


def test_settle_replaces_the_snapshot_with_the_slot_mean(tmp_path):
    """Der Momentanwert vom Slotanfang weicht dem zeitgewichteten Slotmittel.

    Auf einer Rampe ist der Einzelpunkt systematisch daneben - hier steigt PV
    linear von 0 auf 8000 W, der Slotanfang misst 0, das Mittel sind 4000.
    """
    from ems.local_history import (read_actual, read_live_slot_averages,
                                   settle_actual_slots, write_actuals)

    db = str(tmp_path / "hist.sqlite")
    tz = "Europe/Berlin"
    slot = pd.Timestamp("2026-08-04 08:00", tz=tz)
    write_actuals(db, slot, {"pv_w": 0.0, "house_load_w": 500.0, "grid_w": -100.0,
                             "battery_w": 400.0, "soc_percent": 42.0})
    _live_slot(db, slot, pv=lambda s: 8000.0 * s / 900.0)

    avg = read_live_slot_averages(db, slot, slot + pd.Timedelta(minutes=15),
                                  tz, 15, 180.0, 30.0)
    assert settle_actual_slots(db, avg) >= 1

    pv = read_actual(db, "pv_w", slot, slot + pd.Timedelta(minutes=15), tz)
    assert pv.iloc[0] == pytest.approx(4000.0, abs=50.0)


def test_settle_never_touches_the_soc(tmp_path):
    """SoC ist ein Zustand am Slotanfang, kein Intervallmittel.

    Wuerde er mitgemittelt, verschoebe er sich um einen halben Slot - genau
    gegen die Ausrichtung, die quality.planned_soc_on_measurement_axis
    herstellt. In live_samples steht er ohnehin nicht.
    """
    from ems.local_history import (read_actual, read_live_slot_averages,
                                   settle_actual_slots, write_actuals)

    db = str(tmp_path / "hist.sqlite")
    tz = "Europe/Berlin"
    slot = pd.Timestamp("2026-08-04 08:00", tz=tz)
    write_actuals(db, slot, {"pv_w": 0.0, "house_load_w": 500.0, "grid_w": -100.0,
                             "battery_w": 400.0, "soc_percent": 42.0})
    _live_slot(db, slot, pv=lambda s: 8000.0 * s / 900.0)
    avg = read_live_slot_averages(db, slot, slot + pd.Timedelta(minutes=15),
                                  tz, 15, 180.0, 30.0)
    settle_actual_slots(db, avg)

    soc = read_actual(db, "soc", slot, slot + pd.Timedelta(minutes=15), tz)
    assert soc.iloc[0] == pytest.approx(42.0)


def test_settle_is_idempotent(tmp_path):
    """Jeder Zyklus ruft das auf - zweimal darf nichts anderes herauskommen."""
    from ems.local_history import (read_actual, read_live_slot_averages,
                                   settle_actual_slots, write_actuals)

    db = str(tmp_path / "hist.sqlite")
    tz = "Europe/Berlin"
    slot = pd.Timestamp("2026-08-04 08:00", tz=tz)
    write_actuals(db, slot, {"pv_w": 0.0, "house_load_w": 500.0, "grid_w": -100.0,
                             "battery_w": 400.0, "soc_percent": 42.0})
    _live_slot(db, slot, pv=lambda s: 8000.0 * s / 900.0)
    avg = read_live_slot_averages(db, slot, slot + pd.Timedelta(minutes=15),
                                  tz, 15, 180.0, 30.0)

    settle_actual_slots(db, avg)
    einmal = read_actual(db, "pv_w", slot, slot + pd.Timedelta(minutes=15), tz).iloc[0]
    settle_actual_slots(db, avg)
    zweimal = read_actual(db, "pv_w", slot, slot + pd.Timedelta(minutes=15), tz).iloc[0]
    assert einmal == pytest.approx(zweimal)


def test_settle_leaves_a_barely_started_slot_alone(tmp_path):
    """Unter min_coverage_seconds bleibt der Momentanwert stehen.

    Das ist die Absicherung dafuer, dass auch der LAUFENDE Slot nachgezogen
    werden darf: ein Teilmittel aus zwei Messpunkten waere schlechter als der
    Snapshot, deshalb liefert der Aggregator ihn erst gar nicht.
    """
    from ems.local_history import (read_actual, read_live_slot_averages,
                                   settle_actual_slots, write_actuals)

    db = str(tmp_path / "hist.sqlite")
    tz = "Europe/Berlin"
    slot = pd.Timestamp("2026-08-04 08:00", tz=tz)
    write_actuals(db, slot, {"pv_w": 777.0, "house_load_w": 500.0, "grid_w": -100.0,
                             "battery_w": 400.0, "soc_percent": 42.0})
    _live_slot(db, slot, seconds=60, pv=lambda s: 8000.0)   # nur 1 min Abdeckung

    avg = read_live_slot_averages(db, slot, slot + pd.Timedelta(minutes=15),
                                  tz, 15, 180.0, 30.0)
    settle_actual_slots(db, avg)
    pv = read_actual(db, "pv_w", slot, slot + pd.Timedelta(minutes=15), tz)
    assert pv.iloc[0] == pytest.approx(777.0)


# --------------------------------------------------------------------------- #
# Strangleistungen (pv_strings)
# --------------------------------------------------------------------------- #
class _StringStub:
    """E3DC-Ersatz, der bei jedem Abruf den naechsten Messwertsatz liefert."""

    def __init__(self, folge):
        self.folge = list(folge)
        self.aufrufe = 0

    def read_strings(self):
        self.aufrufe += 1
        return self.folge[min(self.aufrufe - 1, len(self.folge) - 1)]

    def read_live(self, force=False):
        return {"pv_w": 1000.0, "soc_percent": 50.0}


def _monitor(tmp_path, stub):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    return _m._LiveExecutionMonitor(cfg, None, stub), cfg


def test_strings_are_stored_as_the_slot_mean(tmp_path):
    """Nicht der letzte Wert, sondern das Mittel.

    Eine Stichprobe am Slotanfang liegt auf der Vormittagsflanke systematisch
    daneben - genau der Fehler, den die Leistungs-Istwerte hatten.
    """
    from ems.local_history import read_pv_strings

    stub = _StringStub([{0: 1000.0, 1: 500.0},
                        {0: 2000.0, 1: 1000.0},
                        {0: 3000.0, 1: 1500.0}])
    mon, cfg = _monitor(tmp_path, stub)
    slot = pd.Timestamp("2026-08-05 08:00", tz=TZ)
    for k in range(3):
        mon._string_last = 0.0          # Minutensperre fuer den Test loesen
        mon._sample_strings(slot + pd.Timedelta(minutes=k))
    mon._sample_strings(slot + pd.Timedelta(minutes=15))   # Slotwechsel -> Flush

    d = read_pv_strings(cfg.e3dc_rscp.history_db_path, slot,
                        slot + pd.Timedelta(minutes=15), TZ)
    assert list(d.columns) == ["string_0", "string_1"]
    assert d.iloc[0]["string_0"] == pytest.approx(2000.0)   # Mittel, nicht 3000
    assert d.iloc[0]["string_1"] == pytest.approx(1000.0)


def test_strings_are_not_polled_every_five_seconds(tmp_path):
    """Ein PVI-Aufruf kostet ~60 ms; der Watchdog laeuft alle 5 s und muss
    nebenher den Akkumodus halten."""
    stub = _StringStub([{0: 1000.0}])
    mon, _ = _monitor(tmp_path, stub)
    slot = pd.Timestamp("2026-08-05 08:00", tz=TZ)
    for _ in range(20):
        mon._sample_strings(slot)
    assert stub.aufrufe == 1, "Minutensperre greift nicht"


def test_strings_survive_a_device_without_them(tmp_path):
    """Anlagen ohne PVI-Strangwerte duerfen nicht stolpern."""
    from ems.local_history import read_pv_strings

    class Leer:
        def read_strings(self):
            return {}

    mon, cfg = _monitor(tmp_path, Leer())
    slot = pd.Timestamp("2026-08-05 08:00", tz=TZ)
    mon._sample_strings(slot)
    mon._sample_strings(slot + pd.Timedelta(minutes=15))
    assert read_pv_strings(cfg.e3dc_rscp.history_db_path, slot,
                           slot + pd.Timedelta(minutes=30), TZ).empty


def test_string_reader_sums_multiple_inverters():
    """Mehrere Wechselrichter mit gleicher Strangnummer werden addiert."""
    from ems.rscp import E3DCLink

    class Fake(E3DCLink):
        def __init__(self):
            import threading
            self._lock = threading.Lock()

        def _connect(self):
            class E:
                @staticmethod
                def get_pvis_data(keepAlive=True):
                    return [{"strings": {"0": {"power": 1000.0},
                                         "1": {"power": 500.0}}},
                            {"strings": {"0": {"power": 250.0}}}]
            return E()

    assert Fake().read_strings() == {0: 1250.0, 1: 500.0}


class _CauseLink:
    """Livewerte mit Hauslast und SoC - beides braucht die Ursachenerkennung."""

    def __init__(self, battery_w, house_load_w, soc_percent=50.0):
        self.values = {"battery_w": battery_w, "house_load_w": house_load_w,
                       "grid_w": 0.0, "soc_percent": soc_percent}

    def read_live(self, force=False):
        return dict(self.values)


def _plan_abend(cfg, akku_w=-930.0, last_w=930.0):
    """Abendslot: der Akku deckt die prognostizierte Hauslast."""
    table = pd.DataFrame([{
        "grid_import_w": 0.0, "grid_export_w": 0.0,
        "batt_dc_charge_w": 0.0, "batt_ac_charge_w": 0.0,
        "batt_discharge_w": -akku_w, "mode": "auto",
        "batt_charge_limit_w": 5000.0, "batt_discharge_limit_w": 5000.0,
        "batt_grid_charge_w": 0.0, "house_soc_percent": 50.0,
        "house_load_w": last_w,
    }], index=[TS])
    write_execution_plan(cfg.e3dc_rscp.history_db_path, TS, table,
                         initial_soc_percent=50.0)


def _cause_cfg(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.monitoring.execution_live_settle_seconds = 5.0
    cfg.monitoring.execution_live_window_seconds = 20.0
    cfg.monitoring.execution_live_sample_seconds = 5.0
    cfg.monitoring.execution_live_max_gap_seconds = 10.0
    cfg.monitoring.execution_live_consecutive = 2
    cfg.monitoring.execution_battery_tolerance_w = 1500.0
    return cfg


def _dreimal(monitor):
    for sekunden in (0, 5, 10):
        monitor.sample(TS + pd.Timedelta(seconds=sekunden))


def test_unforecast_load_step_is_reported_as_cause_not_as_control_fault(tmp_path):
    """Gemessen am 13.08.2026: Last springt 1200 -> 3660 W in einem Sample, der
    Akku folgt, das Netz bleibt bei 0 W. Delta Akku + Delta Last = 10 W - der
    Regelpfad ist unschuldig, die Lastprognose hat den Verbraucher verpasst."""
    cfg = _cause_cfg(tmp_path)
    _plan_abend(cfg)
    alerts = _Alerts()
    monitor = _m._LiveExecutionMonitor(
        cfg, alerts, _CauseLink(-3338.0, 3348.0))
    _dreimal(monitor)

    assert len(alerts.items) == 1
    stufe, text = alerts.items[0]
    assert stufe == "info", f"Regelwarnung statt Info: {text}"
    assert "Lastsprung" in text
    audit = read_execution_audits(cfg.e3dc_rscp.history_db_path, 1)[0]
    assert "Lastsprung" in (audit["deviations"].get("cause") or "")
    assert "Lastsprung" in audit["message"]
    # nur EINE Meldung, auch wenn die Abweichung anhaelt
    _dreimal(monitor)
    assert len(alerts.items) == 1


def test_deviation_at_the_soc_floor_is_reported_as_cause(tmp_path):
    """Gemessen am 14.08.2026 06:14: SoC real 8,1 % bei min_soc 10 %. Der Plan
    wollte halten, das Geraet entlud weiter - an der Grenze kann es dem Plan
    nicht folgen."""
    cfg = _cause_cfg(tmp_path)
    cfg.house_battery.min_soc_percent = 10.0
    _plan_abend(cfg, akku_w=0.0, last_w=1256.0)
    alerts = _Alerts()
    monitor = _m._LiveExecutionMonitor(
        cfg, alerts, _CauseLink(-1932.0, 1256.0, soc_percent=8.1))
    _dreimal(monitor)

    assert len(alerts.items) == 1
    stufe, text = alerts.items[0]
    assert stufe == "info", f"Regelwarnung statt Info: {text}"
    assert "Untergrenze" in text


def test_unexplained_deviation_still_warns(tmp_path):
    """Gegenprobe: passt die Abweichung zu keiner Ursache, muss die Warnung
    bleiben - sonst hat die Aufraeumaktion den Melder blind gemacht."""
    cfg = _cause_cfg(tmp_path)
    _plan_abend(cfg)
    alerts = _Alerts()
    # Last wie prognostiziert, SoC in der Mitte - der Akku weicht trotzdem ab
    monitor = _m._LiveExecutionMonitor(
        cfg, alerts, _CauseLink(-3338.0, 930.0, soc_percent=55.0))
    _dreimal(monitor)

    assert len(alerts.items) == 1
    assert alerts.items[0][0] == "warning"
    assert "Vorläufige EMS-Live-Abweichung" in alerts.items[0][1]


def test_a_partly_explained_deviation_still_warns(tmp_path):
    """Der Restfehler wird ENGER geprueft als die Abweichung selbst. Sonst
    koennte sich eine echte Regelabweichung von fast einer ganzen Toleranz
    hinter einem Lastsprung verstecken."""
    cfg = _cause_cfg(tmp_path)          # Toleranz 1500 W -> Restschranke 750 W
    _plan_abend(cfg)
    alerts = _Alerts()
    # Lastsprung erklaert 3408 W, die Akkuabweichung betraegt 2408 W:
    # 1000 W bleiben ungeklaert - unter der Toleranz, aber ueber der Schranke.
    monitor = _m._LiveExecutionMonitor(
        cfg, alerts, _CauseLink(-3338.0, 4338.0, soc_percent=55.0))
    _dreimal(monitor)

    assert len(alerts.items) == 1
    assert alerts.items[0][0] == "warning", alerts.items[0][1]
