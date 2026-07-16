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
    # Day-Ahead-Spotpreis (Energy-Charts) in ct/kWh netto. Das Tarifmodell
    # (ems/tariff.py) rechnet daraus beim Auslesen den Bezugspreis (brutto).
    con.execute("CREATE TABLE IF NOT EXISTS spot_price ("
                " ts TEXT PRIMARY KEY, ct REAL NOT NULL)")
    # PV-Vorhersage (Solcast) je Quelle (rooftop site), W. p10/p90 = Unsicherheits-
    # bänder. Beim Auslesen werden die Quellen kombiniert (sum/mean, ems/solcast.py).
    con.execute("CREATE TABLE IF NOT EXISTS pv_forecast ("
                " source TEXT, ts TEXT, pv_w REAL, pv10_w REAL, pv90_w REAL,"
                " PRIMARY KEY(source, ts))")
    # Unveränderliche Forecast-Snapshots für ehrliche Rolling-Origin-Backtests:
    # issue_ts = Erstellungszeit, target_ts = prognostizierter Zielzeitpunkt.
    # Die bisherigen Cache-Tabellen bleiben der schnelle Live-Lesepfad.
    con.execute("CREATE TABLE IF NOT EXISTS pv_forecast_archive ("
                " source TEXT, issue_ts TEXT, target_ts TEXT,"
                " pv_w REAL, pv10_w REAL, pv90_w REAL,"
                " PRIMARY KEY(source, issue_ts, target_ts))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pv_fc_archive_target_issue "
                "ON pv_forecast_archive(target_ts, issue_ts)")
    con.execute("CREATE TABLE IF NOT EXISTS weather_forecast_archive ("
                " issue_ts TEXT, target_ts TEXT, temp_c REAL, radiation_w_m2 REAL,"
                " PRIMARY KEY(issue_ts, target_ts))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_weather_fc_archive_target_issue "
                "ON weather_forecast_archive(target_ts, issue_ts)")
    # Diagnosebasis der Intraday-Korrektur. summary enthält Roh-/angewandten
    # Faktor je Lauf, window die dazu verglichenen Ist-/Basisprognose-Slots.
    con.execute("CREATE TABLE IF NOT EXISTS intraday_correction ("
                " issue_ts TEXT, signal TEXT, window_start_ts TEXT,"
                " raw_ratio REAL, clipped_ratio REAL, applied_ratio REAL,"
                " actual_mean_w REAL, predicted_mean_w REAL,"
                " samples INTEGER, used_samples INTEGER,"
                " PRIMARY KEY(issue_ts, signal))")
    con.execute("CREATE TABLE IF NOT EXISTS intraday_window ("
                " issue_ts TEXT, signal TEXT, target_ts TEXT,"
                " actual_w REAL, predicted_w REAL, eligible INTEGER NOT NULL,"
                " PRIMARY KEY(issue_ts, signal, target_ts))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_intraday_window_target "
                "ON intraday_window(signal, target_ts)")
    # Abruf-Protokoll (je erfolgreichem Solcast-Call) für Budget/Verteilung.
    con.execute("CREATE TABLE IF NOT EXISTS solcast_log ("
                " api_key TEXT, resource TEXT, ts TEXT)")
    # Ist-Temperatur thermischer steuerbarer Lasten (Pool) je Zyklus, für den
    # Verlauf im Dashboard (erwartet vs. echt).
    con.execute("CREATE TABLE IF NOT EXISTS load_temp ("
                " name TEXT, ts TEXT, temp_c REAL, PRIMARY KEY(name, ts))")
    # Solar-Einstrahlung (Open-Meteo shortwave_radiation, W/m² Globalstrahlung)
    # für den solaren Wärmeeintrag thermischer Lasten (Pool).
    con.execute("CREATE TABLE IF NOT EXISTS radiation ("
                " ts TEXT PRIMARY KEY, w_m2 REAL NOT NULL)")
    # Publizierte Heiz-FREIGABE thermischer Lasten je Zyklus (1 = mindestens
    # eine Stufe freigegeben, 0 = sicher aus). Grundlage der Thermomodell-
    # Kalibrierung (ems/pool_calibration.py): nur in sicher-aus-Phasen lässt
    # sich der Wärmehaushalt (Verlust + Solareintrag) sauber fitten - bei
    # Freigabe "an" entscheidet das WP-Thermostat selbst, ob geheizt wird.
    con.execute("CREATE TABLE IF NOT EXISTS load_cmd ("
                " name TEXT, ts TEXT, permit INTEGER, PRIMARY KEY(name, ts))")
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


