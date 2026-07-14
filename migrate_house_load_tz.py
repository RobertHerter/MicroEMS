#!/usr/bin/env python3
"""Einmalige Korrektur der 2-h-Verschiebung in der Hauslast-Historie.

read_house_load_15min fragte die E3DC-DB fälschlich mit UTC- statt Ortszeit ab
(Commit b092cc2). Dadurch sind ALLE bereits gespeicherten house_load-Werte um den
Zeitzonen-Offset zu spät abgelegt (Sommer +2 h). Dieses Skript re-keyt jeden
Eintrag um -utcoffset(Ortszeit) und richtet die Historie damit auf die echte
Ortszeit aus (verifiziert gegen den korrekt getakteten Live-Poll).

Idempotent (Marker in _meta), legt vorher ein Backup an. Aufruf als ems:
    sudo -u ems /opt/ems/.venv/bin/python /opt/ems/migrate_house_load_tz.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime

import pandas as pd

from ems.config import load_config

MARKER = "house_load_tz_fixed_v1"


def main() -> int:
    cfg = load_config("config.yaml")
    db = cfg.e3dc_rscp.history_db_path
    tz = cfg.general.timezone

    con = sqlite3.connect(db, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS _meta (k TEXT PRIMARY KEY, v TEXT)")
    if con.execute("SELECT 1 FROM _meta WHERE k=?", (MARKER,)).fetchone():
        print("Bereits migriert (Marker vorhanden) – nichts zu tun.")
        return 0

    rows = con.execute("SELECT ts, w FROM house_load").fetchall()
    if not rows:
        print("house_load leer – nichts zu tun.")
        con.execute("INSERT INTO _meta(k, v) VALUES(?, ?)", (MARKER, "empty"))
        con.commit()
        return 0

    bak = f"{db}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(db, bak)
    print(f"Backup: {bak}  ({len(rows)} Zeilen)")

    K = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    loc = K.tz_convert(tz)
    off = pd.to_timedelta([t.utcoffset().total_seconds() for t in loc], unit="s")
    new_utc = (K - off)            # -Offset = echte Ortszeit-Ausrichtung
    # Kollisionen (DST-Übergang) per Mittelwert zusammenfassen.
    s = pd.Series([r[1] for r in rows], index=new_utc).groupby(level=0).mean()

    con.execute("DELETE FROM house_load")
    con.executemany(
        "INSERT INTO house_load(ts, w) VALUES(?, ?) "
        "ON CONFLICT(ts) DO UPDATE SET w=excluded.w",
        [(t.isoformat(), float(v)) for t, v in s.items()])
    con.execute("INSERT INTO _meta(k, v) VALUES(?, ?)",
                (MARKER, datetime.now().isoformat()))
    con.commit()
    con.close()
    print(f"Migriert: {len(rows)} -> {len(s)} Zeilen re-keyt (-Zeitzonen-Offset).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
