"""Tests für den Invarianten-Validator: sauberer Plan -> keine Fehler,
manipulierte Pläne -> genau der erwartete Verstoß."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.optimizer import Optimizer, OptimizerInputs
from ems.validate import validate_plan
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _solve(cfg, **kw):
    idx = pd.date_range("2026-06-10 00:00", "2026-06-11 00:00", freq="15min",
                        tz=TZ, inclusive="left")
    n = len(idx)
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    pv = np.clip(7000 * np.exp(-((hour - 13) ** 2) / 7), 0, None)
    defaults = dict(index=idx, house_load_w=np.full(n, 500.0), pv_w=pv,
                    price_ct_kwh=np.full(n, 25.0), feedin_ct_kwh=np.full(n, 8.0),
                    initial_house_soc_wh=3000.0)
    defaults.update(kw)
    inp = OptimizerInputs(**defaults)
    return inp, Optimizer(cfg).solve(inp)


def _rules(viols, severity=None):
    return {x.rule for x in viols if severity is None or x.severity == severity}


def test_clean_plan_has_no_errors():
    cfg = make_config()
    inp, res = _solve(cfg)
    viols = validate_plan(cfg, res, inp)
    assert not _rules(viols, "error"), f"unerwartete Fehler: {_rules(viols, 'error')}"


def test_detects_soc_out_of_bounds():
    cfg = make_config()
    inp, res = _solve(cfg)
    res.table.loc[res.table.index[10], "house_soc_wh"] = 1e6
    assert "soc.bounds" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_simultaneous_charge_discharge():
    cfg = make_config()
    inp, res = _solve(cfg)
    i = res.table.index[30]
    res.table.loc[i, "batt_dc_charge_w"] = 2000
    res.table.loc[i, "batt_discharge_w"] = 2000
    assert "mutex.charge_discharge" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_disguised_grid_charge():
    """DC-Laden über den PV-Überschuss hinaus (getarntes Netzladen)."""
    cfg = make_config()
    inp, res = _solve(cfg)
    i = res.table.index[0]   # nachts: PV=0, also jeder dc>0 ist Überschussverstoß
    res.table.loc[i, "batt_dc_charge_w"] = 1500
    assert "pv.dc_le_surplus" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_discharge_on_surplus():
    cfg = make_config()
    inp, res = _solve(cfg)
    i = res.table.index[52]   # 13:00, PV-Überschuss
    res.table.loc[i, "batt_discharge_w"] = 1000
    res.table.loc[i, "batt_dc_charge_w"] = 0
    assert "battery.no_discharge_on_surplus" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_export_cap_violation():
    cfg = make_config()
    cfg.inverter.max_export_w = 2000.0
    inp, res = _solve(cfg)
    # sauber gelöst -> jetzt künstlich überschreiten
    res.table.loc[res.table.index[50], "grid_export_w"] = 9000
    assert "grid.export_cap" in _rules(validate_plan(cfg, res, inp), "error")