def write_pv_actual(path: str, mapping: Dict[str, float]) -> int:
    """UPSERT historischer Ist-PV {UTC-ISO -> W} in actuals.pv_w. Andere Spalten
    (house_w/grid_w/…) bleiben unberührt – für den einmaligen PV-Ist-Import."""
    if not mapping:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO actuals(ts, pv_w) VALUES(?, ?) "
        "ON CONFLICT(ts) DO UPDATE SET pv_w=excluded.pv_w",
        [(k, float(v)) for k, v in mapping.items()])
    con.commit()
    con.close()
    return len(mapping)


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
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    return pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)


def write_load_cmd(path: str, ts, name: str, permit: int) -> None:
    """Publizierte Heiz-Freigabe (0/1) einer thermischen Last beim Slot-
    Zeitstempel ablegen (Grundlage der Thermomodell-Kalibrierung)."""
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    con = _con(path)
    con.execute(
        "INSERT INTO load_cmd(name, ts, permit) VALUES(?,?,?) "
        "ON CONFLICT(name, ts) DO UPDATE SET permit=excluded.permit",
        (str(name), key, int(permit)))
    con.commit()
    con.close()


def read_load_cmd(path: str, name: str, start, end, tz: str) -> pd.Series:
    """Heiz-Freigabe [start, end] einer Last als 0/1-Serie (tz-lokal, nur
    tatsächlich geloggte Zyklen - Lücken bleiben Lücken, KEIN Auffüllen:
    'unbekannt' darf beim Kalibrieren nicht als 'aus' gewertet werden)."""
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, permit FROM load_cmd WHERE name = ? AND ts >= ? AND ts <= ? "
            "ORDER BY ts", (str(name), s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    return pd.Series([float(r[1]) for r in rows], index=idx).tz_convert(tz)


def write_load_temp(path: str, ts, name: str, temp_c: float) -> None:
    """Ist-Temperatur einer thermischen steuerbaren Last beim Slot-Zeitstempel
    ablegen (für den erwartet-vs-echt-Verlauf im Dashboard)."""
    if temp_c is None:
        return
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    con = _con(path)
    con.execute(
        "INSERT INTO load_temp(name, ts, temp_c) VALUES(?,?,?) "
        "ON CONFLICT(name, ts) DO UPDATE SET temp_c=excluded.temp_c",
        (str(name), key, float(temp_c)))
    con.commit()
    con.close()


def read_load_temp(path: str, name: str, start, end, tz: str) -> pd.Series:
    """Ist-Temperatur-Verlauf einer Last [start, end) als tz-lokale Serie."""
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, temp_c FROM load_temp WHERE name = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts", (str(name), s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
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
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    hourly = pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)
    # tz aus den (bereits tz-bewussten) Endpunkten ableiten, NICHT zusätzlich
    # tz= übergeben (sonst pytz/zoneinfo-Konflikt in date_range).
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz), freq=freq,
                         inclusive="left")
    if len(grid) == 0:
        return hourly
    step_min = max(1.0, pd.Timedelta(freq).total_seconds() / 60.0)
    limit = max(1, int(120.0 / step_min))
    return (hourly.reindex(hourly.index.union(grid)).interpolate(
            method="time", limit=limit, limit_area="inside").reindex(grid))


