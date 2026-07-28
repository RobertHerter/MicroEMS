"""Entscheidungsgüte der Planung bewerten: Timing und Prognose-Regret.

Beantwortet die Frage, ob die Prognose zu Tagesbeginn gut genug war, um die
besten Zeitpunkte für Entladen und Netzladen zu treffen - im Gegensatz zu den
Delta-Anzeigen im Dashboard, die alle 15 min neu gegen die aktuelle Prognose
rechnen und deshalb nur den Moment bewerten.

Zwei unabhängige Sichten (Details in ems/planvalue.py):

  Timing-Güte     Lagen die real entladenen kWh in den teuersten und die
                  netzgeladenen in den günstigsten Slots? Verglichen mit der
                  bestmöglichen Verteilung DERSELBEN Energiemenge unter den
                  echten Schranken (Restlast, Ladeleistung, SoC-Verlauf).
                  100 % = perfektes Timing. Rein aus Ist-Daten, kein Solver.

  Regret          Vier Kosten desselben Tages, alle mit demselben Simulator auf
                  den Ist-Daten bewertet und auf den Ist-Endladestand normiert:
                    hellsicht   Optimierer kannte die eingetretenen Werte
                    fix         Plan aus der 00:00-Prognose, nie angepasst
                    rollierend  die je Slot publizierten Sollwerte
                    abgerechnet aus den Ist-Zählerflüssen
                  Daraus die Dreiteilung: was die Prognose gekostet hat, was
                  das 15-min-Nachplanen davon zurückgeholt hat, und was an
                  Ausführung/Messung übrig bleibt.

Aufruf:
    python plan_value.py --config config.yaml --days 7      # Tabelle
    python plan_value.py --config config.yaml --day 2026-07-25
    python plan_value.py --config config.yaml --days 7 --timing-only
    python plan_value.py --config config.yaml --day 2026-07-25 --json

Nur lesend (lokale SQLite + Optimierer im Speicher); es wird nichts geschrieben
und kein RSCP-Aufruf gemacht. Der Regret braucht je Tag zwei Solverläufe über
48 h, rechnet also einige Sekunden pro Tag.
"""
from __future__ import annotations

import argparse
import json
import logging

import pandas as pd

from ems.config import load_config
from ems.planvalue import regret_analysis, timing_quality


def _days(config, args) -> list[str]:
    if args.day:
        return [args.day]
    today = pd.Timestamp.now(tz=config.general.timezone).normalize()
    # Der jeweils letzte Tag braucht den Folgetag als Solver-Horizont, deshalb
    # endet die Liste beim Vortag.
    return [str((today - pd.Timedelta(days=k)).date())
            for k in range(args.days, 0, -1)]


def _fmt_pct(value) -> str:
    return "    -" if value is None else f"{value:4.0f}%"


def _print_timing(rows: list[dict]) -> None:
    print("Timing-Güte (100 % = Energie lag in den bestmöglichen Slots)\n")
    print(f"{'Tag':11s} {'entladen':>9s} {'Güte':>5s} {'Spielraum':>10s}   "
          f"{'netzgeladen':>12s} {'Güte':>5s} {'Spielraum':>10s}")
    print("-" * 74)
    for r in rows:
        if r.get("reason"):
            print(f"{r['day']:11s} {r['reason']}")
            continue
        d, g = r["discharge"], r["grid_charge"]

        def cell(part):
            if not part.get("meaningful"):
                # Ohne Freiheitsgrade ist die Güte trivial ~100 % und sagt
                # nichts aus - der Spielraum macht das transparent.
                return f"{'(fix)':>5s} {part['slack_kwh']:7.2f}kWh"
            return f"{_fmt_pct(part['score_percent'])} {part['slack_kwh']:7.2f}kWh"

        print(f"{r['day']:11s} {d['energy_kwh']:6.2f}kWh {cell(d)}   "
              f"{g['energy_kwh']:9.2f}kWh {cell(g)}")
    print("\n'Spielraum' = wie viel Energie überhaupt anders hätte liegen "
          "können.\n'(fix)' = keine Freiheitsgrade, die Güte wäre trivial.")


def _print_regret(rows: list[dict]) -> None:
    print("\nRegret gegen Hellsicht (€, negativ = Erlös; alle auf den "
          "Ist-Endladestand normiert)\n")
    print(f"{'Tag':11s} {'hellsicht':>10s} {'fix':>8s} {'rollierend':>11s} "
          f"{'abgerechnet':>12s} | {'Prognose':>9s} {'Nachplanen':>11s} "
          f"{'Ausführung':>11s} {'Rest':>7s}")
    print("-" * 106)
    tot = {"forecast_regret_eur": 0.0, "replanning_gain_eur": 0.0,
           "execution_and_metering_eur": 0.0, "total_gap_eur": 0.0}
    counted = 0
    for r in rows:
        if r.get("reason"):
            print(f"{r['day']:11s} {r['reason']}")
            continue

        def c(key):
            item = r.get(key)
            return ("       -" if not item
                    else f"{item['cost_adjusted_eur']:8.2f}")

        d = r.get("delta", {})

        def dv(key, width):
            v = d.get(key)
            return f"{'-':>{width}s}" if v is None else f"{v:{width}.2f}"

        print(f"{r['day']:11s} {c('oracle'):>10s} {c('frozen'):>8s} "
              f"{c('rolling'):>11s} {c('metered'):>12s} | "
              f"{dv('forecast_regret_eur', 9)} {dv('replanning_gain_eur', 11)} "
              f"{dv('execution_and_metering_eur', 11)} "
              f"{dv('total_gap_eur', 7)}")
        if all(d.get(k) is not None for k in tot):
            counted += 1
            for k in tot:
                tot[k] += float(d[k])
    if counted > 1:
        print("-" * 106)
        print(f"{'Summe':11s} {'':>10s} {'':>8s} {'':>11s} {'':>12s} | "
              f"{tot['forecast_regret_eur']:9.2f} "
              f"{tot['replanning_gain_eur']:11.2f} "
              f"{tot['execution_and_metering_eur']:11.2f} "
              f"{tot['total_gap_eur']:7.2f}")
    print("\nPrognose   = was die 00:00-Prognose gegenüber Hellsicht kostet\n"
          "Nachplanen = was das 15-min-Neurechnen davon zurückholt (>0 = Gewinn)\n"
          "Ausführung = Abweichung Anlage/Messung gegenüber den Sollwerten\n"
          "Rest       = abgerechnet minus hellsicht (Prognose - Nachplanen "
          "+ Ausführung)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--day", help="einzelner Tag JJJJ-MM-TT")
    p.add_argument("--days", type=int, default=7,
                   help="Anzahl abgeschlossener Tage bis zum Vortag (Standard 7)")
    p.add_argument("--timing-only", action="store_true",
                   help="nur die Timing-Güte (ohne Solverläufe, schnell)")
    p.add_argument("--regret-only", action="store_true",
                   help="nur die Regret-Analyse")
    p.add_argument("--json", action="store_true", help="Rohdaten als JSON")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s")

    config = load_config(args.config)
    days = _days(config, args)
    out = {"days": days}
    if not args.regret_only:
        out["timing"] = [timing_quality(config, d)
                         or {"day": d, "reason": "keine ausreichenden Ist-Daten"}
                         for d in days]
    if not args.timing_only:
        out["regret"] = [regret_analysis(config, d)
                         or {"day": d, "reason": "keine ausreichenden Ist-Daten"}
                         for d in days]

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    if "timing" in out:
        _print_timing(out["timing"])
    if "regret" in out:
        _print_regret(out["regret"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
