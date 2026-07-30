#!/usr/bin/env python3
"""Wie weit darf der binäre Entscheidungshorizont der Thermolasten reichen?

Hintergrund: Ein thermischer Speicher braucht keine 15-min-Schaltentscheidungen,
deshalb löst ``ems/loads.py`` nur die zeitnahen Blöcke binär und führt fernere
als Duty-Cycle (``binary_horizon_hours``). Der Wert wurde seinerzeit gesenkt,
weil der Solver ins Zeitlimit lief und grobe Zwischenstände lieferte - sichtbar
als seltsame Sperren im Plan.

Seitdem hat sich zweierlei geändert: HiGHS ist Standard-Solver, und der
Optimierer startet warm aus der letzten Lösung. Ob die Kürzung noch nötig ist,
ist damit eine MESSFRAGE, keine Meinung. Dieses Skript beantwortet sie am echten
Fall: es nimmt den Debug-Schnappschuss eines Laufs - der genau dafür auch die
Pool-Eingänge sichert (Außentemperatur, Einstrahlung, Ist-Zustand) - und löst
ihn mehrfach mit verschiedenen Horizonten.

    python pool_horizon_bench.py --config config.yaml
    python pool_horizon_bench.py --hours 6 12 24 48 0 --repeat 3

Gemeldet werden Laufzeit, Zielwert und ob das Zeitlimit gerissen wurde. Ein
längerer Horizont lohnt nur, wenn er den Zielwert MESSBAR verbessert und dabei
sicher unter dem Zeitlimit bleibt - der Plan wird alle 15 Minuten neu gerechnet,
eine Lösung, die 200 s braucht, ist im Betrieb wertlos.
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from ems.config import load_config
from ems.optimizer import Optimizer, OptimizerInputs


def _inputs_from_snapshot(snap: dict) -> OptimizerInputs:
    """Optimierer-Eingaben aus dem Schnappschuss rekonstruieren."""
    src = snap["inputs"]
    index = pd.DatetimeIndex([pd.Timestamp(t) for t in src["index"]])

    def arr(name):
        werte = src.get(name)
        return None if werte is None else np.asarray(
            [np.nan if v is None else float(v) for v in werte], dtype="float64")

    return OptimizerInputs(
        index=index,
        house_load_w=arr("house_load_w"),
        pv_w=arr("pv_w"),
        pv10_w=arr("pv10_w"),
        price_ct_kwh=arr("price_ct_kwh"),
        spot_price_ct_kwh=arr("spot_price_ct_kwh"),
        feedin_ct_kwh=arr("feedin_ct_kwh"),
        initial_house_soc_wh=float(src["initial_house_soc_wh"]),
        initial_car_soc_wh=src.get("initial_car_soc_wh"),
        car_present=bool(src.get("car_present", False)),
        ambient_temp_c=arr("ambient_temp_c"),
        solar_w_m2=arr("solar_w_m2"),
        load_state=src.get("load_state") or {},
        load_feedback=src.get("load_feedback") or {},
    )


def _thermal_loads(config):
    return [ld for ld in (getattr(config, "controllable_loads", []) or [])
            if getattr(ld, "type", None) == "thermal" and ld.enabled]


def _measure(config, inputs, hours: float, repeat: int) -> dict:
    """Einen Horizont setzen und lösen. Rückgabe: Kennzahlen des besten Laufs.

    Warmstart und Plan-Stabilisierung sind AUS: beide würden die Messung an den
    vorherigen Lauf koppeln, und genau die Laufzeit des KALTEN Falls ist die
    Frage - im Betrieb ist der erste Lauf nach einem Neustart genau das.
    """
    for ld in _thermal_loads(config):
        ld.binary_horizon_hours = float(hours)
    zeiten, ergebnis = [], None
    for _ in range(max(1, repeat)):
        start = time.monotonic()
        ergebnis = Optimizer(config, store_warm=False,
                             stabilize_plan=False).solve(inputs)
        zeiten.append(time.monotonic() - start)
    tabelle = ergebnis.table
    heiz_spalten = [c for c in tabelle.columns
                    if c.startswith("load_") and c.endswith("_w")
                    and "_grid_" not in c]
    heiz_kwh = float(sum(tabelle[c].fillna(0).sum() for c in heiz_spalten)) * 0.25 / 1000.0
    return {
        "hours": hours,
        "seconds_min": min(zeiten),
        "seconds_max": max(zeiten),
        "status": ergebnis.status,
        "hit_limit": bool(ergebnis.solver_hit_limit),
        "cost_eur": ergebnis.total_cost_ct / 100.0,
        "heat_kwh": heiz_kwh,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--snapshot", default=None,
                   help="Debug-Schnappschuss (Vorgabe: report.snapshot_path)")
    p.add_argument("--hours", nargs="+", type=float,
                   default=[6.0, 12.0, 24.0, 48.0, 0.0],
                   help="zu messende Horizonte; 0 = alles binär")
    p.add_argument("--repeat", type=int, default=2,
                   help="Wiederholungen je Horizont (gegen Streuung)")
    p.add_argument("--start-temp", type=float, default=None,
                   help="Ist-Temperatur der Thermolasten überschreiben. Im "
                        "Sommer steht der Pool über dem Sollband und heizt in "
                        "keiner Variante - dann hat der Horizont nichts zu "
                        "entscheiden und die Messung ist aussagelos. Ein Wert "
                        "unter min_c erzwingt den fordernden Fall.")
    args = p.parse_args()

    config = load_config(args.config)
    thermisch = _thermal_loads(config)
    if not thermisch:
        print("Keine aktive thermische Last konfiguriert - nichts zu messen.")
        return 1
    pfad = args.snapshot or config.report.snapshot_path
    with open(pfad, encoding="utf-8") as fh:
        snap = json.load(fh)
    inputs = _inputs_from_snapshot(snap)
    if args.start_temp is not None:
        inputs.load_state = {ld.name: float(args.start_temp)
                             for ld in thermisch}

    grenze = float(getattr(config.optimization, "solver_time_limit_s", 0) or 0)
    aktuell = float(getattr(thermisch[0], "binary_horizon_hours", 12.0))
    print(f"Schnappschuss {snap.get('generated')} · {len(inputs.index)} Slots · "
          f"Solver {getattr(config.optimization, 'solver', '?')}")
    ist = ", ".join(f"{ld.name} {(snap['inputs'].get('load_state') or {}).get(ld.name, '?')} °C"
                    for ld in thermisch)
    print(f"Thermolasten: {ist} · Sollband "
          f"{thermisch[0].min_c:.1f}-{thermisch[0].max_c:.1f} °C · "
          f"konfiguriert {aktuell:.0f} h"
          + (f" · Zeitlimit {grenze:.0f} s" if grenze else ""))
    if args.start_temp is not None:
        print(f"Ist-Temperatur überschrieben auf {args.start_temp:.1f} °C "
              "(erzwungener Heizbedarf).")
    print()
    kopf = (f"{'Horizont':>9s} {'Zeit':>14s} {'Status':>10s} {'Limit':>6s} "
            f"{'Kosten':>10s} {'Heizen':>9s}")
    print(kopf)
    print("-" * len(kopf))

    zeilen = []
    for stunden in args.hours:
        r = _measure(config, inputs, stunden, args.repeat)
        zeilen.append(r)
        label = "alles" if stunden <= 0 else f"{stunden:.0f} h"
        print(f"{label:>9s} {r['seconds_min']:6.1f}-{r['seconds_max']:5.1f}s "
              f"{r['status']:>10s} {'JA' if r['hit_limit'] else '-':>6s} "
              f"{r['cost_eur']:9.2f}€ {r['heat_kwh']:8.1f}kWh")

    basis = next((z for z in zeilen if z["hours"] == aktuell), zeilen[0])
    print()
    if max(z["heat_kwh"] for z in zeilen) < 0.05:
        # Ohne geplanten Heizbetrieb gibt es keine Schaltentscheidungen, die der
        # Horizont beeinflussen koennte - die Laufzeiten sind dann echt, der
        # Kostenvergleich aber aussagelos. Lieber sagen als stillschweigend eine
        # Empfehlung aus einem leeren Fall ableiten.
        print("ACHTUNG: In diesem Schnappschuss heizt die Thermolast in KEINER")
        print("Variante. Der Zeitvergleich gilt, der Kostenvergleich nicht -")
        print("für eine belastbare Aussage einen Schnappschuss mit geplantem")
        print("Heizbetrieb nehmen (--snapshot, z.B. aus einer kühlen Nacht).")
        print()
    print(f"Bezug: {basis['hours']:.0f} h (aktuell konfiguriert), "
          f"{basis['cost_eur']:.2f} €, {basis['seconds_max']:.1f} s")
    for z in zeilen:
        if z is basis:
            continue
        besser = basis["cost_eur"] - z["cost_eur"]
        teurer = z["seconds_max"] - basis["seconds_max"]
        label = "alles binär" if z["hours"] <= 0 else f"{z['hours']:.0f} h"
        urteil = ("Zeitlimit gerissen" if z["hit_limit"] else
                  "lohnt" if besser > 0.01 and (not grenze or z["seconds_max"] < grenze * 0.5)
                  else "kein Gewinn" if besser <= 0.01 else "zu langsam")
        print(f"  {label:>12s}: {besser:+.2f} € bei {teurer:+.1f} s  -> {urteil}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