def write_radiation(path: str, mapping: Dict[str, float]) -> int:
    """UPSERT stündlicher Solar-Einstrahlung {UTC-ISO -> W/m²}."""
    if not mapping:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO radiation(ts, w_m2) VALUES(?, ?) "
        "ON CONFLICT(ts) DO UPDATE SET w_m2=excluded.w_m2",
        [(k, float(v)) for k, v in mapping.items()])
    con.commit()
    con.close()
    return len(mapping)


def read_radiation(path: str, start, end, tz: str, freq: str) -> pd.Series:
    """Solar-Einstrahlung (W/m²) [start, end) auf das Slot-Raster interpoliert
    (wie read_temperature). Leer, wenn nichts vorhanden."""
    s_utc = (pd.Timestamp(start) - pd.Timedelta(hours=2)).tz_convert("UTC").isoformat()
    e_utc = (pd.Timestamp(end) + pd.Timedelta(hours=2)).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, w_m2 FROM radiation WHERE ts >= ? AND ts < ? ORDER BY ts",
            (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    hourly = pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz), freq=freq,
                         inclusive="left")
    if len(grid) == 0:
        return hourly.clip(lower=0.0)
    step_min = max(1.0, pd.Timedelta(freq).total_seconds() / 60.0)
    limit = max(1, int(120.0 / step_min))
    out = (hourly.reindex(hourly.index.union(grid)).interpolate(
           method="time", limit=limit, limit_area="inside").reindex(grid))
    return out.clip(lower=0.0)   # Einstrahlung ist nie negativ (Interpolationsrand)


def write_spot(path: str, mapping: Dict[str, float]) -> int:
    """UPSERT von Spotpreisen {UTC-ISO -> ct/kWh netto}."""
    if not mapping:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO spot_price(ts, ct) VALUES(?, ?) "
        "ON CONFLICT(ts) DO UPDATE SET ct=excluded.ct",
        [(k, float(v)) for k, v in mapping.items()])
    con.commit()
    con.close()
    return len(mapping)


def last_spot_timestamp(path: str) -> Optional[pd.Timestamp]:
    """Jüngster gespeicherter Spot-Slot (tz-aware UTC), oder None."""
    try:
        con = _con(path)
        row = con.execute("SELECT max(ts) FROM spot_price").fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return pd.Timestamp(row[0])


