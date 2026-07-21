from __future__ import annotations

import numpy as np
import pandas as pd

from ems.dashboard import _decision_block
from ems.explain import add_plan_explanations
from tests.test_synthetic import make_config


def test_plan_explanations_quantify_grid_charge_hold_and_peak():
    cfg = make_config()
    idx = pd.date_range("2026-07-21 00:00", periods=5, freq="15min",
                        tz="Europe/Berlin")
    table = pd.DataFrame({
        "mode": ["grid_charge", "hold", "auto", "peak", "auto"],
        "price_ct_kwh": [10.0, 20.0, 40.0, 30.0, 30.0],
        "feedin_ct_kwh": [8.0] * 5,
        "pv_w": [0.0, 0.0, 0.0, 3000.0, 0.0],
        "house_load_w": [500.0] * 5,
        "batt_ac_charge_w": [1000.0, 0.0, 0.0, 0.0, 0.0],
        "batt_dc_charge_w": [0.0, 0.0, 0.0, 1500.0, 0.0],
        "batt_discharge_w": [0.0] * 5,
        "batt_grid_discharge_w": [0.0] * 5,
        "grid_import_w": [1500.0, 500.0, 500.0, 0.0, 500.0],
        "export_line_w": [np.nan, np.nan, np.nan, 1000.0, np.nan],
    }, index=idx)

    explained = add_plan_explanations(table, cfg)

    assert "10,0 ct/kWh" in explained.iloc[0]["decision_reason"]
    assert explained.iloc[0]["decision_value_ct"] > 0.0
    assert "40,0 ct/kWh" in explained.iloc[1]["decision_reason"]
    assert explained.iloc[1]["decision_energy_kwh"] == 0.125
    assert "1.000 W" in explained.iloc[3]["decision_reason"]
    assert explained.iloc[2]["decision_reason"] == ""

    html = _decision_block(explained, idx[0])
    assert "Planentscheidungen erklärt" in html
    assert "Netzladen" in html and "Entladen gesperrt" in html
    assert "Modellschätzung" in html
    assert "<details class='decisions'>" in html
    assert "<details class='decisions' open" not in html
