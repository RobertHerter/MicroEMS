"""Backtest: spielt vergangene Tage aus der InfluxDB durch den Optimierer und
prüft jeden Plan gegen den Invarianten-Katalog (ems.validate).

Idee: Modellfehler finden, ohne monatelang zuzuschauen - die Monate liegen
schon in der DB. Für jeden Tag im Zeitraum wird ein 48-h-Plan gerechnet und
validiert. Standardmäßig dienen die TATSÄCHLICHEN Verläufe (perfekte
Voraussicht) als Eingang. Mit ``--historical-forecasts`` werden stattdessen
ausschließlich die zum jeweiligen Tagesstart produktiv archivierten
Optimierer-Eingänge verwendet. Unvollständige Tage werden übersprungen.

Aufruf:
    python backtest.py --config config.yaml --days 120
    python backtest.py --config config.yaml --start 2026-01-01 --end 2026-03-01
    python backtest.py --config config.yaml --days 60 --errors-only
    python backtest.py --config config.yaml --days 30 --historical-forecasts

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
from ems.local_history import read_optimizer_forecast_asof
from ems.optimizer import Optimizer, OptimizerInputs
from ems.validate import economic_comparison, validate_plan

log = logging.getLogger("ems.backtest")

REQUIRED_PERFECT = ("house_consumption", "pv_generation", "electricity_price",
                    "battery_soc")


class HistoricalForecastUnavailable(RuntimeError):
    """Kein zeitlich zulässiger, vollständiger Prognosesnapshot vorhanden."""


def _plan_index(config, day_start):
    tz = config.general.timezone
    freq = f"{config.general.slot_minutes}min"
    opt_end = (day_start + timedelta(
        hours=config.general.optimization_horizon_hours)).normalize()
    if opt_end <= day_start:
        opt_end += timedelta(days=1)
    idx = pd.date_range(day_start, opt_end, freq=freq, tz=tz, inclusive="left")
    return opt_end, idx


def _initial_soc_wh(repo, config, day_start):
    soc0 = repo.read_scalar_latest(
        "battery_soc", day_start - timedelta(hours=6), day_start)
    if soc0 is None:
        soc0 = config.house_battery.min_soc_percent
    return soc0 / 100.0 * config.house_battery.capacity_wh


def _day_plan(repo, config, day_start):
    """Ein 48-h-Plan ab day_start aus Ist-Daten (perfekte Voraussicht)."""
    opt_end, idx = _plan_index(config, day_start)

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

    # Genug echte Daten vorhanden?
    if load.isna().all() or pv.isna().all() or price.isna().all():
        return None
    inp = OptimizerInputs(
        index=idx, house_load_w=load.values.astype(float),
        pv_w=pv.values.astype(float), price_ct_kwh=price.values.astype(float),
        feedin_ct_kwh=feedin.values.astype(float),
        initial_house_soc_wh=_initial_soc_wh(repo, config, day_start),
    )
    return inp, Optimizer(config).solve(inp)


def _historical_day_plan(repo, config, day_start):
    """Plan aus genau einem am ``day_start`` publizierten Eingangssnapshot."""
    opt_end, idx = _plan_index(config, day_start)
    # Der Dauerbetrieb startet planmäßig wenige Sekunden nach dem Slotwechsel.
    # Bis +5 min darf daher der echte 00:00-Lauf gewählt werden; ein strikt auf
    # 00:00 begrenztes as-of würde fälschlich den 23:45-Vorgängerlauf testen.
    decision_cutoff = day_start + pd.Timedelta(minutes=5)
    issue, frame = read_optimizer_forecast_asof(
        config.e3dc_rscp.history_db_path, decision_cutoff, day_start, opt_end,
        config.general.timezone)
    if issue is None or frame.empty:
        raise HistoricalForecastUnavailable("kein Prognosesnapshot vor Start")

    max_age = pd.Timedelta(minutes=max(
        30, 2 * config.general.run_interval_minutes + 5))
    if issue < day_start - max_age or issue > decision_cutoff:
        raise HistoricalForecastUnavailable(
            f"letzter Snapshot zu alt ({issue.strftime('%d.%m. %H:%M')})")

    frame = frame.reindex(idx)
    required = ("house_load_w", "pv_w", "price_ct_kwh", "feedin_ct_kwh")
    missing = {name: int(frame[name].isna().sum()) if name in frame else len(idx)
               for name in required}
    missing = {name: count for name, count in missing.items() if count}
    if missing:
        detail = ", ".join(f"{name}: {count}" for name, count in missing.items())
        raise HistoricalForecastUnavailable(f"Snapshot unvollständig ({detail})")

    def optional(name):
        if name not in frame or frame[name].isna().all():
            return None
        values = frame[name].to_numpy(dtype=float)
        return values if np.isfinite(values).all() else None

    inp = OptimizerInputs(
        index=idx,
        house_load_w=frame["house_load_w"].to_numpy(dtype=float),
        pv_w=frame["pv_w"].to_numpy(dtype=float),
        price_ct_kwh=frame["price_ct_kwh"].to_numpy(dtype=float),
        feedin_ct_kwh=frame["feedin_ct_kwh"].to_numpy(dtype=float),
        initial_house_soc_wh=_initial_soc_wh(repo, config, day_start),
        pv10_w=optional("pv10_w"),
        ambient_temp_c=optional("ambient_temp_c"),
        solar_w_m2=optional("solar_w_m2"),
    )
    return inp, Optimizer(config).solve(inp), issue, frame


def _forecast_errors(repo, inp, day_start, config):
    """Fehler der am Origin bekannten Prognose für die ersten 24 Stunden."""
    end = day_start + timedelta(days=1)
    idx = inp.index[(inp.index >= day_start) & (inp.index < end)]
    out = {}
    for signal, predicted in (("house_consumption", inp.house_load_w),
                              ("pv_generation", inp.pv_w)):
        if not repo.signal_available(signal):
            continue
        actual = repo.read_slots(signal, day_start, end, fill=False).reindex(idx)
        pred = pd.Series(predicted, index=inp.index).reindex(idx)
        pair = pd.DataFrame({"actual": actual, "pred": pred}).dropna()
        if pair.empty:
            continue
        error = pair["pred"] - pair["actual"]
        dt_h = config.general.slot_minutes / 60.0
        out[signal] = {
            "mae_w": float(error.abs().mean()),
            "bias_kwh": float(error.sum() * dt_h / 1000.0),
            "slots": int(len(pair)),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="EMS Backtest / Invarianten-Sweep")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=120, help="Tage rückwärts ab heute")
    ap.add_argument("--start", help="Startdatum YYYY-MM-DD (überschreibt --days)")
    ap.add_argument("--end", help="Enddatum YYYY-MM-DD")
    ap.add_argument("--errors-only", action="store_true", help="nur Fehler zeigen")
    ap.add_argument(
        "--historical-forecasts", action="store_true",
        help="damals archivierte produktive Prognosen statt perfekter Voraussicht")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    tz = config.general.timezone
    today = pd.Timestamp.now(tz=tz).normalize()
    # Nur Tage, deren KOMPLETTER Horizont in der Vergangenheit liegt - sonst
    # streckt ffill nicht existierende Zukunftsdaten zu konstanten Werten
    # (perfekte Voraussicht setzt echte Ist-Daten über den ganzen Horizont voraus).
    latest = today - timedelta(days=config.general.optimization_horizon_hours // 24 + 1)
    if args.start:
        start = pd.Timestamp(args.start, tz=tz)
        end = pd.Timestamp(args.end, tz=tz) if args.end else latest
    else:
        end = latest
        start = end - timedelta(days=args.days)
    if end > latest:
        print(f"Hinweis: Enddatum auf {latest.date()} begrenzt "
              f"(Horizont braucht vollständige Ist-Daten).\n")
        end = latest
    days = pd.date_range(start, end, freq="D", tz=tz)

    repo = InfluxRepository(config)
    required = ("battery_soc",) if args.historical_forecasts else REQUIRED_PERFECT
    missing = [s for s in required if not repo.signal_available(s)]
    if missing:
        print(f"FEHLER: Signale fehlen in der Config: {missing}")
        return 2

    mode_label = ("echte historische Prognosesnapshots"
                  if args.historical_forecasts else "perfekte Voraussicht")
    print(f"Backtest {start.date()} .. {end.date()}  ({len(days)} Tage), "
          f"{mode_label}, {config.general.optimization_horizon_hours}-h-Horizont\n")
    rule_counter: Counter = Counter()
    flagged_days, n_ok, n_skip = [], 0, 0
    savings = []
    forecast_errors = {"house_consumption": [], "pv_generation": []}
    snapshot_issues = []

    for day in days:
        try:
            if args.historical_forecasts:
                inp, result, issue, _ = _historical_day_plan(repo, config, day)
                snapshot_issues.append(issue)
                for signal, values in _forecast_errors(
                        repo, inp, day, config).items():
                    forecast_errors[signal].append(values)
                res = (inp, result)
            else:
                res = _day_plan(repo, config, day)
        except HistoricalForecastUnavailable as exc:
            print(f"  {day.date()}  ÜBERSPRUNGEN ({exc})")
            n_skip += 1
            continue
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
        # Ersparnis dieses Tages, TERMINALWERT-BEREINIGT: DIESELBE Bewertung
        # wie der econ.worse_than_baseline-Check (economic_comparison) - eine
        # abweichende Metrik (z.B. flacher statt konkaver Terminalwert) hat
        # hier früher selbst beweisbar optimale Pläne "schlechter als
        # Baseline" aussehen lassen (s. ems-projekt-entscheidungen).
        plan_adj, base_adj = economic_comparison(config, result, inp)
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
        savings_label = ("laut damaliger Prognose" if args.historical_forecasts
                         else "perfekte Voraussicht")
        print(f"\nErsparnis/Tag vs. Ohne-EMS-Baseline (terminalwert-bereinigt, "
              f"{savings_label}):")
        print(f"  Median {np.median(arr):+.2f} €  min {arr.min():+.2f}  "
              f"max {arr.max():+.2f}  | negative Tage: {(arr < -0.01).sum()}")
        if (arr < -0.01).any():
            suffix = ("im damaligen Prognosemodell" if args.historical_forecasts
                      else "trotz perfekter Voraussicht")
            print(f"  ⚠ negative Tage = Plan schlechter als naives Verhalten "
                  f"{suffix} -> Modell prüfen!")
    if args.historical_forecasts:
        print("\nPrognosegüte erste 24 h (Prognose minus Ist):")
        labels = {"house_consumption": "Hauslast", "pv_generation": "PV"}
        for signal, rows in forecast_errors.items():
            if not rows:
                print(f"  {labels[signal]:9s}: keine Ist-Vergleichsdaten")
                continue
            maes = np.array([x["mae_w"] for x in rows])
            biases = np.array([x["bias_kwh"] for x in rows])
            print(f"  {labels[signal]:9s}: MAE Median {np.median(maes):.0f} W  | "
                  f"Energie-Bias Median {np.median(biases):+.2f} kWh/Tag "
                  f"({len(rows)} Tage)")
        print(f"  Archivabdeckung: {len(snapshot_issues)} vollständige "
              f"Tages-Snapshots")
    repo.close()
    return 1 if any(s == "error" for s, _ in rule_counter) else 0


if __name__ == "__main__":
    raise SystemExit(main())
