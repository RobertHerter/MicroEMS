"""Backtest: spielt vergangene Tage aus der InfluxDB durch den Optimierer und
prüft jeden Plan gegen den Invarianten-Katalog (ems.validate).

Idee: Modellfehler finden, ohne monatelang zuzuschauen - die Monate liegen
schon in der DB. Für jeden Tag im Zeitraum wird ein 48-h-Plan gerechnet und
validiert. Als Eingang dienen die TATSÄCHLICHEN Verläufe (perfekte Voraussicht)
- so werden Modell-/Optimiererfehler von Prognosefehlern getrennt.

Aufruf:
    python backtest.py --config config.yaml --days 120
    python backtest.py --config config.yaml --start 2026-01-01 --end 2026-03-01
    python backtest.py --config config.yaml --days 60 --errors-only

Es wird NICHTS in die Datenbank geschrieben und der Dienst nicht berührt.
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import timedelta

import numpy as np
import pandas as pd

from ems.config import load_config
from ems.influx import InfluxRepository
from ems.optimizer import Optimizer, OptimizerInputs, natural_battery_step
from ems.validate import validate_plan

log = logging.getLogger("ems.backtest")

REQUIRED = ("house_consumption", "pv_generation", "electricity_price", "battery_soc")


def _day_plan(repo, config, day_start):
    """Ein 48-h-Plan ab day_start aus Ist-Daten (perfekte Voraussicht)."""
    tz = config.general.timezone
    freq = f"{config.general.slot_minutes}min"
    opt_end = (day_start + timedelta(hours=config.general.optimization_horizon_hours)).normalize()
    if opt_end <= day_start:
        opt_end += timedelta(days=1)
    idx = pd.date_range(day_start, opt_end, freq=freq, tz=tz, inclusive="left")

    load = repo.read_slots("house_consumption", day_start, opt_end).reindex(idx).ffill().bfill()
    pv = repo.read_slots("pv_generation", day_start, opt_end).reindex(idx).ffill().bfill()
    price = repo.read_slots("electricity_price", day_start, opt_end).reindex(idx).ffill().bfill()
    if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
        feedin = repo.read_slots("feed_in_tariff", day_start, opt_end).reindex(idx)
        feedin = feedin.ffill().bfill().fillna(config.feed_in.fixed_ct_kwh)
    else:
        feedin = pd.Series(config.feed_in.fixed_ct_kwh, index=idx)
    if config.feed_in.zero_at_negative_price:
        feedin = feedin.where(price >= 0.0, 0.0)

    soc0 = repo.read_scalar_latest("battery_soc", day_start - timedelta(hours=6), day_start)
    if soc0 is None:
        soc0 = config.house_battery.min_soc_percent
    # Genug echte Daten vorhanden?
    if load.isna().all() or pv.isna().all() or price.isna().all():
        return None
    inp = OptimizerInputs(
        index=idx, house_load_w=load.values.astype(float),
        pv_w=pv.values.astype(float), price_ct_kwh=price.values.astype(float),
        feedin_ct_kwh=feedin.values.astype(float),
        initial_house_soc_wh=soc0 / 100.0 * config.house_battery.capacity_wh,
    )
    return inp, Optimizer(config).solve(inp)


def _baseline_cost(config, inp):
    """Netto-Kosten (ct) UND End-SoC (Wh) des naiven Eigenverbrauchsverhaltens."""
    hb = config.house_battery
    dt = config.general.dt_hours
    kwh = dt / 1000.0
    soc = min(hb.max_soc_wh, max(hb.min_soc_wh, inp.initial_house_soc_wh))
    cost = 0.0
    for i in range(len(inp.index)):
        soc, _c, _d, imp, exp = natural_battery_step(
            soc, inp.pv_w[i], inp.house_load_w[i], hb, dt,
            max_export_w=config.inverter.max_export_w)
        cost += (imp * inp.price_ct_kwh[i] - exp * inp.feedin_ct_kwh[i]) * kwh
    return cost, soc


def main() -> int:
    ap = argparse.ArgumentParser(description="EMS Backtest / Invarianten-Sweep")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=120, help="Tage rückwärts ab heute")
    ap.add_argument("--start", help="Startdatum YYYY-MM-DD (überschreibt --days)")
    ap.add_argument("--end", help="Enddatum YYYY-MM-DD")
    ap.add_argument("--errors-only", action="store_true", help="nur Fehler zeigen")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    tz = config.general.timezone
    today = pd.Timestamp.now(tz=tz).normalize()
    if args.start:
        start = pd.Timestamp(args.start, tz=tz)
        end = pd.Timestamp(args.end, tz=tz) if args.end else today
    else:
        end = today
        start = end - timedelta(days=args.days)
    days = pd.date_range(start, end, freq="D", tz=tz)

    repo = InfluxRepository(config)
    missing = [s for s in REQUIRED if not repo.signal_available(s)]
    if missing:
        print(f"FEHLER: Signale fehlen in der Config: {missing}")
        return 2

    print(f"Backtest {start.date()} .. {end.date()}  ({len(days)} Tage), "
          f"perfekte Voraussicht, 48-h-Horizont\n")
    rule_counter: Counter = Counter()
    flagged_days, n_ok, n_skip = [], 0, 0
    savings = []

    for day in days:
        try:
            res = _day_plan(repo, config, day)
        except Exception as exc:
            print(f"  {day.date()}  ÜBERSPRUNGEN ({exc})")
            n_skip += 1
            continue
        if res is None:
            n_skip += 1
            continue
        inp, result = res
        viols = validate_plan(config, result, inp)
        errs = [x for x in viols if x.severity == "error"]
        warns = [x for x in viols if x.severity == "warning"]
        show = errs if args.errors_only else viols
        for x in viols:
            rule_counter[(x.severity, x.rule)] += 1
        # Ersparnis dieses Tages, TERMINALWERT-BEREINIGT (ehrlich): Roh-Kosten
        # bestrafen sonst gespeicherte Energie am Horizontende. term = Mittel-
        # preis * Entlade-Wirkungsgrad (wie terminal_soc_value "auto").
        base_cost, base_soc = _baseline_cost(config, inp)
        term = float(np.mean(inp.price_ct_kwh)) * config.house_battery.discharge_efficiency
        plan_end = float(result.table["house_soc_wh"].iloc[-1])
        plan_adj = result.total_cost_ct - term * plan_end / 1000.0
        base_adj = base_cost - term * base_soc / 1000.0
        savings.append((base_adj - plan_adj) / 100.0)
        if show:
            flagged_days.append(day)
            print(f"  {day.strftime('%a %d.%m.%Y')}  "
                  f"({len(errs)} Fehler, {len(warns)} Warn.)")
            for x in show:
                print(f"       {x}")
        else:
            n_ok += 1

    print("\n" + "=" * 64)
    print(f"Geprüft: {len(days) - n_skip} Tage  |  sauber: {n_ok}  |  "
          f"auffällig: {len(flagged_days)}  |  übersprungen: {n_skip}")
    if rule_counter:
        print("\nVerstöße nach Regel (Tage):")
        for (sev, rule), n in sorted(rule_counter.items(),
                                     key=lambda kv: (-kv[1], kv[0][1])):
            tag = "✗" if sev == "error" else "⚠"
            print(f"  {tag} {rule:34s} {n:4d}")
    if savings:
        arr = np.array(savings)
        print(f"\nErsparnis/Tag vs. Ohne-EMS-Baseline (terminalwert-bereinigt, "
              f"perfekte Voraussicht):")
        print(f"  Median {np.median(arr):+.2f} €  min {arr.min():+.2f}  "
              f"max {arr.max():+.2f}  | negative Tage: {(arr < -0.01).sum()}")
        if (arr < -0.01).any():
            print("  ⚠ negative Tage = Plan schlechter als naives Verhalten "
                  "trotz perfekter Voraussicht -> Modell prüfen!")
    repo.close()
    return 1 if any(s == "error" for s, _ in rule_counter) else 0


if __name__ == "__main__":
    raise SystemExit(main())
