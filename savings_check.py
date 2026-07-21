"""Ersparnis gegen die echten E3DC-Zählerwerte gegenprüfen (Verbesserung #5)
und als laufende Kennzahl persistieren (#2).

Liest die GEMESSENEN 15-min-Energieaggregate der E3DC-Historie (Netzbezug/
-einspeisung, PV, Last, Akku), rechnet daraus unabhängig vom laufenden Tracking
die reale Ersparnis (metered) gegen die "Ohne-EMS"-Baseline und gleicht das mit
dem Tracking-Signal (actuals.grid_w) ab. Prüft die Energiebilanz -> deckt
Vorzeichen-/Integrationsfehler auf.

Aufruf:
    python savings_check.py --config config.yaml --days 7        # Fenster-Report
    python savings_check.py --config config.yaml --persist        # + je Tag ablegen
    python savings_check.py --config config.yaml --summary         # kumuliert (nur DB)

Ein RSCP-Aufruf je 15-min-Fenster (~96/Tag). Außer der optionalen Persistenz in
die lokale SQLite (savings_validated) wird nichts geschrieben; nur lesende RSCP-
Aufrufe. Der nächtliche Timer (ems-savings.timer) ruft --persist auf.
"""
from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import pandas as pd

from ems.config import load_config
from ems.influx import InfluxRepository
from ems.local_history import (read_actual_signal, read_savings_baseline_soc,
                               read_savings_validated, write_savings_validated)
from ems.rscp import E3DCLink
from ems.savings_validate import METER_COLUMNS, reconcile
from ems.tariff import read_price_signal


def _meter_frame(raw: dict, tz: str) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=METER_COLUMNS)
    idx = pd.to_datetime(sorted(raw), utc=True, format="ISO8601").tz_convert(tz)
    return pd.DataFrame([raw[k] for k in sorted(raw)], index=idx)


def _soc0_wh(config, repo, start, cap_wh):
    try:
        s = read_actual_signal(config, repo, "battery_soc",
                               start - timedelta(hours=6),
                               start + timedelta(hours=1)).dropna()
        if not s.empty:
            return float(s.iloc[0]) / 100.0 * cap_wh
    except Exception:
        pass
    return None


def _gather(config, start, end):
    """Zähler (RSCP) + Preis/Einspeisung + Tracking-Signal für [start, end)."""
    tz = config.general.timezone
    link = E3DCLink(config)
    try:
        raw = link.read_energy_15min(start, end)
    finally:
        link.close()
    meter = _meter_frame(raw, tz)
    if meter.empty:
        return None
    repo = InfluxRepository(config)
    price = read_price_signal(config, repo, start, end)
    if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
        feedin = repo.read_slots("feed_in_tariff", start, end, fill=False)
    else:
        feedin = pd.Series(config.feed_in.fixed_ct_kwh, index=meter.index)
    grid_w = read_actual_signal(config, repo, "grid_power", start, end)
    return {"repo": repo, "meter": meter, "price": price,
            "feedin": feedin, "grid_w": grid_w}


def _reconcile_slice(config, data, s, e, soc0=None):
    cap = config.house_battery.capacity_wh
    meter = data["meter"].loc[(data["meter"].index >= s) & (data["meter"].index < e)]
    if meter.empty:
        return None
    if soc0 is None:
        soc0 = _soc0_wh(config, data["repo"], s, cap)
    r = reconcile(meter, data["price"], data["feedin"], config,
                  actual_grid_w=data["grid_w"], soc0_wh=soc0)
    return None if r.get("insufficient") else r


def _print_report(r):
    print(f"\nZeitraum: {r['start'].strftime('%d.%m. %H:%M')} .. "
          f"{r['end'].strftime('%d.%m. %H:%M')}  ({r['n_slots']} Slots)")
    b = r["balance"]
    flag = "OK" if b["ok"] else "!! PRÜFEN"
    print(f"\nEnergiebilanz-Residuum: Ø |{b['mean_abs_wh']:.0f}| Wh, "
          f"max |{b['max_abs_wh']:.0f}| Wh, Bias {b['mean_signed_wh']:+.0f} Wh/Slot "
          f"[{flag}]")
    print("  (PV + Akku-Entladung + Netzbezug == Last + Akku-Ladung + Einspeisung)")
    m, base = r["meter"], r["baseline"]
    print(f"\nGemessen (Zähler):  Bezug {m['import_kwh']:.1f} kWh, "
          f"Einspeisung {m['export_kwh']:.1f} kWh  ->  Netzkosten "
          f"{m['net_cost_eur']:+.2f} €")
    print(f"  (PV {m['pv_kwh']:.1f} kWh, Last {m['load_kwh']:.1f} kWh)")
    print(f"Ohne-EMS-Baseline:  Bezug {base['import_kwh']:.1f} kWh, "
          f"Einspeisung {base['export_kwh']:.1f} kWh  ->  Netzkosten "
          f"{base['net_cost_eur']:+.2f} €")
    pct = f" ({r['saved_pct']:+.1f} %)" if r["saved_pct"] is not None else ""
    verdict = "das EMS spart Geld" if r["saved_eur"] > 0 else \
        "das EMS spart NICHT – Modell prüfen!"
    print(f"\n=> Reale Ersparnis (Zähler): {r['saved_eur']:+.2f} €{pct}  -> {verdict}")
    tr = r.get("tracker")
    if tr:
        sign = "OK" if tr["sign_ok"] else "!! VORZEICHEN VERTAUSCHT"
        print(f"\nTracking-Signal (actuals.grid_w): Bezug {tr['import_kwh']:.1f} kWh, "
              f"Einspeisung {tr['export_kwh']:.1f} kWh, Kosten {tr['net_cost_eur']:+.2f} € "
              f"[{sign}]")
        print(f"  Kostendifferenz zum Zähler: {tr['cost_delta_eur']:+.2f} € "
              f"(Momentan-Snapshot vs. Energiezähler; kleine Abweichung normal).")


