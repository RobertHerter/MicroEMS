"""Tests der Entscheidungsgüte-Bewertung (ems/planvalue.py) und der
Infeasibility, die sie aufgedeckt hat.

Alle Tests arbeiten auf synthetischen Daten - keine Live-DB, kein RSCP.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.optimizer import Optimizer, OptimizerInputs
from ems.planvalue import (_best_allocation, _metered_cost, simulate_schedule,
                           timing_quality)
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _frame(n=96, start="2026-01-15 00:00"):
    """Ein Tag mit klarem Preisgefaelle: teurer Abend, guenstige Nacht."""
    idx = pd.date_range(pd.Timestamp(start, tz=TZ), periods=n, freq="15min")
    price = np.full(n, 20.0)
    price[68:80] = 45.0                 # 17:00-20:00 teuer
    price[8:20] = 8.0                   # 02:00-05:00 guenstig
    return idx, price


def _data(idx, price, pv, house, battery, grid, soc, dt=0.25):
    return {"index": idx, "start": idx[0], "end": idx[-1], "dt_hours": dt,
            "pv_w": pd.Series(pv, index=idx),
            "house_w": pd.Series(house, index=idx),
            "battery_w": pd.Series(battery, index=idx),
            "grid_w": pd.Series(grid, index=idx),
            "soc_percent": pd.Series(soc, index=idx),
            "price_ct_kwh": pd.Series(price, index=idx),
            "feedin_ct_kwh": pd.Series(np.full(len(idx), 8.0), index=idx)}


# --------------------------------------------------------------------------- #
# Bestmarke
# --------------------------------------------------------------------------- #
def test_best_allocation_fills_the_most_expensive_slots_first():
    price = np.array([10.0, 50.0, 30.0, 20.0])
    cap = np.full(4, 1.0)                       # je Slot max 1 kWh
    head = np.full(4, 99.0)                     # keine kumulative Schranke
    best = _best_allocation(price, cap, 2.0, head, cheapest=False)
    assert best == pytest.approx(40.0)          # 50 + 30 auf 2 kWh
    cheap = _best_allocation(price, cap, 2.0, head, cheapest=True)
    assert cheap == pytest.approx(15.0)         # 10 + 20


def test_best_allocation_respects_cumulative_availability():
    # Der teuerste Slot liegt vorn, ist aber erst spaeter verfuegbar.
    price = np.array([50.0, 10.0, 10.0, 10.0])
    cap = np.full(4, 1.0)
    head = np.array([0.0, 0.0, 1.0, 1.0])       # vor Slot 2 nichts verfuegbar
    best = _best_allocation(price, cap, 1.0, head, cheapest=False)
    assert best == pytest.approx(10.0)          # der 50er ist nicht erreichbar


# --------------------------------------------------------------------------- #
# Timing-Guete
# --------------------------------------------------------------------------- #
def test_timing_quality_rates_perfect_discharge_timing_at_100_percent():
    idx, price = _frame()
    n = len(idx)
    house = np.full(n, 2000.0)
    pv = np.zeros(n)
    battery = np.zeros(n)
    battery[68:80] = -2000.0                    # genau im teuren Fenster
    grid = house + battery
    soc = np.clip(100.0 - np.cumsum(np.maximum(0.0, -battery)) * 0.25 / 100.0,
                  10.0, 100.0)
    out = timing_quality(make_config(), "2026-01-15",
                         data=_data(idx, price, pv, house, battery, grid, soc))
    assert out["discharge"]["score_percent"] == pytest.approx(100.0, abs=0.5)
    assert out["discharge"]["meaningful"] is True


def test_timing_quality_penalises_discharge_in_cheap_slots():
    idx, price = _frame()
    n = len(idx)
    house = np.full(n, 2000.0)
    battery = np.zeros(n)
    battery[8:20] = -2000.0                     # in den guenstigsten Slots
    grid = house + battery
    soc = np.full(n, 80.0)
    out = timing_quality(make_config(), "2026-01-15",
                         data=_data(idx, price, np.zeros(n), house, battery,
                                    grid, soc))
    # Real 8 ct gegen bestmoegliche 45 ct -> deutlich unter 100 %.
    assert out["discharge"]["score_percent"] < 30.0
    assert out["discharge"]["meaningful"] is True


def test_timing_quality_marks_missing_freedom_as_not_meaningful():
    """Deckt der Akku exakt die Restlast jedes Slots, gab es keine Wahl."""
    idx, price = _frame()
    n = len(idx)
    house = np.full(n, 300.0)
    battery = np.full(n, -300.0)                # jede Restlast sofort gedeckt
    grid = np.zeros(n)
    out = timing_quality(make_config(), "2026-01-15",
                         data=_data(idx, price, np.zeros(n), house, battery,
                                    grid, np.full(n, 60.0)))
    assert out["discharge"]["meaningful"] is False
    assert out["discharge"]["slack_kwh"] < 1.0


# --------------------------------------------------------------------------- #
# Fixplan-Simulator
# --------------------------------------------------------------------------- #
def test_simulator_matches_metered_cost_for_a_consistent_day():
    """Auf widerspruchsfreien Ist-Daten muss der Simulator die Abrechnung
    reproduzieren - sonst sind Regret-Differenzen nicht interpretierbar."""
    idx, price = _frame()
    n = len(idx)
    house = np.full(n, 1200.0)
    pv = np.zeros(n)
    pv[32:64] = 4000.0
    battery = np.zeros(n)
    battery[32:40] = 2000.0                     # laden aus PV
    battery[68:80] = -1200.0                    # entladen am Abend
    grid = house - pv + battery
    soc = np.full(n, 50.0)
    data = _data(idx, price, pv, house, battery, grid, soc)
    # Der Fahrplan muss in Kapazitaet und SoC passen, sonst kappt der Simulator
    # (zu Recht) und die Fluesse weichen von den Ist-Werten ab.
    sim = simulate_schedule(make_config(), data, battery, soc0_wh=2000.0)
    met = _metered_cost(data)
    assert sim["cost_eur"] == pytest.approx(met["cost_eur"], abs=0.01)


def test_simulator_clips_discharge_at_the_minimum_soc():
    idx, price = _frame(n=8)
    n = len(idx)
    cfg = make_config()
    house = np.full(n, 5000.0)
    battery = np.full(n, -5000.0)               # will mehr entladen als drin ist
    data = _data(idx, price, np.zeros(n), house, battery, house + battery,
                 np.full(n, 20.0))
    sim = simulate_schedule(cfg, data, battery,
                            soc0_wh=cfg.house_battery.min_soc_wh + 500.0)
    assert sim["end_soc_percent"] >= cfg.house_battery.min_soc_percent - 0.1
    assert sim["battery_discharge_kwh"] <= 0.6   # nur die 500 Wh (mit Verlust)
    assert sim["grid_import_kwh"] > 9.0          # der Rest kommt aus dem Netz


# --------------------------------------------------------------------------- #
# Regression: Mindest-Entladeleistung darf den Plan nicht unloesbar machen
# --------------------------------------------------------------------------- #
def test_tiny_residual_load_stays_feasible_with_minimum_discharge():
    """Eine Restlast unter min_discharge_w darf nicht infeasible werden.

    Der Suffizienz-Block (allow_grid_discharge=False + Halte-Malus) verbietet
    Netzbezug, solange der Akku Energie hat. Kann der Akku die Restlast wegen
    der Mindest-Entladeleistung gar nicht bedienen (dis = 0 oder >= min_dis),
    entsteht ohne die Ausnahme ein Widerspruch - und der GESAMTE Tagesplan
    fällt aus. Trat mit echten Ist-Daten auf (Restlast 53 W bei min_dis 100 W).
    """
    idx = pd.date_range(pd.Timestamp("2026-07-26 00:00", tz=TZ),
                        periods=48, freq="15min")
    n = len(idx)
    cfg = make_config()
    cfg.optimization.min_discharge_w = 100.0
    cfg.optimization.allow_grid_discharge = False
    cfg.optimization.battery_hold_penalty_ct_kwh = 5.0
    cfg.controllable_loads = []

    house = np.full(n, 800.0)
    pv = np.zeros(n)
    # Durchgehende Restlastphase, in der Mitte drei Slots mit winziger Restlast.
    house[20:23] = 800.0
    pv[20:23] = 800.0 - np.array([53.0, 73.0, 74.0])
    price = np.full(n, 25.0)
    inp = OptimizerInputs(
        index=idx, house_load_w=house, pv_w=pv, price_ct_kwh=price,
        feedin_ct_kwh=np.full(n, 8.0),
        initial_house_soc_wh=cfg.house_battery.max_soc_wh,
        spot_price_ct_kwh=price)
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
    assert res.status == "Optimal", res.status
    imp = res.table["grid_import_w"].to_numpy(float)
    # Die Ausnahme gilt genau in Hoehe der nicht bedienbaren Restlast ...
    assert imp[20] == pytest.approx(53.0, abs=1.0)
    assert imp[21] == pytest.approx(73.0, abs=1.0)
    # ... und lockert den Bezug in der uebrigen Restlastphase nicht auf.
    assert imp[:20].max() <= 5.0
    # Ab ca. Slot 40 ist der Akku leer, danach ist Bezug unvermeidbar.
    assert imp[23:38].max() <= 5.0
