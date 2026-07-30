"""Kalibrierung des Entladewirkungsgrads (ems/battery_calibration.py).

Der Knackpunkt ist die SoC-Aufloesung: gemeldet werden ganze Prozent (bei 22 kWh
rund 223 Wh je Schritt). Eine Slot-weise Auswertung liefert damit unsinnige
Werte - deshalb wird ueber ganze Entladephasen gemessen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.battery_calibration import (EFF_BOUNDS, discharge_windows,
                                     fit_discharge_efficiency, maybe_apply)
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
CAP = 22344.0
DT = 0.25


def _phase(start, hours, power_w, efficiency, capacity=CAP, quantise=True):
    """Entladephase mit BEKANNTEM Wirkungsgrad erzeugen.

    Der SoC wird wie beim echten Geraet auf ganze Prozent gerundet - genau die
    Quantisierung, an der eine naive Slot-Auswertung scheitert.
    """
    n = int(hours / DT)
    index = pd.date_range(start, periods=n + 1, freq="15min", tz=TZ)
    drawn = power_w * DT / efficiency          # Wh je Slot aus dem Speicher
    soc = 95.0 - np.arange(n + 1) * drawn / capacity * 100.0
    if quantise:
        soc = np.round(soc)
    return pd.DataFrame({"battery_w": [-power_w] * n + [0.0], "soc": soc},
                        index=index)


def test_fit_recovers_a_known_efficiency_despite_soc_quantisation():
    frame = pd.concat([_phase("2026-01-10 20:00", 10.0, 1200.0, 0.78),
                       _phase("2026-01-11 20:00", 11.0, 900.0, 0.78),
                       _phase("2026-01-12 20:00", 9.0, 1400.0, 0.78)])
    fit = fit_discharge_efficiency(frame, CAP, DT)
    assert fit.n_windows == 3
    assert fit.efficiency == pytest.approx(0.78, abs=0.02)
    assert fit.hours == pytest.approx(30.0, abs=0.5)


def test_short_phases_are_ignored():
    """Unter drei Stunden dominiert die 1-%-Quantisierung - solche Fenster
    duerfen den Wert nicht verfaelschen."""
    frame = _phase("2026-01-10 20:00", 1.0, 1200.0, 0.78)
    assert discharge_windows(frame, CAP, DT) == []
    assert fit_discharge_efficiency(frame, CAP, DT).efficiency is None


def test_charging_splits_the_phase():
    """Eine Ladephase dazwischen darf nicht ueberbrueckt werden."""
    a = _phase("2026-01-10 20:00", 5.0, 1000.0, 0.8)
    charge = pd.DataFrame(
        {"battery_w": [2000.0] * 8, "soc": [70.0] * 8},
        index=pd.date_range("2026-01-11 01:15", periods=8, freq="15min", tz=TZ))
    b = _phase("2026-01-11 03:15", 5.0, 1000.0, 0.8)
    windows = discharge_windows(pd.concat([a, charge, b]), CAP, DT)
    assert len(windows) == 2
    assert all(w.slots >= 12 for w in windows)


def test_time_gaps_split_the_phase():
    """Ueber einen Datenausfall darf nicht integriert werden - der SoC-Sprung
    waere sonst der falschen Energie zugeordnet."""
    a = _phase("2026-01-10 20:00", 4.0, 1000.0, 0.8)
    b = _phase("2026-01-11 06:00", 4.0, 1000.0, 0.8)   # 6 h Luecke
    windows = discharge_windows(pd.concat([a, b]), CAP, DT)
    assert len(windows) == 2


def test_standby_is_credited_to_the_delivered_energy():
    """Der Eigenverbrauch des Geraets steckt im SoC-Rueckgang, aber nicht in der
    ans Haus gelieferten Leistung - sonst faellt der Wirkungsgrad zu niedrig aus."""
    frame = _phase("2026-01-10 20:00", 10.0, 1000.0, 0.78)
    without = fit_discharge_efficiency(frame, CAP, DT, standby_w=0.0)
    with_sb = fit_discharge_efficiency(frame, CAP, DT, standby_w=40.0)
    assert with_sb.efficiency > without.efficiency


def test_empty_input_is_handled():
    empty = pd.DataFrame(columns=["battery_w", "soc"])
    fit = fit_discharge_efficiency(empty, CAP, DT)
    assert fit.efficiency is None and fit.n_windows == 0 and not fit.usable


# --------------------------------------------------------------------------- #
# Uebernahme ins Overlay
# --------------------------------------------------------------------------- #
def _fit_with(efficiency, n_windows=8, hours=90.0):
    from ems.battery_calibration import FitResult
    return FitResult(efficiency, n_windows, hours, 100.0, 120.0, 0.05, [])


def test_apply_blends_and_writes_to_the_overlay(tmp_path, monkeypatch):
    cfg = make_config()
    cfg.house_battery.discharge_efficiency = 0.93
    written = {}
    monkeypatch.setattr("ems.config.save_override",
                        lambda path, key, value: written.update({key: value}))
    changed = maybe_apply(_fit_with(0.78), cfg, str(tmp_path / "config.yaml"))
    # Gedaempft: 0.5 * 0.93 + 0.5 * 0.78 - eine einzelne Woche kippt die
    # Planung nicht komplett um.
    assert changed == {"discharge_efficiency": pytest.approx(0.855)}
    assert written == {"house_battery.discharge_efficiency":
                       pytest.approx(0.855)}


def test_apply_refuses_a_thin_sample(tmp_path, monkeypatch):
    cfg = make_config()
    monkeypatch.setattr("ems.config.save_override",
                        lambda *a, **k: pytest.fail("darf nicht schreiben"))
    assert maybe_apply(_fit_with(0.78, n_windows=2, hours=10.0), cfg,
                       str(tmp_path / "config.yaml")) is None


def test_apply_refuses_implausible_values(tmp_path, monkeypatch):
    """Werte ueber 1.0 sind physikalisch unmoeglich und deuten auf ein falsches
    Kapazitaets- oder SoC-Signal - dann lieber nichts uebernehmen."""
    cfg = make_config()
    monkeypatch.setattr("ems.config.save_override",
                        lambda *a, **k: pytest.fail("darf nicht schreiben"))
    for bad in (EFF_BOUNDS[0] - 0.01, EFF_BOUNDS[1] + 0.01, 1.5):
        assert maybe_apply(_fit_with(bad), cfg,
                           str(tmp_path / "config.yaml")) is None


# --------------------------------------------------------------------------- #
# Drift-Monitor: Bilanzpruefung statt SoC-Kurve
# --------------------------------------------------------------------------- #
def _seed_actuals(db, phases=8, efficiency=0.78):
    """Mehrere Naechte echter Entladephasen in die actuals-Tabelle schreiben."""
    from ems.local_history import write_actuals
    for k in range(phases):
        frame = _phase(pd.Timestamp("2026-01-06 20:00", tz=TZ)
                       + pd.Timedelta(days=k), 11.0, 1100.0, efficiency)
        for ts, row in frame.iterrows():
            write_actuals(db, ts, {"battery_w": float(row["battery_w"]),
                                   "soc_percent": float(row["soc"]),
                                   "pv_w": 0.0, "house_load_w": 1100.0,
                                   "grid_w": 0.0})


def test_energy_model_check_flags_a_wrong_efficiency(tmp_path, monkeypatch):
    """Der Fall, der monatelang unbemerkt blieb: Modell 0.93, real 0.78."""
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    cfg.house_battery.capacity_wh = CAP
    cfg.house_battery.discharge_efficiency = 0.93
    _seed_actuals(cfg.e3dc_rscp.history_db_path, efficiency=0.78)
    now = pd.Timestamp("2026-01-14 12:00", tz=TZ)
    out = DriftMonitor(cfg).check_energy_model(now)
    assert out is not None, "Stichprobe sollte reichen"
    assert out["measured"] == pytest.approx(0.78, abs=0.03)
    assert out["deviation_percent"] < -10.0
    assert out["alert"] is True


def test_energy_model_check_is_quiet_when_the_model_fits(tmp_path):
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    cfg.house_battery.capacity_wh = CAP
    cfg.house_battery.discharge_efficiency = 0.78
    _seed_actuals(cfg.e3dc_rscp.history_db_path, efficiency=0.78)
    out = DriftMonitor(cfg).check_energy_model(
        pd.Timestamp("2026-01-14 12:00", tz=TZ))
    assert out is not None and out["alert"] is False
    assert abs(out["deviation_percent"]) < 6.0


def test_energy_model_check_stays_silent_without_data(tmp_path):
    """Lieber nichts melden als aus zwei Fenstern einen Alarm bauen."""
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "leer.sqlite")
    assert DriftMonitor(cfg).check_energy_model(
        pd.Timestamp("2026-01-14 12:00", tz=TZ)) is None


def _seed_audits(db, deviation_w, n=200, tail=False):
    """Zaehlerbasierte Audits mit bekanntem Versatz schreiben."""
    from ems.local_history import write_execution_audit
    base = pd.Timestamp("2026-01-10 00:00", tz="UTC")
    for k in range(n):
        dev = deviation_w
        if tail and k % 20 == 0:        # einzelne grosse Ausreisser
            dev = -1500.0
        write_execution_audit(db, base + pd.Timedelta(minutes=15 * k), {
            "ok": True, "state": "ok", "message": "",
            "planned": {"battery_w": 0.0}, "actual": {"battery_w": dev},
            "deviations": {"battery_w": dev}})


def test_execution_bias_flags_a_one_sided_offset(tmp_path):
    """Ein Versatz, der je Slot unter jeder Einzelschwelle bleibt, aber immer
    dasselbe Vorzeichen hat, muss aufsummiert auffallen."""
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    _seed_audits(cfg.e3dc_rscp.history_db_path, -120.0)
    out = DriftMonitor(cfg).check_execution_bias(
        pd.Timestamp("2026-01-12 06:00", tz="UTC"))
    assert out is not None and out["alert"] is True
    assert out["median_w"] == pytest.approx(-120.0)
    assert out["kwh_per_day"] < 0


def test_execution_bias_ignores_a_heavy_tail(tmp_path):
    """Einzelne grosse Ausreisser duerfen keinen Alarm ausloesen - genau daran
    scheiterte der Mittelwert (er meldete -67 W bei -12 W Median)."""
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    _seed_audits(cfg.e3dc_rscp.history_db_path, -10.0, tail=True)
    out = DriftMonitor(cfg).check_execution_bias(
        pd.Timestamp("2026-01-12 06:00", tz="UTC"))
    assert out is not None and out["alert"] is False
    assert out["median_w"] == pytest.approx(-10.0)
    assert out["mean_w"] < out["median_w"]      # Rand zieht das Mittel weg


def test_execution_bias_needs_a_day_of_data(tmp_path):
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    _seed_audits(cfg.e3dc_rscp.history_db_path, -120.0, n=20)
    assert DriftMonitor(cfg).check_execution_bias(
        pd.Timestamp("2026-01-12 06:00", tz="UTC")) is None


# --------------------------------------------------------------------------- #
# SoC-Kurve: laeuft ueber die lokale SQLite, nicht ueber die InfluxDB
# --------------------------------------------------------------------------- #
def _seed_soc_plan(db, planned, actual, start="2026-01-12 18:00"):
    """Sollfahrplan (execution_plan) und gemessenen SoC (actuals) schreiben."""
    from ems.local_history import write_actuals, write_execution_plan
    idx = pd.date_range(pd.Timestamp(start, tz=TZ), periods=24, freq="15min")
    table = pd.DataFrame({
        "house_soc_percent": [planned(k) for k in range(len(idx))],
        "grid_import_w": 0.0, "grid_export_w": 0.0,
        "batt_dc_charge_w": 0.0, "batt_ac_charge_w": 0.0,
        "batt_discharge_w": 1000.0, "house_load_w": 1000.0, "pv_w": 0.0,
        "mode": "auto"}, index=idx)
    # issued_at vor dem ersten Zielslot: alle Slots werden als Soll gesichert.
    write_execution_plan(db, idx[0] - pd.Timedelta(minutes=5), table,
                         initial_soc_percent=float(table
                                                   ["house_soc_percent"].iloc[0]))
    for k, ts in enumerate(idx):
        write_actuals(db, ts, {"soc_percent": float(actual(k)),
                               "battery_w": -1000.0, "pv_w": 0.0,
                               "house_load_w": 1000.0, "grid_w": 0.0})
    return idx


def _noinflux_repo(cfg):
    """Repository wie im Standalone-Betrieb: kein Signal, kein Writeback."""
    from ems.influx import NoOpRepository
    cfg.influxdb.enabled = False
    return NoOpRepository(cfg)


def test_soc_drift_works_without_influxdb(tmp_path):
    """Die SoC-Pruefung darf nicht am fehlenden predicted_state haengen.

    Ohne InfluxDB lieferte ``read_slots_output`` None und die Pruefung fiel
    STILLSCHWEIGEND aus - im reinen E3DC-Betrieb also immer. Der Sollfahrplan
    liegt lokal in ``execution_plan`` und traegt dieselbe Groesse.
    """
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    cfg.e3dc_rscp.history_source = True
    cfg.monitoring.drift_window_hours = 12.0
    # Plan faellt von 80 % auf 68 %, real nur auf 74 % -> 3 pp mittlerer Fehler.
    idx = _seed_soc_plan(cfg.e3dc_rscp.history_db_path,
                         planned=lambda k: 80.0 - 0.5 * k,
                         actual=lambda k: 80.0 - 0.25 * k)
    repo = _noinflux_repo(cfg)
    assert repo.read_slots_output("predicted_state", "house_soc_percent",
                                  None, None) is None
    mae = DriftMonitor(cfg).check(repo, idx[-1] + pd.Timedelta(minutes=15))
    assert mae is not None, "ohne InfluxDB muss die Pruefung trotzdem laufen"
    assert mae == pytest.approx(2.875, abs=0.1)


def test_soc_drift_is_quiet_when_the_plan_holds(tmp_path):
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    cfg.e3dc_rscp.history_source = True
    idx = _seed_soc_plan(cfg.e3dc_rscp.history_db_path,
                         planned=lambda k: 80.0 - 0.5 * k,
                         actual=lambda k: 80.0 - 0.5 * k)
    mae = DriftMonitor(cfg).check(_noinflux_repo(cfg),
                                  idx[-1] + pd.Timedelta(minutes=15))
    assert mae == pytest.approx(0.0, abs=0.01)


def test_soc_drift_stays_silent_without_a_local_plan(tmp_path):
    """Kein Sollfahrplan und keine InfluxDB: lieber None als eine Zahl."""
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "leer.sqlite")
    cfg.e3dc_rscp.history_source = True
    assert DriftMonitor(cfg).check(
        _noinflux_repo(cfg), pd.Timestamp("2026-01-13 06:00", tz=TZ)) is None


def _seed_load_bias(db, tz, forecast_w, actual_w, days=3):
    """Prognose-Snapshots und gemessene Hauslast mit bekanntem Versatz."""
    from ems.local_history import (write_house_load,
                                    write_optimizer_forecast_archive)
    base = pd.Timestamp("2026-01-10 00:00", tz=tz)
    for d in range(days):
        day = base + pd.Timedelta(days=d)
        idx = pd.date_range(day, periods=96, freq="15min", tz=tz)
        fc = pd.Series([forecast_w(t) if callable(forecast_w) else forecast_w
                        for t in idx], index=idx, dtype="float64")
        # issue_ts VOR Tagesbeginn: genau der Stand, den die Pruefung liest.
        write_optimizer_forecast_archive(
            db, day - pd.Timedelta(minutes=5), {"house_load_w": fc})
        write_house_load(db, {t.tz_convert("UTC").isoformat():
                              (actual_w(t) if callable(actual_w) else actual_w)
                              for t in idx})


def test_load_bias_finds_a_night_only_offset(tmp_path):
    """Ein Sockelfehler trifft oft nur die Nacht und verschwindet im
    Tagesmedian - gemessen +359 W nachts gegen +62 W ueber den Tag."""
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    tz = cfg.general.timezone
    _seed_load_bias(cfg.e3dc_rscp.history_db_path, tz,
                        forecast_w=lambda t: 400.0 if t.hour < 6 else 1200.0,
                        actual_w=lambda t: 1200.0 if t.hour < 6 else 1200.0)
    out = DriftMonitor(cfg).check_load_bias(
        pd.Timestamp("2026-01-13 12:00", tz=tz))
    assert out is not None, "Stichprobe sollte reichen"
    assert out["night_median_w"] < -700.0       # nachts klar zu niedrig
    assert out["alert"] is True                 # trotz unauffaelligem Tag
    assert out["alert_scope"] == "Nacht"
    assert "Grundlastbereinigung ist aktiv" in out["diagnostic"]


def test_load_bias_reports_its_sign_convention(tmp_path):
    """Projektkonvention: Prognose minus Ist (ems/quality.bias_w). Prognose 400
    gegen Ist 1200 heisst also NEGATIV - und die Richtung kommt im Klartext mit,
    damit Anzeigen das Vorzeichen nicht selbst deuten muessen."""
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    tz = cfg.general.timezone
    _seed_load_bias(cfg.e3dc_rscp.history_db_path, tz,
                    forecast_w=400.0, actual_w=1200.0)
    out = DriftMonitor(cfg).check_load_bias(
        pd.Timestamp("2026-01-14 12:00", tz=tz))
    assert out is not None
    from ems.quality import BIAS_CONVENTION
    assert out["sign_convention"] == BIAS_CONVENTION
    assert out["median_w"] < 0 and out["direction"] == "Prognose zu niedrig"
