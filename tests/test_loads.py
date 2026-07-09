"""Tests der steuerbaren Lasten (controllable_loads): deferrable + thermal."""
from __future__ import annotations

import numpy as np

from ems.config import ControllableLoad, LoadStage
from ems.optimizer import Optimizer
from tests.test_optimizer import _day_index, _inputs
from tests.test_synthetic import make_config

DT_H = 0.25


def test_deferrable_runs_required_time_in_cheap_slots():
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="waschen", type="deferrable", power_w=1000.0, runtime_minutes=60.0,
        switch_penalty_ct=0.0)]
    idx = _day_index("2026-01-15")
    n = len(idx)
    price = np.full(n, 40.0)
    price[:16] = 5.0                       # erste 4 h billig
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_waschen_w"]
    energy = float(w.sum()) * DT_H
    assert abs(energy - 1000.0) < 60.0, f"Laufzeit-Energie {energy:.0f} Wh != ~1000"
    cheap = float(w.iloc[:16].sum()); rest = float(w.iloc[16:].sum())
    assert cheap > rest, "Last läuft nicht bevorzugt in den günstigen Slots"


def test_deferrable_respects_window():
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="pumpe", type="deferrable", power_w=800.0, runtime_minutes=120.0,
        window_from_hour=10, window_to_hour=14, switch_penalty_ct=0.0)]
    idx = _day_index("2026-06-10")
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=30.0,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_pumpe_w"]
    hours = w.index.tz_convert("Europe/Berlin").hour
    assert float(w[(hours < 10) | (hours >= 14)].sum()) < 1.0, "Last läuft außerhalb des Fensters"
    assert float(w.sum()) * DT_H > 500.0, "Last läuft gar nicht"


def _pool_load(loss=100.0, min_c=27.0, target=28.0):
    return ControllableLoad(
        name="pool", type="thermal", volume_l=7000.0, target_c=target,
        min_c=min_c, max_c=29.0, loss_w_per_k=loss, switch_penalty_ct=0.0,
        stages=[LoadStage(name="klein", power_w=400.0, heat_w=3000.0),
                LoadStage(name="gross", power_w=650.0, heat_w=4000.0,
                          requires="klein")])


def test_thermal_holds_band_and_coupling():
    cfg = make_config()
    cfg.controllable_loads = [_pool_load()]
    idx = _day_index("2026-06-10")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 20.0), load_state={"pool": 27.0}))
    assert not res.infeasible
    temp = res.table["load_pool_temp_c"]
    klein = res.table["load_pool_klein_w"]
    gross = res.table["load_pool_gross_w"]
    # Komfort: Band im Wesentlichen gehalten
    assert temp.min() > 27.0 - 0.3, f"Band verletzt (min {temp.min():.2f} °C)"
    assert temp.max() <= 29.0 + 0.05
    # es wird geheizt (Verlust muss ersetzt werden)
    assert float(klein.sum()) * DT_H > 0.0
    # Kopplung: große WP nur, wenn kleine läuft
    assert ((gross <= 1.0) | (klein > 1.0)).all(), "große WP läuft ohne kleine"


def test_thermal_recovers_from_low_start():
    """Startet der Pool unter dem Band, wird kräftig (auch mit großer WP) geheizt."""
    cfg = make_config()
    cfg.controllable_loads = [_pool_load(loss=150.0)]
    idx = _day_index("2026-06-10")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 18.0), load_state={"pool": 25.0}))
    assert not res.infeasible
    temp = res.table["load_pool_temp_c"]
    gross = res.table["load_pool_gross_w"]
    klein = res.table["load_pool_klein_w"]
    assert temp.iloc[-1] > temp.iloc[0], "Pool erholt sich nicht"
    assert ((gross <= 1.0) | (klein > 1.0)).all()      # Kopplung auch hier


def test_deferrable_profile_cycle_runs_once_in_cheap_slots():
    """15-min-Kurve: der ganze Zyklus wird einmal gestartet, bevorzugt günstig."""
    cfg = make_config()
    prof = [2000.0, 500.0, 500.0, 2000.0]           # 4×15 min = 1 h
    cfg.controllable_loads = [ControllableLoad(
        name="wm", type="deferrable", power_profile_w=prof, runtime_minutes=60.0,
        switch_penalty_ct=0.0)]
    idx = _day_index("2026-01-15")
    n = len(idx)
    price = np.full(n, 40.0)
    price[:8] = 5.0
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_wm_w"]
    energy = float(w.sum()) * DT_H
    assert abs(energy - sum(prof) * DT_H) < 1.0, "kein vollständiger Zyklus"
    assert float(w.iloc[:8].sum()) > float(w.iloc[8:].sum()), "Zyklus nicht in günstigen Slots"


def test_load_mqtt_map_shape():
    """load_mqtt_map liefert Dicts mit label/column/topic für die Ausgabe."""
    cfg = make_config()
    cfg.controllable_loads = [_pool_load()]
    idx = _day_index("2026-06-10")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 20.0), load_state={"pool": 27.0}))
    labels = {e["label"] for e in res.load_mqtt_map}
    assert {"pool/klein", "pool/gross"} <= labels
    for e in res.load_mqtt_map:
        assert e["column"] in res.table.columns


def test_empty_loads_is_noop():
    cfg = make_config()
    idx = _day_index("2026-06-10")
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=500.0, price=30.0, soc=5000.0))
    assert not res.infeasible
    assert not any(c.startswith("load_") for c in res.table.columns)
