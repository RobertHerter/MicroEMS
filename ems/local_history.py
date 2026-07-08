"""Lokaler 15-min-Hausverbrauchs-Speicher (SQLite) für die Prognose.

Alternative zur InfluxDB als Historienquelle: die 15-min-Hauslast (W) wird per
RSCP aus dem E3DC gefüllt (Backfill + zyklisch) und hier abgelegt. Die
Verbrauchsprognose (forecast.load_history) liest daraus, wenn
config.e3dc_rscp.history_source aktiv ist -> Schritt Richtung Standalone.

Schlüssel = UTC-ISO-Zeitstempel (monoton, DST-sicher). Werte = W (Mittel des
15-min-Fensters).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Dict, Optional

import pandas as pd


def _con(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS house_load ("
                " ts TEXT PRIMARY KEY, w REAL NOT NULL)")
    con.commit()
    return con


def write_house_load(path: str, mapping: Dict[str, float]) -> int:
    """UPSERT einer Zuordnung {UTC-ISO -> W}. Rückgabe: Anzahl Zeilen."""
    if not mapping:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO house_load(ts, w) VALUES(?, ?) "
        "ON CONFLICT(ts) DO UPDATE SET w=excluded.w",
        [(k, float(v)) for k, v in mapping.items()])
    con.commit()
    con.close()
    return len(mapping)


def last_timestamp(path: str) -> Optional[pd.Timestamp]:
    """Jüngster gespeicherter Slot (tz-aware UTC), oder None."""
    try:
        con = _con(path)
        row = con.execute("SELECT max(ts) FROM house_load").fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return pd.Timestamp(row[0])


def read_house_load(path: str, start, end, tz: str) -> pd.Series:
    """15-min-Hauslast [start, end) als tz-lokale Serie (leer, wenn nichts da)."""
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, w FROM house_load WHERE ts >= ? AND ts < ? ORDER BY ts",
            (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)


def count(path: str) -> int:
    try:
        con = _con(path)
        n = con.execute("SELECT count(*) FROM house_load").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return 0
