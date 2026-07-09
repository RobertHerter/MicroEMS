"""Einmaliger Import der historischen Solcast-PV-Vorhersage (pv_forecast +
P10/P90) aus der InfluxDB in den lokalen Speicher (Tabelle pv_forecast).

Die Live-Quellen (Ost/West) decken nur ~48 h ab jetzt ab. Für Intraday-PV-
Korrektur, Dashboard-Historie und eine spätere PV-Kalibrierung fehlt die tiefe
Vergangenheit – die holt dieses Skript aus der InfluxDB.

Wichtig: gespeichert als eigene Quelle "influx_hist" und NUR für Zeitstempel VOR
dem frühesten Live-Datenpunkt. So überlappt der historische (kombinierte)
Forecast nie mit den Live-Quellen Ost+West -> combine="sum" zählt nichts doppelt
(Vergangenheit = influx_hist, ab Live-Beginn = Ost+West).

Aufruf (als Nutzer ems, damit die DB schreibbar bleibt):
    sudo -u ems .venv/bin/python solcast_import.py --config config.yaml
    sudo -u ems .venv/bin/python solcast_import.py --config config.yaml --days 365
"""
from __future__ import annotations

import argparse
import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from ems.config import load_config
from ems.influx import InfluxRepository
from ems.local_history import write_pv_forecast

log = logging.getLogger("ems.solcast_import")

_HIST_SOURCE = "influx_hist"


def _live_start(db: str):
    """Frühester Live-Zeitstempel (tz-aware UTC) oder None."""
    import sqlite3
    try:
        con = sqlite3.connect(db)
        row = con.execute("SELECT min(ts) FROM pv_forecast WHERE source!=?",
                          (_HIST_SOURCE,)).fetchone()
        con.close()
    except Exception:
        return None
    return pd.Timestamp(row[0]) if row and row[0] else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Historische Solcast-PV -> lokale SQLite")
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

    # Obergrenze = Beginn der Live-Abdeckung (exklusiv), sonst jetzt.
    cutoff = _live_start(db)
    if cutoff is None:
        cutoff = now.tz_convert("UTC")
        print("Hinweis: noch keine Live-Solcast-Daten – importiere bis jetzt.")
    cutoff = cutoff.tz_convert(config.general.timezone)
    start = now - timedelta(days=days)
    if start >= cutoff:
        print("Nichts zu importieren (Startzeitpunkt liegt nach dem Live-Beginn).")
        return 0
    print(f"Solcast-Historie-Import {start.date()} .. {cutoff:%Y-%m-%d %H:%M} "
          f"(exkl. Live) -> {db} [Quelle {_HIST_SOURCE}]")

    repo = InfluxRepository(config)
    total = 0
    try:
        if not repo.signal_available("pv_forecast"):
            print("pv_forecast in der InfluxDB nicht verfügbar – Abbruch.")
            return 2
        has10 = repo.signal_available("pv_forecast_p10")
        has90 = repo.signal_available("pv_forecast_p90")
        block = start
        while block < cutoff:
            b_end = min(block + timedelta(days=90), cutoff)
            pv = repo.read_slots("pv_forecast", block, b_end, fill=False)
            p10 = (repo.read_slots("pv_forecast_p10", block, b_end, fill=False)
                   if has10 else pd.Series(dtype="float64"))
            p90 = (repo.read_slots("pv_forecast_p90", block, b_end, fill=False)
                   if has90 else pd.Series(dtype="float64"))
            mapping = {}
            for ts, v in pv.dropna().items():
                if not np.isfinite(v):
                    continue
                v10 = p10.get(ts, v)
                v90 = p90.get(ts, v)
                v10 = float(v10) if v10 is not None and np.isfinite(v10) else float(v)
                v90 = float(v90) if v90 is not None and np.isfinite(v90) else float(v)
                key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
                mapping[key] = (float(v), v10, v90)
            total += write_pv_forecast(db, _HIST_SOURCE, mapping)
            print(f"  {block.date()} .. {b_end.date()}: +{len(mapping)} (gesamt {total})",
                  flush=True)
            block = b_end
    finally:
        repo.close()
    print(f"Fertig: {total} historische PV-Vorhersagewerte in der DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
