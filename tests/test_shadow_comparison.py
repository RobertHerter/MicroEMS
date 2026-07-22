import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

import ems.main as main
from ems.optimizer import OptimizerInputs
from tests.test_synthetic import make_config


def _result(cost_eur, end_soc):
    idx = pd.date_range("2026-07-22 12:00", periods=2, freq="15min",
                        tz="Europe/Berlin")
    table = pd.DataFrame({
        "grid_import_w": [1000.0, 0.0], "grid_export_w": [0.0, 500.0],
        "pv_curtail_w": [0.0, 100.0], "house_soc_percent": [50.0, end_soc],
        "mode": ["auto", "auto"],
    }, index=idx)
    return SimpleNamespace(table=table, total_cost_ct=cost_eur * 100.0,
                           status="Optimal", solver_seconds=0.1)


def test_automatic_shadow_comparison_contains_all_modes_and_recommendation(monkeypatch):
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
    main._start_shadow_comparison(cfg, inp, _result(10.0, 60.0), [])
    deadline = time.time() + 2.0
    while main._shadow_status["state"] in ("queued", "running") and time.time() < deadline:
        time.sleep(0.01)
    payload = main._shadow_status["result"]
    assert set(payload["modes"]) == {"auto", "asap", "peak", "late"}
    assert calls == ["asap", "peak", "late"]
    assert payload["recommended"] == "late"
    assert payload["modes"]["late"]["curtailment_kwh"] == 0.025
    assert payload["modes"]["late"]["max_soc_at"].endswith("+02:00")
