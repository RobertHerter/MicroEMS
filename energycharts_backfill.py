"""Einmaliger Backfill der Spotpreis-Historie (Energy-Charts) in die lokale
SQLite – Basis für die Folgetag-Preisschätzung (Ähnliche-Tage) im Standalone.

Holt den Day-Ahead-Spot (EUR/MWh -> ct/kWh) blockweise (90-Tage-Blöcke, damit
der einzelne API-Call klein bleibt). Idempotent (UPSERT). Das Tarifmodell wird
NICHT gespeichert – es rechnet erst beim Auslesen (ems/tariff.py), damit
Tarifänderungen keinen neuen Backfill erfordern.

Aufruf:
    python energycharts_backfill.py --config config.yaml            # tariff.history_backfill_days
    python energycharts_backfill.py --config config.yaml --days 730
"""
from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import pandas as pd

from ems.config import load_config
from ems.energycharts import fetch_spot
from ems.local_history import write_spot

log = logging.getLogger("ems.energycharts_backfill")


def main() -> int:
    ap = argparse.ArgumentParser(description="Spotpreis-Historie -> SQLite")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    if not config.tariff.enabled:
        print("tariff.enabled=false – bitte aktivieren (Energy-Charts als Preisquelle).")
        return 2
    if config.tariff.type != "dynamic":
        print("tariff.type=fixed – kein Spot-Backfill nötig (konstanter Preis).")
        return 0

    days = args.days if args.days is not None else config.tariff.history_backfill_days
    bzn = config.tariff.bidding_zone
    db = config.e3dc_rscp.history_db_path
    end = pd.Timestamp.now(tz="UTC").normalize() + timedelta(days=1)  # inkl. morgen
    start = end - timedelta(days=days)
    print(f"Spot-Backfill {start.date()} .. {end.date()} ({bzn}) -> {db}")

    total = 0
    block = start
    while block < end:
        b_end = min(block + timedelta(days=90), end)
        try:
            data = fetch_spot(bzn, block.strftime("%Y-%m-%d"), b_end.strftime("%Y-%m-%d"))
        except Exception as exc:
            print(f"  {block.date()}..{b_end.date()}: FEHLER {exc}")
            return 1
        total += write_spot(db, data)
        print(f"  {block.date()} .. {b_end.date()}: +{len(data)} (gesamt {total})",
              flush=True)
        block = b_end + timedelta(days=1)
    print(f"Fertig: {total} Spot-Preiswerte in der DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
