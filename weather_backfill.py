"""Einmaliger Backfill der stündlichen Temperatur- + Solar-Einstrahlungs-Historie
(Open-Meteo ERA5) in die lokale SQLite – Temperatur für die tiefe Ähnliche-Tage-
Gewichtung, Einstrahlung als Historie für den solaren Pool-Wärmeeintrag.

Die Forecast-API deckt nur die letzten ~92 Tage ab; dieses Skript holt ältere
Jahre über die Archive-API (ein Call je Jahres-Block, beide Felder zusammen).
Idempotent (UPSERT).

Aufruf:
    python weather_backfill.py --config config.yaml            # forecast.lookback_days
    python weather_backfill.py --config config.yaml --days 365
"""
from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import pandas as pd

from ems.config import load_config
from ems.local_history import write_temperature, write_radiation
from ems.weather import fetch_archive

log = logging.getLogger("ems.weather_backfill")


def main() -> int:
    ap = argparse.ArgumentParser(description="Temperatur-Historie -> SQLite")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    if not config.weather.enabled:
        print("weather.enabled=false – bitte aktivieren und latitude/longitude setzen.")
        return 2
    days = args.days if args.days is not None else config.forecast.lookback_days
    w = config.weather
    db = config.e3dc_rscp.history_db_path
    end = pd.Timestamp.now(tz="UTC").normalize()
    start = end - timedelta(days=days)
    # ERA5 hat ~5 Tage Verzug; die jüngsten Tage liefert die Forecast-API im Betrieb.
    arc_end = end - timedelta(days=6)
    print(f"Temperatur+Strahlungs-Backfill {start.date()} .. {arc_end.date()} "
          f"({w.latitude},{w.longitude}) -> {db}")

    total_t, total_r = 0, 0
    block = start
    while block < arc_end:
        b_end = min(block + timedelta(days=365), arc_end)
        try:
            temp_map, rad_map = fetch_archive(
                w.latitude, w.longitude,
                block.strftime("%Y-%m-%d"), b_end.strftime("%Y-%m-%d"))
        except Exception as exc:
            print(f"  {block.date()}..{b_end.date()}: FEHLER {exc}")
            return 1
        total_t += write_temperature(db, temp_map)
        total_r += write_radiation(db, rad_map)
        print(f"  {block.date()} .. {b_end.date()}: +{len(temp_map)} Temp / "
              f"+{len(rad_map)} Strahlung (gesamt {total_t}/{total_r})", flush=True)
        block = b_end + timedelta(days=1)
    print(f"Fertig: {total_t} Temperatur- + {total_r} Strahlungs-Stundenwerte in der DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
