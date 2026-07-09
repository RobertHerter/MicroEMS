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
    # Live-Ist-Werte des E3DC je Zyklus (Ersatz für die InfluxDB-Ist-Signale).
    con.execute("CREATE TABLE IF NOT EXISTS actuals ("
                " ts TEXT PRIMARY KEY, pv_w REAL, house_w REAL, grid_w REAL,"
                " battery_w REAL, soc REAL)")
    # Stündliche Temperatur (Open-Meteo) für die Prognose-Gewichtung.
    con.execute("CREATE TABLE IF NOT EXISTS temperature ("
                " ts TEXT PRIMARY KEY, temp_c REAL NOT NULL)")
    con.commit()
    return con


# Signalname (InfluxDB-Konvention) -> Spalte in der actuals-Tabelle
_ACTUAL_FIELD = {"pv_generation": "pv_w", "house_consumption": "house_w",
                 "grid_power": "grid_w", "battery_power": "battery_w",
                 "battery_soc": "soc"}


def write_actuals(path: str, ts, live: dict) -> None:
    """Einen Live-Snapshot (aus rscp.read_live) beim Slot-Zeitstempel ablegen."""
    if not live:
        return
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    con = _con(path)
    con.execute(
        "INSERT INTO actuals(ts, pv_w, house_w, grid_w, battery_w, soc) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET "
        "pv_w=excluded.pv_w, house_w=excluded.house_w, grid_w=excluded.grid_w, "
        "battery_w=excluded.battery_w, soc=excluded.soc",
        (key, live.get("pv_w"), live.get("house_load_w"), live.get("grid_w"),
         live.get("battery_w"), live.get("soc_percent")))
    con.commit()
    con.close()


def read_actual(path: str, field: str, start, end, tz: str) -> pd.Series:
    """Ist-Wert-Spalte [start, end) als tz-lokale Serie (leer, wenn nichts da)."""
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            f"SELECT ts, {field} FROM actuals WHERE ts >= ? AND ts < ? "
            f"AND {field} IS NOT NULL ORDER BY ts", (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)


def write_temperature(path: str, mapping: Dict[str, float]) -> int:
    """UPSERT stündlicher Temperaturen {UTC-ISO -> °C}."""
    if not mapping:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO temperature(ts, temp_c) VALUES(?, ?) "
        "ON CONFLICT(ts) DO UPDATE SET temp_c=excluded.temp_c",
        [(k, float(v)) for k, v in mapping.items()])
    con.commit()
    con.close()
    return len(mapping)


def read_temperature(path: str, start, end, tz: str, freq: str) -> pd.Series:
    """Temperatur [start, end) auf das Slot-Raster interpoliert (wie zuvor
    read_slots('temperature')). Leer, wenn nichts vorhanden."""
    # etwas Rand mitlesen, damit die Interpolation an den Kanten greift
    s_utc = (pd.Timestamp(start) - pd.Timedelta(hours=2)).tz_convert("UTC").isoformat()
    e_utc = (pd.Timestamp(end) + pd.Timedelta(hours=2)).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, temp_c FROM temperature WHERE ts >= ? AND ts < ? ORDER BY ts",
            (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    hourly = pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)
    # tz aus den (bereits tz-bewussten) Endpunkten ableiten, NICHT zusätzlich
    # tz= übergeben (sonst pytz/zoneinfo-Konflikt in date_range).
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz), freq=freq,
                         inclusive="left")
    if len(grid) == 0:
        return hourly
    return (hourly.reindex(hourly.index.union(grid)).interpolate(method="time")
            .reindex(grid))


def read_actual_signal(config, repo, signal: str, start, end):
    """Ist-Signal aus dem lokalen E3DC-Speicher (wenn history_source aktiv und
    das Signal E3DC-nativ ist), sonst aus der InfluxDB. Zentrale Weiche für den
    Standalone-Betrieb."""
    field = _ACTUAL_FIELD.get(signal)
    if config.e3dc_rscp.history_source and field:
        return read_actual(config.e3dc_rscp.history_db_path, field, start, end,
                           config.general.timezone)
    return repo.read_slots(signal, start, end, fill=False)


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