def read_spot(path: str, start, end, tz: str, slot_minutes: int = 15) -> pd.Series:
    """Spotpreis [start, end) auf das Slot-Raster (ct/kWh netto). Gröbere Quell-
    schritte (stündlich) werden gehalten; Slots NACH dem letzten vorhandenen
    Punkt bleiben NaN (-> lösen die Folgetag-Preisschätzung aus). Leer, wenn
    nichts vorhanden."""
    # etwas Rand links, damit der zuletzt vor `start` bekannte Preis gehalten wird
    s_utc = (pd.Timestamp(start) - pd.Timedelta(hours=2)).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, ct FROM spot_price WHERE ts >= ? AND ts < ? ORDER BY ts",
            (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    src = pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz),
                         freq=f"{slot_minutes}min", inclusive="left")
    if len(grid) == 0:
        return src
    spl = max(1, 60 // slot_minutes)      # Slots je Stunde
    # Ältere, gröbere (stündliche) Quelle auf die Sub-Slots halten (limit) -
    # ABER NICHT über den letzten ECHTEN Datenpunkt hinaus, wenn dieser bereits
    # nativ im Slot-Raster liegt: Energy-Charts liefert die jüngste Historie/
    # Folgetag inzwischen bereits 15-min-genau; ffill(limit=…) kennt nur die
    # ANZAHL der Lücken-Slots, nicht ob dahinter noch echte Daten kommen -> hielt
    # den letzten realen 15-min-Wert fälschlich 3 weitere Slots (bis zu 45 min)
    # über das reale Ende hinaus (z.B. bis 00:30 statt 23:45). Unterscheidung
    # über den Abstand zum vorletzten Punkt: liegt er bei einer vollen Stunde
    # (alte stündliche Quelle), bleibt das Halten über die Stunde wie bisher;
    # liegt er im Slot-Raster (native Auflösung), wird NICHT verlängert.
    allidx = src.index.union(grid)
    held = src.reindex(allidx).ffill(limit=spl - 1)
    if len(src.index) >= 2:
        last_gap = src.index[-1] - src.index[-2]
    else:
        last_gap = pd.Timedelta(hours=1)   # unbekannt -> altes (sicheres) Verhalten
    if last_gap <= pd.Timedelta(minutes=slot_minutes):
        held.loc[held.index > src.index.max()] = float("nan")
    return held.reindex(grid)


def write_pv_forecast(path: str, source: str, mapping: Dict[str, tuple]) -> int:
    """UPSERT einer Solcast-Quelle {UTC-ISO -> (pv_w, pv10_w, pv90_w)}."""
    if not mapping:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO pv_forecast(source, ts, pv_w, pv10_w, pv90_w) "
        "VALUES(?,?,?,?,?) ON CONFLICT(source, ts) DO UPDATE SET "
        "pv_w=excluded.pv_w, pv10_w=excluded.pv10_w, pv90_w=excluded.pv90_w",
        [(source, k, v[0], v[1], v[2]) for k, v in mapping.items()])
    con.commit()
    con.close()
    return len(mapping)


def write_pv_forecast_archive(path: str, source: str, issue_time,
                              mapping: Dict[str, tuple]) -> int:
    """Unveränderlichen PV-Forecast-Snapshot archivieren.

    Nur target >= issue_time wird gespeichert; importierte Historie und bereits
    vergangene Zielwerte sind keine am Origin nutzbaren Zukunftsprognosen.
    """
    if not mapping:
        return 0
    issue = pd.Timestamp(issue_time)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue = issue.tz_convert("UTC")
    issue_iso = issue.isoformat()
    rows = []
    for target, values in mapping.items():
        t = pd.Timestamp(target)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        t = t.tz_convert("UTC")
        if t < issue:
            continue
        rows.append((str(source), issue_iso, t.isoformat(),
                     values[0], values[1], values[2]))
    if not rows:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT OR IGNORE INTO pv_forecast_archive"
        "(source, issue_ts, target_ts, pv_w, pv10_w, pv90_w) "
        "VALUES(?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)


def write_weather_forecast_archive(path: str, issue_time,
                                   temp_mapping: Dict[str, float],
                                   radiation_mapping: Dict[str, float]) -> int:
    """Open-Meteo-Snapshot mit issue_time/target_time archivieren.

    Vergangenheitswerte aus ``past_days`` werden absichtlich ausgelassen.
    """
    issue = pd.Timestamp(issue_time)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue = issue.tz_convert("UTC")
    issue_iso = issue.isoformat()
    rows = []
    for target in set(temp_mapping) | set(radiation_mapping):
        t = pd.Timestamp(target)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        t = t.tz_convert("UTC")
        if t < issue:
            continue
        rows.append((issue_iso, t.isoformat(), temp_mapping.get(target),
                     radiation_mapping.get(target)))
    if not rows:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT OR IGNORE INTO weather_forecast_archive"
        "(issue_ts, target_ts, temp_c, radiation_w_m2) VALUES(?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)


