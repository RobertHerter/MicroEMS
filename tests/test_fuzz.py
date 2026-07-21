"""Property-/Fuzz-Tests und metamorphe Relationen für den Optimierer.

Fuzz: viele zufällige, aber plausible Szenarien -> auf jedem müssen ALLE
harten Invarianten (ems.validate) halten. Fängt Randfälle, bevor sie in der
Realität auftreten.

Metamorph: Relationen, die ohne bekannte "richtige" Antwort gelten müssen -
fangen Vorzeichen-/Monotoniefehler, die feste Testfälle übersehen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.optimizer import Optimizer, OptimizerInputs
from ems.validate import validate_plan
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
TOL = 1.0


def _scenario(rng, day="2026-05-15", hours=24):
    idx = pd.date_range(f"{day} 00:00", periods=hours * 4, freq="15min", tz=TZ)
    n = len(idx)
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    pv_peak = rng.uniform(0, 11000)
    pv = np.clip(pv_peak * np.exp(-((hour - rng.uniform(12, 14)) ** 2)
                                  / rng.uniform(4, 9)), 0, None)
    base = rng.uniform(200, 1200)
    load = base + rng.uniform(0, 900) * np.exp(-((hour - 19) ** 2) / 3) \
        + rng.normal(0, 60, n)
    load = np.clip(load, 50, None)
    price = (rng.uniform(15, 32)
             + rng.uniform(4, 14) * np.sin((hour - rng.uniform(0, 6)) / 24 * 2 * np.pi)
             + np.where((hour >= 17) & (hour <= 20), rng.uniform(0, 15), 0))
    if rng.random() < 0.25:            # gelegentlich Negativpreis-Mittag
        price = price - np.where((hour >= 11) & (hour <= 14), rng.uniform(20, 40), 0)
    soc = rng.uniform(0, 21000)
    return OptimizerInputs(
        index=idx, house_load_w=load.astype(float), pv_w=pv.astype(float),
        price_ct_kwh=price.astype(float), feedin_ct_kwh=np.full(n, rng.uniform(0, 12)),
        initial_house_soc_wh=soc)


@pytest.mark.parametrize("seed", range(24))
def test_fuzz_invariants_hold(seed):
    """Auf jedem zufälligen Szenario: Lösung ohne harte Invariantenverletzung."""
    rng = np.random.default_rng(seed)
    cfg = make_config()
    inp = _scenario(rng)
    res = Optimizer(cfg).solve(inp)
    errs = [v for v in validate_plan(cfg, res, inp) if v.severity == "error"]
    assert not errs, f"seed {seed}: {[str(e) for e in errs]}"


def test_metamorph_price_offset_invariant():
    """Alle Preise um denselben Betrag anheben -> physisch identischer Plan
    (nur die Kosten verschieben sich). Fängt Absolut-vs-Differenz-Fehler.

    AUSGENOMMEN: Hold- und Schalt-Malus sind bewusst FIXE Cent-Beträge auf
    vermeidbaren Netzbezug bzw. reale Eingriffe. Sie konkurrieren mit den
    Energiekosten; die strikte Offset-Invarianz gilt für sie per Design nicht
    (sie SOLLEN Mikro-Preisvorteile überstimmen). Hier auf 0 gesetzt, um die
    Invarianz des preisgetriebenen Kern-Optimierers zu prüfen; ihr Verhalten
    selbst deckt test_optimizer_limits ab."""
    cfg = make_config()
    cfg.optimization.battery_hold_penalty_ct_kwh = 0.0
    cfg.optimization.battery_switch_penalty_ct = 0.0
    rng = np.random.default_rng(1)
    inp = _scenario(rng)
    a = Optimizer(cfg).solve(inp)
    inp2 = OptimizerInputs(**{**inp.__dict__,
                              "price_ct_kwh": inp.price_ct_kwh + 10.0})
    b = Optimizer(cfg).solve(inp2)
    for col in ("batt_dc_charge_w", "batt_discharge_w", "grid_import_w",
                "grid_export_w"):
        assert np.allclose(a.table[col], b.table[col], atol=25), \
            f"{col} ändert sich bei reinem Preis-Offset"


def test_metamorph_higher_feedin_not_less_export():
    """Höhere Einspeisevergütung -> nie WENIGER Gesamteinspeisung."""
    cfg = make_config()
    rng = np.random.default_rng(2)
    inp = _scenario(rng)
    low = Optimizer(cfg).solve(OptimizerInputs(**{**inp.__dict__,
        "feedin_ct_kwh": np.full(len(inp.index), 5.0)}))
    high = Optimizer(cfg).solve(OptimizerInputs(**{**inp.__dict__,
        "feedin_ct_kwh": np.full(len(inp.index), 25.0)}))
    assert high.table["grid_export_w"].sum() >= low.table["grid_export_w"].sum() - 50


def test_metamorph_more_pv_fills_battery_no_later():
    """Mehr PV -> Akku ist nie SPÄTER voll (erreicht Ziel min. so früh)."""
    cfg = make_config()
    cfg.optimization.charge_strategy = "asap"
    rng = np.random.default_rng(3)
    inp = _scenario(rng)
    inp = OptimizerInputs(**{**inp.__dict__, "initial_house_soc_wh": 2000.0})
    base = Optimizer(cfg).solve(inp)
    more = Optimizer(cfg).solve(OptimizerInputs(**{**inp.__dict__,
        "pv_w": inp.pv_w * 1.5}))

    def first_full(t):
        full = t.index[t["house_soc_percent"] >= 99.0]
        return full[0] if len(full) else t.index[-1]
    assert first_full(more.table) <= first_full(base.table)