def _persist_days(config, data, now):
    """Jeden VOLLSTÄNDIGEN Kalendertag im Fenster gegen die Zähler validieren
    und ablegen (idempotent). Rückgabe: Anzahl geschriebener Tage."""
    tz = config.general.timezone
    db = config.e3dc_rscp.history_db_path
    first = data["meter"].index.min().normalize()
    today = pd.Timestamp(now).tz_convert(tz).normalize()
    min_slots = 0.9 * (24 * 60 / config.general.slot_minutes)   # nur volle Tage
    written = 0
    day = first
    while day < today:                    # nur abgeschlossene Tage
        # Durchgehende Ohne-EMS-Baseline: vom gespeicherten End-SoC des Vortags
        # seeden (sonst vom realen Ist-SoC zum Tagesbeginn). So summieren die
        # Tageswerte sauber zum Zeitraum-Total statt an Tagesgrenzen zu springen.
        prev = (day - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        soc0 = read_savings_baseline_soc(db, prev)
        r = _reconcile_slice(config, data, day, day + pd.Timedelta(days=1), soc0=soc0)
        if r is not None and r["n_slots"] < min_slots:
            r = None                       # partieller Rand-Tag -> nicht ablegen
        if r is not None:
            r["computed_ts"] = pd.Timestamp(now).tz_convert("UTC")
            write_savings_validated(db, day.strftime("%Y-%m-%d"), r)
            written += 1
            print(f"  {day.strftime('%Y-%m-%d')}: {r['saved_eur']:+.2f} € "
                  f"(Bilanz {'ok' if r['balance']['ok'] else 'PRÜFEN'})", flush=True)
        day += pd.Timedelta(days=1)
    return written


def _print_summary(config):
    df = read_savings_validated(config.e3dc_rscp.history_db_path)
    if df.empty:
        print("Noch keine validierten Tage gespeichert (savings_check --persist).")
        return
    total = float(df["saved_eur"].sum())
    days = len(df)
    per_day = total / days if days else 0.0
    month = df[df["day"].str[:7] == df["day"].iloc[-1][:7]]["saved_eur"].sum()
    bad = int((df["balance_ok"] == 0).sum())
    print(f"Validierte Ersparnis (gegen E3DC-Zähler): {total:+.2f} € über "
          f"{days} Tage (Ø {per_day:+.2f} €/Tag).")
    print(f"  laufender Monat: {float(month):+.2f} €"
          + (f"  |  {bad} Tag(e) mit auffälliger Energiebilanz!" if bad else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="EMS Ersparnis-Gegenprüfung (Zähler)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=7, help="Zeitraum rückwärts (Tage)")
    ap.add_argument("--persist", action="store_true",
                    help="jeden vollständigen Tag validiert ablegen (savings_validated)")
    ap.add_argument("--summary", action="store_true",
                    help="kumulierte validierte Ersparnis aus der DB (kein RSCP)")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)

    if args.summary:
        _print_summary(config)
        if not args.persist:
            return 0

    if not config.e3dc_rscp.enabled:
        print("e3dc_rscp.enabled=false – die Zählerwerte kommen per RSCP vom E3DC.")
        return 2

    tz = config.general.timezone
    now = (pd.Timestamp.now(tz=tz) - pd.Timedelta(
        minutes=config.e3dc_rscp.history_settle_minutes)).floor("15min")
    start = now - timedelta(days=args.days)
    print(f"Lese Zählerwerte {start.strftime('%d.%m. %H:%M')} .. "
          f"{now.strftime('%d.%m. %H:%M')}  (~{args.days*96} RSCP-Abrufe) …",
          flush=True)

    data = _gather(config, start, now)
    if data is None:
        print("Keine Zählerdaten gelesen (RSCP-Historie leer?).")
        return 1

    if args.persist:
        print("\nTageweise validieren und ablegen:")
        n = _persist_days(config, data, now)
        print(f"-> {n} volle(r) Tag(e) in savings_validated geschrieben.")
        _print_summary(config)
        return 0

    # Fenster-Gesamtreport (manueller Aufruf ohne --persist): eine durchgehende
    # Abrechnung über den ganzen Zeitraum inkl. Bilanz-/Vorzeichen-Check.
    r = _reconcile_slice(config, data, start, now)
    if r is None:
        print("Zu wenige vollständige Slots (Preis/Zähler) für den Fenster-Report.")
        return 1
    _print_report(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