def read_pv_forecast_asof(path: str, issue_time, start, end, tz: str,
                          slot_minutes: int, combine: str,
                          which: str = "pv") -> pd.Series:
    """Je Quelle jüngsten PV-Snapshot mit issue_ts <= Origin lesen."""
    col = {"pv": "pv_w", "p10": "pv10_w", "p90": "pv90_w"}[which]
    agg = "sum" if combine == "sum" else "avg"
    issue_iso = pd.Timestamp(issue_time).tz_convert("UTC").isoformat()
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            f"WITH latest AS ("
            " SELECT source, target_ts, max(issue_ts) issue_ts"
            " FROM pv_forecast_archive"
            " WHERE issue_ts <= ? AND target_ts >= ? AND target_ts < ?"
            " GROUP BY source, target_ts)"
            f" SELECT a.target_ts, {agg}(a.{col})"
            " FROM pv_forecast_archive a JOIN latest l"
            " ON a.source=l.source AND a.target_ts=l.target_ts"
            " AND a.issue_ts=l.issue_ts"
            f" WHERE a.{col} IS NOT NULL GROUP BY a.target_ts ORDER BY a.target_ts",
            (issue_iso, s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    src = pd.Series([r[1] for r in rows], index=idx,
                    dtype="float64").tz_convert(tz)
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz),
                         freq=f"{slot_minutes}min", inclusive="left")
    spl = max(1, 30 // slot_minutes)
    return src.reindex(src.index.union(grid)).ffill(limit=spl - 1).reindex(grid)


def read_weather_forecast_asof(path: str, issue_time, start, end, tz: str,
                               freq: str, field: str = "temp") -> pd.Series:
    """Jüngsten Open-Meteo-Snapshot mit issue_ts <= Origin lesen."""
    col = {"temp": "temp_c", "radiation": "radiation_w_m2"}[field]
    issue_iso = pd.Timestamp(issue_time).tz_convert("UTC").isoformat()
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "WITH latest AS ("
            " SELECT target_ts, max(issue_ts) issue_ts"
            " FROM weather_forecast_archive"
            " WHERE issue_ts <= ? AND target_ts >= ? AND target_ts < ?"
            " GROUP BY target_ts)"
            f" SELECT a.target_ts, a.{col}"
            " FROM weather_forecast_archive a JOIN latest l"
            " ON a.target_ts=l.target_ts AND a.issue_ts=l.issue_ts"
            f" WHERE a.{col} IS NOT NULL ORDER BY a.target_ts",
            (issue_iso, s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    src = pd.Series([r[1] for r in rows], index=idx,
                    dtype="float64").tz_convert(tz)
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz), freq=freq,
                         inclusive="left")
    step_min = max(1.0, pd.Timedelta(freq).total_seconds() / 60.0)
    limit = max(1, int(120.0 / step_min))
    out = src.reindex(src.index.union(grid)).interpolate(
        method="time", limit=limit, limit_area="inside").reindex(grid)
    return out.clip(lower=0.0) if field == "radiation" else out


