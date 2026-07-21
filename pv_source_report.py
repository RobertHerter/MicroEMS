"""PV-Prognosequellen-Report: pvlib vs. Solcast gegen die realen Ertragsdaten.

Vergleicht - rolling-origin aus dem Snapshot-Archiv (Fallback: Live-Cache) - die
kombinierte pvlib- und Solcast-Prognose gegen die Ist-PV (actuals.pv_w) und gibt
je Quelle WAPE/MAE/Bias sowie eine Umschalt-Empfehlung aus. Zusätzlich wird das
pvlib-p10/p90-Band aus echten Residuen kalibriert (statt des heuristischen
Festwerts pv_model.p10_uncertainty/p90_uncertainty).

Aufruf:
    python pv_source_report.py --config config.yaml --lookback-days 30
    python pv_source_report.py --config config.yaml --lead-hours 12   # strenger

Es wird NICHTS geschrieben und der Dienst nicht berührt. Die empfohlenen
Bandwerte trägt man bei Bedarf selbst in config.yaml (pv_model) ein.
"""
from __future__ import annotations

import argparse
import math

from ems.config import load_config
from ems import pv_eval


def _fmt(v, unit="", nd=1):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "  n/a"
    return f"{v:.{nd}f}{unit}"


def _print_group(name, g):
    if g is None:
        print(f"  {name:8s}: keine Quelle konfiguriert")
        return
    if g["n"] < 8 or math.isnan(g["wape_pct"]):
        print(f"  {name:8s}: zu wenig Datenpaare (n={g['n']}, Quelle "
              f"{g['method']}) - sammelt noch")
        return
    print(f"  {name:8s}: WAPE {_fmt(g['wape_pct'], ' %', 2):>9s}   "
          f"MAE {_fmt(g['mae_w'], ' W', 0):>8s}   "
          f"Bias {_fmt(g['bias_w'], ' W', 0):>8s}   "
          f"(n={g['n']}, {g['method']}, Ist {_fmt(g.get('actual_kwh'), ' kWh')})")


def main() -> int:
    ap = argparse.ArgumentParser(description="EMS PV-Quellen-Report (pvlib vs. Solcast)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--lookback-days", type=int, default=30,
                    help="Vergleichsfenster für WAPE/MAE (Tage rückwärts)")
    ap.add_argument("--band-lookback-days", type=int, default=60,
                    help="Fenster für die p10/p90-Bandkalibrierung")
    ap.add_argument("--lead-hours", type=float, default=0.0,
                    help="Mindest-Vorlaufzeit je Prognose (0 = frischester Wert)")
    ap.add_argument("--min-pv-w", type=float, default=50.0,
                    help="Erzeugungsschwelle: Nachtslots ausblenden")
    args = ap.parse_args()

    config = load_config(args.config)

    cmp = pv_eval.compare_sources(
        config, lookback_days=args.lookback_days, min_pv_w=args.min_pv_w,
        lead_hours=args.lead_hours)
    print(f"PV-Quellenvergleich {cmp['start'].strftime('%d.%m.%Y')} - "
          f"{cmp['end'].strftime('%d.%m.%Y')}  "
          f"(Lead {args.lead_hours:g} h, Erzeugung > {args.min_pv_w:g} W)")
    _print_group("pvlib", cmp["groups"].get("pvlib"))
    _print_group("Solcast", cmp["groups"].get("solcast"))

    methods = {name: g["method"] for name, g in cmp["groups"].items()
               if g and g.get("n", 0) >= 8}
    rec = cmp["recommendation"]
    if rec:
        if rec["meaningful"]:
            print(f"\n  -> {rec['better']} liegt {rec['wape_delta_pct']:.2f} "
                  f"Prozentpunkte WAPE vorn -> Umschalten erwägen.")
        else:
            print(f"\n  -> praktisch gleichauf (Δ {rec['wape_delta_pct']:.2f} "
                  f"Prozentpunkte WAPE) - kein klarer Vorteil.")
    else:
        print("\n  -> Empfehlung erst möglich, wenn beide Quellen genug "
              "Vergleichsdaten haben.")

    if len(set(methods.values())) > 1:
        print("  ACHTUNG: Quellen mit unterschiedlicher Datenbasis verglichen "
              f"({', '.join(f'{k}={v}' for k, v in methods.items())}). Der "
              "Cache rechnet Vergangenheit mit (nahezu) realem Wetter nach und "
              "zeigt die Prognosegüte zu optimistisch - nicht direkt vergleichbar.")
    elif set(methods.values()) == {"cache"}:
        print("  Hinweis: rein Cache-basiert (kein Archiv) - Werte tendenziell "
              "optimistisch. Wird belastbar, sobald das Snapshot-Archiv füllt.")

    band = pv_eval.calibrate_band(
        config, lookback_days=args.band_lookback_days, min_pv_w=100.0,
        lead_hours=args.lead_hours)
    if band is None:
        print("\npvlib-Band: kein pv_model konfiguriert.")
        return 0
    print(f"\npvlib-p10/p90-Band (aus {band['n']} Residuen, {band['method']}):")
    if band.get("insufficient"):
        print(f"  zu wenig Daten (n={band['n']}) - aktueller Festwert bleibt: "
              f"p10={band['current_p10_uncertainty']}, "
              f"p90={band['current_p90_uncertainty']}")
        return 0
    print(f"  aktuell:    p10_uncertainty={band['current_p10_uncertainty']}, "
          f"p90_uncertainty={band['current_p90_uncertainty']}")
    print(f"  empfohlen:  p10_uncertainty={band['recommended_p10_uncertainty']}, "
          f"p90_uncertainty={band['recommended_p90_uncertainty']}")
    print(f"  Abdeckung heute: {band['current_below_p10_pct']:.1f} % der Ist-"
          f"Werte unter p10 (Ziel {band['target_low_pct']:.0f} %), "
          f"{band['current_above_p90_pct']:.1f} % über p90 "
          f"(Ziel {100 - band['target_high_pct']:.0f} %).")
    print("\nAnwenden (optional): pv_model.p10_uncertainty / .p90_uncertainty in "
          "config.yaml auf die empfohlenen Werte setzen, dann Dienst neu starten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
