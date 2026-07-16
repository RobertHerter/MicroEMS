"""Einmaliger Backfill der 15-min-Hauslast per RSCP in die lokale SQLite.

Füllt die Verbrauchs-Historie, aus der die Prognose liest, wenn
config.e3dc_rscp.history_source aktiv ist (Schritt Richtung Standalone).
Die Hauslast (W) wird je 15-min-Fenster aus der E3DC-Energiebilanz berechnet.

ACHTUNG: 1 RSCP-Aufruf je Fenster -> 96/Tag (ein Jahr ≈ 35 000, ~1-3 h). Am
besten im Hintergrund laufen lassen. Idempotent (UPSERT); erneuter Aufruf
aktualisiert vorhandene Fenster. Der Dienst führt danach zyklisch nur die
neuen Fenster nach.

Aufruf:
    python rscp_import.py --config config.yaml            # forecast.lookback_days
    python rscp_import.py --config config.yaml --days 90
"""
from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import pandas as pd

from ems.config import load_config
from ems.local_history import count, write_house_load
from ems.rscp import E3DCLink

log = logging.getLogger("ems.rscp_import")


def main() -> int:
    ap = argparse.ArgumentParser(description="E3DC 15-min-Hauslast -> SQLite")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=None,
                    help="Backfill-Tiefe in Tagen (Default: e3dc_rscp."
                         "history_backfill_days)")
    ap.add_argument("--chunk-days", type=int, default=7,
                    help="in Blöcken schreiben (Fortschritt/Robustheit)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    if not config.e3dc_rscp.enabled:
        print("e3dc_rscp.enabled=false – bitte aktivieren und Zugang eintragen.")
        return 2
    days = args.days if args.days is not None else config.e3dc_rscp.history_backfill_days
    tz = config.general.timezone
    # Das jüngste E3DC-DB-Fenster ist kurz nach Slotende noch nicht final und
    # kann eine negative, später zu 0 gekappte Zwischenbilanz liefern.
    now = (pd.Timestamp.now(tz=tz) - pd.Timedelta(
        minutes=config.e3dc_rscp.history_settle_minutes)).floor("15min")
    start = now - timedelta(days=days)
    db = config.e3dc_rscp.history_db_path
    print(f"Backfill 15-min-Hauslast {start.date()} .. {now.date()} "
          f"({days} Tage, ~{days*96} RSCP-Abrufe) -> {db}")

    link = E3DCLink(config)
    total = 0
    t = start
    step = timedelta(days=args.chunk_days)
    try:
        while t < now:
            chunk_end = min(t + step, now)
            data = link.read_house_load_15min(t, chunk_end)
            total += write_house_load(db, data)
            print(f"  {t.date()} .. {chunk_end.date()}: +{len(data)} "
                  f"(gesamt {total})", flush=True)
            t = chunk_end
    finally:
        link.close()
    print(f"Fertig: {count(db)} Fenster in der DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
