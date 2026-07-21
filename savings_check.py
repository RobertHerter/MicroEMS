"""Ersparnis gegen die echten E3DC-Zählerwerte gegenprüfen (Verbesserung #5).

Liest die GEMESSENEN 15-min-Energieaggregate der E3DC-Historie (Netzbezug/
-einspeisung, PV, Last, Akku) über einen Zeitraum, rechnet daraus unabhängig
vom laufenden Tracking die reale Ersparnis (metered) gegen die "Ohne-EMS"-
Baseline und gleicht das mit dem Tracking-Signal (actuals.grid_w) ab. Prüft
zugleich die Energiebilanz -> deckt Vorzeichen-/Integrationsfehler auf.

Aufruf:
    python savings_check.py --config config.yaml --days 7

Ein RSCP-Aufruf je 15-min-Fenster (~96/Tag). Es wird NICHTS geschrieben und der
Dienst nicht berührt (nur lesende RSCP-Aufrufe).
"""
from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import pandas as pd

from ems.config import load_config
from ems.influx import InfluxRepository
from ems.local_history import read_actual_signal
from ems.rscp import E3DCLink
from ems.savings_validate import METER_COLUMNS, reconcile
from ems.tariff import read_price_signal


def _meter_frame(raw: dict, tz: str) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=METER_COLUMNS)
    idx = pd.to_datetime(sorted(raw), utc=True, format="ISO8601").tz_convert(tz)
    rows = [raw[k] for k in sorted(raw)]
    return pd.DataFrame(rows, index=idx)


def _soc0_wh(config, repo, start, cap_wh):
    try:
        s = read_actual_signal(config, repo, "battery_soc",
                               start - timedelta(hours=6), start + timedelta(hours=1))
        s = s.dropna()
        if not s.empty:
            return float(s.iloc[0]) / 100.0 * cap_wh
    except Exception:
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="EMS Ersparnis-Gegenprüfung (Zähler)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=7, help="Zeitraum rückwärts (Tage)")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
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

    link = E3DCLink(config)
    try:
        raw = link.read_energy_15min(start, now)
    finally:
        link.close()
    meter = _meter_frame(raw, tz)
    if meter.empty:
        print("Keine Zählerdaten gelesen (RSCP-Historie leer?).")
        return 1

    repo = InfluxRepository(config)
    price = read_price_signal(config, repo, start, now)
    if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
        feedin = repo.read_slots("feed_in_tariff", start, now, fill=False)
    else:
        feedin = pd.Series(config.feed_in.fixed_ct_kwh, index=meter.index)
    grid_w = read_actual_signal(config, repo, "grid_power", start, now)
    soc0 = _soc0_wh(config, repo, start, config.house_battery.capacity_wh)

    r = reconcile(meter, price, feedin, config, actual_grid_w=grid_w, soc0_wh=soc0)
    if r.get("insufficient"):
        print("Zu wenige vollständige Slots (Preis/Zähler) für die Prüfung.")
        return 1

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
    print(f"\n=> Reale Ersparnis (Zähler): {r['saved_eur']:+.2f} €{pct}  "
          f"-> {verdict}")

    tr = r.get("tracker")
    if tr:
        sign = "OK" if tr["sign_ok"] else "!! VORZEICHEN VERTAUSCHT"
        print(f"\nTracking-Signal (actuals.grid_w): Bezug {tr['import_kwh']:.1f} kWh, "
              f"Einspeisung {tr['export_kwh']:.1f} kWh, Kosten {tr['net_cost_eur']:+.2f} € "
              f"[{sign}]")
        print(f"  Kostendifferenz zum Zähler: {tr['cost_delta_eur']:+.2f} € "
              f"(Momentan-Snapshot vs. Energiezähler; kleine Abweichung normal).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
