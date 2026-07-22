import pandas as pd

from ems.executability import annotate_executability
from tests.test_synthetic import make_config


def _table(**values):
    base = {"pv_w": 8000.0, "pv_curtail_w": 0.0,
            "batt_grid_charge_w": 0.0, "batt_grid_discharge_w": 0.0,
            "charge_limited": False, "discharge_limited": False}
    base.update(values)
    return pd.DataFrame([base], index=[pd.Timestamp(
        "2026-07-22 12:00", tz="Europe/Berlin")])


def test_battery_decision_prefers_direct_rscp():
    cfg = make_config()
    cfg.e3dc_rscp.enabled = True
    cfg.e3dc_rscp.control_enabled = True
    cfg.mqtt.enabled = True
    table = _table(discharge_limited=True)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "rscp"


def test_battery_decision_falls_back_to_mqtt_then_model():
    cfg = make_config()
    cfg.e3dc_rscp.control_enabled = False
    cfg.mqtt.enabled = True
    table = _table(batt_grid_charge_w=2000.0)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "mqtt"
    cfg.mqtt.enabled = False
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "model"


def test_static_export_curtailment_is_inverter_work():
    cfg = make_config()
    cfg.inverter.max_export_w = 5000.0
    table = _table(pv_curtail_w=2000.0)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "inverter"


def test_dynamic_curtailment_requires_confirmed_rscp_actuator():
    cfg = make_config()
    cfg.inverter.max_export_w = None
    cfg.inverter.max_ac_power_w = 12000.0
    cfg.e3dc_rscp.enabled = True
    cfg.e3dc_rscp.control_enabled = True
    cfg.e3dc_rscp.curtailment_control_enabled = True
    table = _table(pv_curtail_w=2000.0)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "rscp"
