"""Einmaliger Import der historischen Ist-PV (E3DC pv_generation) aus der
InfluxDB in den lokalen Speicher (actuals.pv_w) – Gegenstück zum Solcast-
Prognose-Import. Erst damit kann die PV-Kalibrierung (Prognose vs. Ist)
standalone gegen lokale Daten laufen.

Nur die Spalte pv_w wird gesetzt (übrige actuals-Spalten unberührt). Idempotent
(UPSERT), blockweise (90 Tage).

Aufruf (als Nutzer ems):
    sudo -u ems .venv/bin/python pv_actual_import.py --config config.yaml
    sudo -u ems .venv/bin/python pv_actual_import.py --config config.yaml --days 365
"""
from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from ems.config import load_config
from ems.influx import InfluxRepository
from ems.local_history import write_pv_actual

log = logging.getLogger("ems.pv_actual_import")


def main() -> int:
    ap = argparse.ArgumentParser(description="Historische Ist-PV -> lokale SQLite (actuals.pv_w)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    db = config.e3dc_rscp.history_db_path
    days = args.days if args.days is not None else config.forecast.lookback_days
    now = pd.Timestamp.now(tz=config.general.timezone)
    start = now - timedelta(days=days)
    print(f"Ist-PV-Import {start.date()} .. {now.date()} -> {db} (actuals.pv_w)")

    repo = InfluxRepository(config)
    total = 0
    try:
        if not repo.signal_available("pv_generation"):
            print("pv_generation in der InfluxDB nicht verfügbar – Abbruch.")
            return 2
        block = start
        while block < now:
            b_end = min(block + timedelta(days=90), now)
            pv = repo.read_slots("pv_generation", block, b_end, fill=False)
            mapping = {pd.Timestamp(ts).tz_convert("UTC").isoformat(): float(v)
                       for ts, v in pv.dropna().items() if np.isfinite(v)}
            total += write_pv_actual(db, mapping)
            print(f"  {block.date()} .. {b_end.date()}: +{len(mapping)} (gesamt {total})",
                  flush=True)
            block = b_end
    finally:
        repo.close()
    print(f"Fertig: {total} Ist-PV-Werte in actuals.pv_w.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
