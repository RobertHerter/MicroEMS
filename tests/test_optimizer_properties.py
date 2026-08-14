"""Eigenschaften der MILP-Formulierung statt einzelner Szenarien.

Die bestehenden Optimierer-Tests pruefen von Hand gebaute Faelle. Hier stehen
Aussagen, die fuer JEDES Szenario gelten muessen:

* **Invarianten** - ueber zufaellig gewuerfelte Szenarien, damit auch
  Kombinationen geprueft werden, die niemand von Hand hinschreibt.
  ``validate_plan`` haelt die Regeln (AC-Knotenbilanz, SoC-Fortschreibung,
  Mutex Laden/Entladen und Import/Export, Leistungs- und Kappungsgrenzen).
* **Kostenkonsistenz** - die Summe der Slotkosten muss die berichtete
  Gesamtsumme sein. Ein Term, der nur an einer der beiden Stellen zaehlt,
  faellt sonst nicht auf.
* **Preis-Monotonie** - wird ein Slot teurer, darf der geplante Netzbezug in
  genau diesem Slot nicht steigen. Das prueft die ZIELFUNKTION; Invarianten
  sagen nur, dass ein Plan zulaessig ist, nicht dass er der richtige ist.

Zur Reichweite: die Invarianten-Regeln stammen aus ``ems/validate.py`` und sind
damit nicht unabhaengig von der Modellformulierung - ein gemeinsamer Denkfehler
in beiden faellt hier nicht auf. Die zufaellige Abdeckung und die beiden
oekonomischen Aussagen sind der eigentliche Zugewinn.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.optimizer import Optimizer, OptimizerInputs
from ems.validate import summarize, validate_plan
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _tag(datum: str, slots: int = 96) -> pd.DatetimeIndex:
    return pd.date_range(datum, periods=slots, freq="15min", tz=TZ)


def _szenario(rng: np.random.Generator):
    """Zufaelliges, aber physikalisch plausibles Szenario."""
    datum = rng.choice(["2026-01-15", "2026-04-02", "2026-06-21", "2026-10-08"])
    idx = _tag(str(datum))
    stunde = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)

    spitze = float(rng.uniform(0.0, 11000.0))
    pv = np.clip(spitze * np.exp(-((stunde - 13.0) ** 2) / rng.uniform(4.0, 9.0)),
                 0.0, None)
    grund = float(rng.uniform(200.0, 1200.0))
    last = grund + rng.uniform(0.0, 2500.0, size=len(idx)) * (
        (stunde > 6.5) & (stunde < 22.0))

    preis = np.full(len(idx), float(rng.uniform(12.0, 45.0)))
    preis += rng.normal(0.0, rng.uniform(1.0, 12.0), size=len(idx))
    if rng.random() < 0.35:                       # negative Preise mitnehmen
        von = int(rng.integers(0, len(idx) - 12))
        preis[von:von + int(rng.integers(4, 12))] = -float(rng.uniform(0.5, 8.0))
    einsp = np.full(len(idx), float(rng.uniform(0.0, 12.0)))

    cfg = make_config()
    cfg.optimization.charge_strategy = str(
        rng.choice(["auto", "asap", "peak", "late"]))
    cfg.optimization.allow_grid_discharge = bool(rng.random() < 0.5)

    kw = {}
    if rng.random() < 0.5:                        # Fahrzeug dazu
        cfg.vehicle.enabled = True
        kw["car_present"] = True
        kw["initial_car_soc_wh"] = float(rng.uniform(
            cfg.vehicle.min_soc_wh, cfg.vehicle.capacity_wh * 0.9))

    soc = float(rng.uniform(cfg.house_battery.min_soc_wh,
                            cfg.house_battery.max_soc_wh))
    return cfg, OptimizerInputs(
        index=idx, house_load_w=last, pv_w=pv, price_ct_kwh=preis,
        feedin_ct_kwh=einsp, initial_house_soc_wh=soc, **kw)


@pytest.mark.parametrize("seed", range(8))
def test_random_scenarios_break_no_plan_invariant(seed):
    rng = np.random.default_rng(seed)
    cfg, inp = _szenario(rng)
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
    assert not res.infeasible, f"unloesbar: {res.infeasible_reason}"
    verstoesse = validate_plan(cfg, res, inp)
    fehler = [v for v in verstoesse if v.severity == "error"]
    assert not fehler, (
        f"Strategie {cfg.optimization.charge_strategy}, "
        f"Netzentladen {cfg.optimization.allow_grid_discharge}: "
        f"{summarize(verstoesse)}")


@pytest.mark.parametrize("seed", range(4))
def test_slot_costs_sum_to_the_reported_total(seed):
    """total_cost_ct und die Slotkosten muessen dasselbe Modell abrechnen."""
    rng = np.random.default_rng(100 + seed)
    cfg, inp = _szenario(rng)
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
    assert not res.infeasible
    summe = float(res.table["slot_cost_ct"].sum())
    assert summe == pytest.approx(res.total_cost_ct, abs=0.05), (
        f"Slotkosten {summe:.3f} ct vs. berichtet {res.total_cost_ct:.3f} ct")


def test_a_more_expensive_slot_never_gets_more_grid_import():
    """Aussage ueber die ZIELFUNKTION, nicht ueber Zulaessigkeit: derselbe Tag,
    nur ein Slot von 10 auf 60 ct - der Bezug dort darf nicht steigen."""
    idx = _tag("2026-01-15")
    slot = 40

    def loese(preise):
        cfg = make_config()
        inp = OptimizerInputs(
            index=idx, house_load_w=np.full(len(idx), 800.0),
            pv_w=np.zeros(len(idx)), price_ct_kwh=preise,
            feedin_ct_kwh=np.full(len(idx), 8.0),
            initial_house_soc_wh=8000.0)
        res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
        assert not res.infeasible
        return float(res.table["grid_import_w"].iloc[slot])

    billig = np.full(len(idx), 30.0)
    billig[slot] = 10.0
    teuer = billig.copy()
    teuer[slot] = 60.0

    bezug_billig, bezug_teuer = loese(billig), loese(teuer)
    assert bezug_billig > 100.0, (
        "der billige Slot muss ueberhaupt Bezug ziehen, sonst prueft der "
        f"Vergleich nichts (war {bezug_billig:.0f} W)")
    assert bezug_teuer <= bezug_billig + 1.0, (
        f"teurer Slot zieht MEHR: {bezug_teuer:.0f} W statt "
        f"hoechstens {bezug_billig:.0f} W")
