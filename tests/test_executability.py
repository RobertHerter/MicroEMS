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


def test_static_export_curtailment_at_cap_is_inverter_work():
    """Regelt der Plan nur so weit ab, dass die Einspeisung AUF der Grenze liegt,
    erledigt das der statische Einspeise-Limiter (Überschuss kappen)."""
    cfg = make_config()
    cfg.inverter.max_export_w = 5000.0
    table = _table(pv_curtail_w=2000.0, grid_export_w=5000.0)   # an der Grenze
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "inverter"


def test_static_export_curtailment_below_cap_needs_actuator():
    """Regelt der Plan PV UNTER die Einspeisegrenze ab (z.B. Negativpreis, Akku
    voll -> Einspeisung auf 0), kann ein statischer Limiter das NICHT leisten.
    Ohne steuerbaren Aktor ist das nur modelliert -> der Audit muss es fangen."""
    cfg = make_config()
    cfg.inverter.max_export_w = 5000.0
    cfg.e3dc_rscp.enabled = False
    cfg.e3dc_rscp.control_enabled = False
    table = _table(pv_curtail_w=2000.0, grid_export_w=0.0)      # unter der Grenze
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "model"

    # Mit bestätigtem RSCP-Derating-Aktor ist dieselbe Abregelung ausführbar.
    cfg.e3dc_rscp.enabled = True
    cfg.e3dc_rscp.control_enabled = True
    cfg.e3dc_rscp.curtailment_control_enabled = True
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "rscp"


def test_inherent_clip_curtailment_is_inverter_work_without_actuator():
    """Abregelung bis zur physischen WR-Clipping-Reserve (PV über AC-Nennleistung)
    ist immer der Wechselrichter – ohne Einspeisegrenze und ohne RSCP-Aktor."""
    cfg = make_config()
    cfg.inverter.max_export_w = None
    cfg.inverter.max_ac_power_w = 6000.0
    cfg.e3dc_rscp.enabled = False
    cfg.e3dc_rscp.control_enabled = False
    # pv 8000, WR 6000 -> inhärentes Clipping 2000; curt 1500 <= 2000 -> inverter
    table = _table(pv_w=8000.0, pv_curtail_w=1500.0, grid_export_w=0.0)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "inverter"
    # curt ÜBER der Clipping-Reserve ohne Aktor -> nur modelliert
    table = _table(pv_w=8000.0, pv_curtail_w=3000.0, grid_export_w=0.0)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "model"


def test_load_command_uses_mqtt_then_model():
    """Eine steuerbare Last (Spalte load_*_w > 0) ohne Abregelung/Akku-Eingriff
    geht per MQTT; ohne MQTT bleibt nur 'model'."""
    cfg = make_config()
    cfg.e3dc_rscp.enabled = False
    cfg.e3dc_rscp.control_enabled = False
    cfg.mqtt.enabled = True
    table = _table(load_pool_w=1500.0)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "mqtt"
    cfg.mqtt.enabled = False
    table = _table(load_pool_w=1500.0)
    annotate_executability(cfg, table)
    assert table.iloc[0]["execution_path"] == "model"


def test_no_command_slot_is_inverter_self_consumption():
    """Kein Eingriff (weder Abregelung noch Akku/Last) -> Eigenverbrauch/freie
    Akkuregelung erledigt der Wechselrichter."""
    cfg = make_config()
    table = _table()
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
