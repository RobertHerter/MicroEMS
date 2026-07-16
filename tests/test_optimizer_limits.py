"""Tests für WR-Sockellast, Mindest-Entladung (#1) und Leistungs-Reserve (#2)."""
from __future__ import annotations

import numpy as np

from ems.optimizer import Optimizer
from tests.test_optimizer import _day_index, _inputs
from tests.test_synthetic import make_config


def test_power_headroom_caps_battery_power():
    """#2: power_headroom_percent deckelt die geplante Entladeleistung."""
    idx = _day_index("2026-01-15")            # Winter, kein PV

    def max_dis(headroom):
        cfg = make_config()
        cfg.optimization.power_headroom_percent = headroom
        cfg.optimization.terminal_soc_value = 0.0   # Restwert 0 -> Akku entlädt voll
        r = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=20000.0, price=40.0,
                                         soc=cfg.house_battery.max_soc_wh))
        assert not r.infeasible
        return float(r.table["batt_discharge_w"].max()), cfg.house_battery.max_discharge_w

    d0, cap = max_dis(0.0)
    d20, _ = max_dis(20.0)
    assert d0 > d20 + 100.0, "Reserve sollte die Spitzen-Entladung senken"
    assert d20 <= cap * 0.8 + 1.0, "Entladung überschreitet die deratete Grenze"


def test_min_discharge_is_semicontinuous():
    """#1: aktive Entladung ist entweder 0 oder >= min_discharge_w."""
    cfg = make_config()
    cfg.optimization.min_discharge_w = 400.0
    cfg.optimization.terminal_soc_value = 0.0   # Restwert 0 -> Entladen erwünscht
    idx = _day_index("2026-01-15")
    load = np.full(len(idx), 250.0)
    load[:20] = 3000.0                        # nur hier lohnt sich >=400 W Entladung
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=load, price=40.0,
                                       soc=cfg.house_battery.max_soc_wh))
    assert not res.infeasible
    dis = res.table["batt_discharge_w"].values
    active = dis > 1.0
    assert active.any(), "Szenario sollte teils entladen"
    assert (dis[active] >= 400.0 - 1.0).all(), "aktive Entladung unter der Mindestleistung"


def test_standby_loss_suppresses_tiny_discharge():
    """#1: hohe WR-Sockellast unterdrückt unwirtschaftliches Kleinlast-Entladen."""
    idx = _day_index("2026-01-15")

    def total_dis(standby):
        cfg = make_config()
        cfg.optimization.standby_discharge_w = standby
        # Restwert 20 ct: 200 W bei 50 ct zu decken lohnt OHNE Sockellast, aber
        # mit 5000 W Sockellast ist der Akku-Drain teurer als der Netzbezug.
        cfg.optimization.terminal_soc_value = 20.0
        r = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=200.0, price=50.0,
                                         soc=cfg.house_battery.max_soc_wh))
        assert not r.infeasible
        return float(r.table["batt_discharge_w"].sum())

    assert total_dis(0.0) > total_dis(5000.0) + 100.0, \
        "Sockellast sollte Kleinlast-Entladung unterdrücken (Netzbezug stattdessen)"


def test_battery_switch_penalty_avoids_single_slot_hold():
    """Ein kurzer Halte-Slot zwischen zwei Entlade-Slots kostet einen Wechsel.

    Das verhindert 0-W-Entladelimits, die bei nur geringfügig abweichenden
    Preisen wirtschaftlich kaum etwas bringen, aber real den E3DC takten.
    """
    cfg = make_config()
    cfg.optimization.battery_switch_penalty_ct = 1.0
    idx = _day_index("2026-01-15")[:8]
    price = np.array([30.0, 30.0, 30.0, 30.2, 30.0, 30.0, 30.0, 30.0])
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=900.0, price=price,
                                       soc=cfg.house_battery.max_soc_wh))
    assert not res.infeasible
    dis = res.table["batt_discharge_w"].values
    # Bei ausreichendem SoC darf keine einzelne 0-W-Pause in einer laufenden
    # Entladephase bleiben.
    assert not ((dis[1:-1] < 1.0) & (dis[:-2] > 1.0) & (dis[2:] > 1.0)).any()
