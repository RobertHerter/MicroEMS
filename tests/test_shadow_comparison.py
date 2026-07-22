import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

import ems.main as main
from ems.optimizer import OptimizerInputs
from tests.test_synthetic import make_config


def _result(cost_eur, end_soc, *, auto_peak=False):
    idx = pd.date_range("2026-07-22 12:00", periods=2, freq="15min",
                        tz="Europe/Berlin")
    table = pd.DataFrame({
        "grid_import_w": [1000.0, 0.0], "grid_export_w": [0.0, 500.0],
        "pv_curtail_w": [0.0, 100.0], "house_soc_percent": [50.0, end_soc],
        "mode": ["auto", "auto"],
        "export_line_w": ([3000.0, 3000.0] if auto_peak
                          else [np.nan, np.nan]),
    }, index=idx)
    return SimpleNamespace(table=table, total_cost_ct=cost_eur * 100.0,
                           status="Optimal", solver_seconds=0.1)


def test_automatic_shadow_comparison_contains_three_strategies(monkeypatch):
    cfg = make_config()
    cfg.optimization.charge_strategy = "auto"
    cfg.monitoring.shadow_compare_enabled = True
    cfg.monitoring.shadow_recommend_min_savings_eur = 0.05
    idx = pd.date_range("2026-07-22 12:00", periods=2, freq="15min",
                        tz="Europe/Berlin")
    inp = OptimizerInputs(
        index=idx, house_load_w=np.zeros(2), pv_w=np.zeros(2),
        price_ct_kwh=np.ones(2), feedin_ct_kwh=np.zeros(2),
        initial_house_soc_wh=1000.0)
    costs = {"asap": 9.0, "peak": 8.0, "late": 7.0}
    calls = []

    class FakeOptimizer:
        def __init__(self, candidate, **kwargs):
            self.strategy = candidate.optimization.charge_strategy

        def solve(self, inputs):
            calls.append(self.strategy)
            return _result(costs[self.strategy], 70.0)

    monkeypatch.setattr(main, "Optimizer", FakeOptimizer)
    monkeypatch.setattr(main, "validate_plan", lambda *args: [])
    monkeypatch.setattr(main, "_record_dashboard_event", lambda *args, **kwargs: None)
    main._shadow_status.update(state="idle", result=None)
    main._shadow_curves.update(generated=None, series=None)
    main._start_shadow_comparison(
        cfg, inp, _result(10.0, 60.0, auto_peak=True), [])
    deadline = time.time() + 2.0
    while main._shadow_status["state"] in ("queued", "running") and time.time() < deadline:
        time.sleep(0.01)
    payload = main._shadow_status["result"]
    assert set(payload["modes"]) == {"asap", "peak", "late"}
    assert calls == ["asap", "peak", "late"]
    assert payload["configured_mode"] == "auto"
    assert payload["active"] == "peak"
    assert payload["modes"]["peak"]["active"] is True
    assert payload["modes"]["peak"]["intervention_slots"] == 0
    assert payload["recommended"] == "late"
    assert payload["modes"]["late"]["curtailment_kwh"] == 0.025
    assert payload["modes"]["late"]["max_soc_at"].endswith("+02:00")
    assert payload["late_confidence"]["code"] == "unavailable"
    assert main._shadow_curves["generated"].endswith("+02:00")
    assert set(main._shadow_curves["series"]["modes"]) == {
        "asap", "peak", "late"}
    assert len(main._shadow_curves["series"]["timestamp"]) == 2


def test_comparison_metrics_count_intervention_slots():
    result = _result(1.0, 60.0)
    result.table["mode"] = ["hold", "grid_discharge"]
    metrics = main._comparison_metrics_complete(result.table, 100.0, 15)
    assert metrics["intervention_slots"] == 2


def test_late_confidence_distinguishes_expected_from_p10(monkeypatch):
    cfg = make_config()
    cfg.optimization.charge_strategy = "late"
    idx = pd.date_range("2026-07-22 08:00", periods=32, freq="15min",
                        tz="Europe/Berlin")
    pv = np.full(len(idx), 3000.0)
    inp = OptimizerInputs(
        index=idx, house_load_w=np.full(len(idx), 500.0), pv_w=pv,
        pv10_w=np.full(len(idx), 1000.0), price_ct_kwh=np.ones(len(idx)),
        feedin_ct_kwh=np.zeros(len(idx)), initial_house_soc_wh=1000.0)

    def result_with_soc(soc):
        table = pd.DataFrame({
            "house_soc_percent": np.linspace(10.0, soc, len(idx)),
            "batt_ac_charge_w": np.zeros(len(idx)),
        }, index=idx)
        return SimpleNamespace(table=table, solver_seconds=0.2)

    p10_soc = {"value": 80.0}

    class FakeOptimizer:
        def __init__(self, candidate, **kwargs):
            pass

        def solve(self, candidate_inputs):
            return result_with_soc(p10_soc["value"])

    monkeypatch.setattr(main, "Optimizer", FakeOptimizer)
    confidence = main._late_confidence(cfg, inp, result_with_soc(100.0))

    assert confidence["code"] == "expected_only"
    assert confidence["label"] == "nur mit Erwartungsprognose erreichbar"
    assert confidence["expected_soc_percent"] == 100.0
    assert confidence["p10_soc_percent"] == 80.0

    p10_soc["value"] = 100.0
    likely = main._late_confidence(cfg, inp, result_with_soc(100.0))
    assert likely["code"] == "very_likely"
    assert likely["label"] == "100 % sehr wahrscheinlich"

    p10_soc["value"] = 80.0
    unreachable = main._late_confidence(cfg, inp, result_with_soc(80.0))
    assert unreachable["code"] == "p10_unreachable"
    assert unreachable["label"] == "unter P10 nicht erreichbar"
