"""Historische E3DC-Daten per RSCP einlesen und lokal in SQLite speichern.

Nutzt die optionale RSCP-Anbindung (config.e3dc_rscp, Bibliothek pye3dc) und
holt die Tagesbilanzen (Energie) der letzten Tage in eine lokale SQLite-DB
(config.e3dc_rscp.history_db_path). RSCP liefert Tages-/Monatsaggregate – kein
15-min-Raster; für die 15-min-Prognose bleibt InfluxDB die Quelle.

Aufruf:
    python rscp_import.py --config config.yaml --days 365

Idempotent: bereits vorhandene Tage werden aktualisiert (UPSERT). Es wird
nichts am laufenden Dienst geändert.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3

from ems.config import load_config
from ems.rscp import E3DCLink

log = logging.getLogger("ems.rscp_import")


def _ensure_table(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS e3dc_daily ("
        " date TEXT PRIMARY KEY,"
        " data_json TEXT NOT NULL)")
    con.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description="E3DC-Historie per RSCP -> SQLite")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    if not config.e3dc_rscp.enabled:
        print("e3dc_rscp.enabled=false – bitte in der Config aktivieren und "
              "Zugang (host/username/password/key) eintragen.")
        return 2

    link = E3DCLink(config)
    rows = link.read_history_daily(args.days)
    link.close()
    if not rows:
        print("Keine Historie erhalten (Verbindung/pye3dc prüfen, siehe Log).")
        return 1

    con = sqlite3.connect(config.e3dc_rscp.history_db_path)
    _ensure_table(con)
    n = 0
    for row in rows:
        con.execute(
            "INSERT INTO e3dc_daily(date, data_json) VALUES(?, ?) "
            "ON CONFLICT(date) DO UPDATE SET data_json=excluded.data_json",
            (row["date"], json.dumps(row, ensure_ascii=False)))
        n += 1
    con.commit()
    con.close()
    print(f"{n} Tagesbilanzen -> {config.e3dc_rscp.history_db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
