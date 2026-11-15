"""Slot-0-Anker: nur vom EMS geschaltete Lasten aus der Hauslast nehmen."""
from __future__ import annotations

from types import SimpleNamespace

import ems.main as m


def _cfg(monkeypatch, geschaltet):
    loads = [SimpleNamespace(name=n) for n in ("Pool", "Waschmaschine 1")]
    import ems.local_history as lh
    monkeypatch.setattr(lh, "ems_schaltet_last",
                        lambda ld: ld.name in geschaltet)
    return SimpleNamespace(controllable_loads=loads)


def test_unswitched_load_feedback_is_not_subtracted(monkeypatch):
    """Eingefrorene Rueckmeldung einer Last OHNE Schaltkanal (14.11.2026:
    Waschmaschine 1, 294 W 'an') darf die Hauslast nicht mindern."""
    cfg = _cfg(monkeypatch, {"Pool"})
    status = [{"name": "Waschmaschine 1", "label": "Waschmaschine 1",
               "fresh": True, "on": True, "power_w": 294.22}]
    assert m._anchor_controlled_w(cfg, status, {}) == 0.0


def test_switched_load_is_subtracted_measured_or_nominal(monkeypatch):
    cfg = _cfg(monkeypatch, {"Pool"})
    status = [
        {"name": "Pool", "label": "Pool/WP Pinguin", "fresh": True,
         "on": True, "power_w": 640.0},
        {"name": "Pool", "label": "Pool/WP klein", "fresh": True,
         "on": True, "power_w": None},                 # Nennleistung
        {"name": "Pool", "label": "Pool/aus", "fresh": True, "on": False,
         "power_w": 500.0},
        {"name": "Pool", "label": "Pool/alt", "fresh": False, "on": True,
         "power_w": 500.0},
        {"name": "Waschmaschine 1", "label": "Waschmaschine 1",
         "fresh": True, "on": True, "power_w": 294.22},
    ]
    assert m._anchor_controlled_w(
        cfg, status, {"Pool/WP klein": 400.0}) == 1040.0


def test_empty_status_is_zero(monkeypatch):
    assert m._anchor_controlled_w(_cfg(monkeypatch, set()), None, {}) == 0.0
