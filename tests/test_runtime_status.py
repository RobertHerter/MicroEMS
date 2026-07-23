"""Runtime-Statuszustand von run_once (Grundlage von /api/status.json im
Dashboard)."""
from __future__ import annotations

import time as _time

import pandas as pd
import pytest

import ems.main as m
from tests.test_synthetic import make_config

TS = pd.Timestamp("2026-07-23 12:00", tz="Europe/Berlin")


def test_runtime_begin_then_finish_transitions_state():
    """begin -> running (Sequenz erhöht); finish -> ready (progress 100)."""
    m._runtime_update(state="ready", pending_recalc=False)
    seq0 = m._runtime_snapshot()["sequence"]
    trigger = m._runtime_begin()
    snap = m._runtime_snapshot()
    assert snap["state"] == "running" and snap["sequence"] == seq0 + 1
    assert trigger == "schedule"                       # kein Recalc vorgemerkt
    m._runtime_finish(TS, _time.monotonic() - 1.0)
    snap = m._runtime_snapshot()
    assert snap["state"] == "ready" and snap["progress"] == 100
    assert snap["duration_seconds"] >= 0


def test_request_recalc_bundles_repeated_requests():
    """Erster Recalc-Wunsch ist frisch und stellt auf 'queued'; ein zweiter davor
    wird gebündelt (fresh=False)."""
    m._runtime_update(state="ready", pending_recalc=False)
    fresh1, st1 = m._request_runtime_recalc()
    fresh2, _st2 = m._request_runtime_recalc()
    assert fresh1 is True and fresh2 is False
    assert st1["state"] == "queued" and st1["pending_recalc"] is True
    # ein danach gestarteter Lauf meldet den Trigger als 'manual'
    assert m._runtime_begin() == "manual"


def test_finish_returns_to_queued_when_recalc_pending():
    """Wird während des Laufs ein Recalc vorgemerkt, endet der Lauf in 'queued'
    (der Folgelauf ist eingeplant), nicht in 'ready'."""
    m._runtime_update(state="running", pending_recalc=True)
    m._runtime_finish(TS, _time.monotonic())
    assert m._runtime_snapshot()["state"] == "queued"


def test_cycle_overdue_pure():
    """#7: überfällige Sekunden – None bei nie gelaufen/frisch/deaktiviert."""
    assert m._cycle_overdue(None, 100.0, 60.0) is None       # nie erfolgreich
    assert m._cycle_overdue(100.0, 130.0, 60.0) is None      # frisch (30 < 60)
    assert m._cycle_overdue(100.0, 200.0, 60.0) == pytest.approx(40.0)
    assert m._cycle_overdue(100.0, 200.0, 0.0) is None       # Schwelle 0 -> aus


def test_staleness_threshold_auto_configured_off():
    cfg = make_config()
    cfg.general.run_interval_minutes = 20
    cfg.monitoring.cycle_staleness_alert_minutes = 0.0       # auto = 2,5 × 20
    assert m._staleness_threshold_minutes(cfg) == pytest.approx(50.0)
    cfg.monitoring.cycle_staleness_alert_minutes = 30.0
    assert m._staleness_threshold_minutes(cfg) == 30.0
    cfg.monitoring.cycle_staleness_alert_minutes = -1.0      # aus
    assert m._staleness_threshold_minutes(cfg) is None


def test_check_config_dry_run_is_solvable_and_side_effect_free():
    """--check (check_config): validierte Config muss auf Fallback-Eingaben
    lösbar sein und einen Report ohne MQTT/RSCP/Dashboard liefern."""
    cfg = make_config()
    report = m.check_config(cfg)
    assert report["ok"] is True and report["infeasible"] is False
    assert report["status"] in ("Optimal", "Optimal ")
    assert report["horizon_slots"] > 0
    assert "charge_strategy" in report and "pv_source" in report
    assert isinstance(report["fallback_plan_cost_ct"], float)


def test_run_once_sets_error_state_when_repository_init_fails(monkeypatch):
    """P3#1: schlägt die Repository-Initialisierung fehl, muss der Runtime-Status
    auf 'error' fallen – NICHT auf 'running' hängen bleiben (sonst zeigt das
    Dashboard dauerhaft "Zyklus läuft", ohne Fehler und ohne Erholung)."""
    cfg = make_config()
    cfg.e3dc_rscp.enabled = False        # E3DC-Pfad für den Test außen vor

    def boom(config):
        raise RuntimeError("DB nicht erreichbar")

    monkeypatch.setattr(m, "make_repository", boom)
    with pytest.raises(RuntimeError):
        m.run_once(cfg)
    assert m._runtime_snapshot()["state"] == "error"