def write_intraday_diagnostic(path: str, issue_time, signal: str,
                              window_start, details: dict,
                              applied_ratio) -> None:
    """Intraday-Ist/Basisprognose und Faktorentscheidung unveränderlich sichern."""
    issue = pd.Timestamp(issue_time)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue_iso = issue.tz_convert("UTC").isoformat()
    start = pd.Timestamp(window_start)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    observations = details.get("observations")
    con = _con(path)
    con.execute(
        "INSERT OR IGNORE INTO intraday_correction("
        "issue_ts, signal, window_start_ts, raw_ratio, clipped_ratio,"
        "applied_ratio, actual_mean_w, predicted_mean_w, samples, used_samples)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (issue_iso, str(signal), start.tz_convert("UTC").isoformat(),
         details.get("raw_ratio"), details.get("clipped_ratio"), applied_ratio,
         details.get("actual_mean_w"), details.get("predicted_mean_w"),
         details.get("samples", 0), details.get("used_samples", 0)))
    if observations is not None and not observations.empty:
        rows = []
        for ts, row in observations.iterrows():
            target = pd.Timestamp(ts)
            if target.tzinfo is None:
                target = target.tz_localize("UTC")
            rows.append((issue_iso, str(signal),
                         target.tz_convert("UTC").isoformat(),
                         float(row["a"]), float(row["p"]),
                         int(row.get("eligible", 1))))
        con.executemany(
            "INSERT OR IGNORE INTO intraday_window("
            "issue_ts, signal, target_ts, actual_w, predicted_w, eligible)"
            " VALUES(?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def read_pv_forecast(path: str, start, end, tz: str, slot_minutes: int,
                     combine: str, which: str,
                     expected_sources=None) -> pd.Series:
    """Kombinierte PV-Vorhersage [start, end) auf dem Slot-Raster (W).
    which: 'pv' | 'p10' | 'p90'. combine: 'sum' (Arrays addieren) | 'mean'
    (redundante Quellen mitteln). Gröbere Quellschritte (30-min) werden gehalten;
    nach dem letzten Punkt NaN. Leer, wenn nichts vorhanden."""
    col = {"pv": "pv_w", "p10": "pv10_w", "p90": "pv90_w"}[which]
    agg = "sum" if combine == "sum" else "avg"
    s_utc = (pd.Timestamp(start) - pd.Timedelta(hours=1)).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        expected = list(dict.fromkeys(expected_sources or []))
        if expected:
            marks = ",".join("?" for _ in expected)
            raw_rows = con.execute(
                f"SELECT ts, source, {col} FROM pv_forecast "
                f"WHERE ts >= ? AND ts < ? AND {col} IS NOT NULL "
                f"AND source IN ({marks}) ORDER BY ts",
                (s_utc, e_utc, *expected)).fetchall()
            if raw_rows:
                frame = pd.DataFrame(raw_rows, columns=["ts", "source", "value"])
                wide = frame.pivot_table(index="ts", columns="source",
                                         values="value", aggfunc="last")
                wide = wide.reindex(columns=expected).dropna(how="any")
                combined = wide.sum(axis=1) if combine == "sum" else wide.mean(axis=1)
                rows = list(combined.items())
            else:
                rows = []
        else:
            rows = con.execute(
                f"SELECT ts, {agg}({col}) FROM pv_forecast WHERE ts >= ? AND ts < ? "
                f"AND {col} IS NOT NULL GROUP BY ts ORDER BY ts",
                (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    src = pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz),
                         freq=f"{slot_minutes}min", inclusive="left")
    if len(grid) == 0:
        return src
    spl = max(1, 30 // slot_minutes)      # Solcast-Periode 30-min -> Sub-Slots halten
    allidx = src.index.union(grid)
    return src.reindex(allidx).ffill(limit=spl - 1).reindex(grid)


def log_solcast_call(path: str, api_key: str, resource: str, ts_iso: str) -> None:
    con = _con(path)
    con.execute("INSERT INTO solcast_log(api_key, resource, ts) VALUES(?,?,?)",
                (api_key, resource, ts_iso))
    con.commit()
    con.close()


def solcast_calls_since(path: str, api_key: str, since_iso: str) -> int:
    """Zahl der Abrufe dieses Keys seit `since_iso` (UTC-ISO) – für das Tagesbudget."""
    try:
        con = _con(path)
        n = con.execute("SELECT count(*) FROM solcast_log WHERE api_key=? AND ts>=?",
                        (api_key, since_iso)).fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return 0


def last_solcast_fetch(path: str, api_key: str, resource: str) -> Optional[pd.Timestamp]:
    """Zeitpunkt des letzten Abrufs dieser Quelle (tz-aware UTC), oder None."""
    try:
        con = _con(path)
        row = con.execute("SELECT max(ts) FROM solcast_log WHERE api_key=? AND resource=?",
                          (api_key, resource)).fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return pd.Timestamp(row[0])


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
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    return pd.Series([r[1] for r in rows], index=idx, dtype="float64").tz_convert(tz)


def count(path: str) -> int:
    try:
        con = _con(path)
        n = con.execute("SELECT count(*) FROM house_load").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return 0
