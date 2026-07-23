"""Runtime-Statuszustand von run_once (Grundlage von /api/status.json im
Dashboard)."""
from __future__ import annotations

import pytest

import ems.main as m
from tests.test_synthetic import make_config


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
