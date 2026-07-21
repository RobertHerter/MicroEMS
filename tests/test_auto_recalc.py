"""Tests für den Auto-Recalc bei großer Live-Abweichung (_live_recalc_needed)."""
from __future__ import annotations

import pandas as pd

from ems.local_history import write_execution_plan
from ems.main import _live_recalc_needed
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


class _FakeE3DC:
    def __init__(self, grid_w):
        self._grid = grid_w

    def read_live(self, force=False):
        return {"grid_w": self._grid, "battery_w": 0.0, "soc_percent": 50.0}


def _cfg(tmp_path):
    cfg = make_config()
    cfg.general.timezone = TZ
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    return cfg


def _write_current_plan(cfg, grid_w):
    slot = pd.Timestamp.now(tz=TZ).floor(f"{cfg.general.slot_minutes}min")
    imp = max(0.0, grid_w)
    exp = max(0.0, -grid_w)
    table = pd.DataFrame([{
        "grid_import_w": imp, "grid_export_w": exp,
        "batt_dc_charge_w": 0.0, "batt_ac_charge_w": 0.0, "batt_discharge_w": 0.0,
        "mode": "auto", "batt_charge_limit_w": 5000.0,
        "batt_discharge_limit_w": 5000.0, "batt_grid_charge_w": 0.0,
        "house_soc_percent": 50.0,
    }], index=[slot])
    write_execution_plan(cfg.e3dc_rscp.history_db_path, slot, table,
                         initial_soc_percent=50.0)


def test_large_grid_deviation_triggers_recalc(tmp_path):
    cfg = _cfg(tmp_path)
    _write_current_plan(cfg, grid_w=0.0)          # Plan: kein Netz
    delta = _live_recalc_needed(cfg, _FakeE3DC(4000.0))  # real: 4 kW Bezug
    assert delta is not None and abs(delta - 4000.0) < 1.0


def test_small_deviation_does_not_trigger(tmp_path):
    cfg = _cfg(tmp_path)
    _write_current_plan(cfg, grid_w=0.0)
    assert _live_recalc_needed(cfg, _FakeE3DC(500.0)) is None   # < 3000 W


def test_disabled_or_no_device(tmp_path):
    cfg = _cfg(tmp_path)
    _write_current_plan(cfg, grid_w=0.0)
    assert _live_recalc_needed(cfg, None) is None
    cfg.recalc.enabled = False
    assert _live_recalc_needed(cfg, _FakeE3DC(9000.0)) is None


def test_no_plan_slot_is_safe(tmp_path):
    cfg = _cfg(tmp_path)   # kein Plan geschrieben
    assert _live_recalc_needed(cfg, _FakeE3DC(9000.0)) is None
