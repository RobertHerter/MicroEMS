"""Lokaler 15-min-Hausverbrauchs-Speicher (SQLite) für die Prognose.

Alternative zur InfluxDB als Historienquelle: die 15-min-Hauslast (W) wird per
RSCP aus dem E3DC gefüllt (Backfill + zyklisch) und hier abgelegt. Die
Verbrauchsprognose (forecast.load_history) liest daraus, wenn
config.e3dc_rscp.history_source aktiv ist -> Schritt Richtung Standalone.

Schlüssel = UTC-ISO-Zeitstempel (monoton, DST-sicher). Werte = W (Mittel des
15-min-Fensters).
"""
from __future__ import annotations

import json
import sqlite3
import zlib
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
    # Exakter, bereits aufbereiteter Eingang des Optimierers. Anders als die
    # Provider-Archive enthält dieser Snapshot auch Kalibrierung, Intraday-
    # Korrektur und Preisersatzwerte so, wie sie im produktiven Lauf galten.
    con.execute("CREATE TABLE IF NOT EXISTS optimizer_forecast_snapshots ("
                " issue_ts TEXT PRIMARY KEY, payload BLOB NOT NULL)")
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
    # Zuletzt an den E3DC gesendeter Steuerbefehl (aktueller Slot), als JSON.
    # Beim Dienststart sofort wieder anwendbar, um die Peak-/Steuer-Lücke
    # zwischen sauberem Herunterfahren (Limits freigegeben) und dem ersten
    # Solve zu schließen. Immer nur EINE Zeile (id=1).
    con.execute("CREATE TABLE IF NOT EXISTS last_control ("
                " id INTEGER PRIMARY KEY CHECK(id=1), ts TEXT, cmd_json TEXT)")
    # Rücklesebestätigung der tatsächlich wirksamen E3DC-Limits. Dient als
    # Audit-Verlauf und überlebt Dienst-/Dashboard-Neustarts.
    con.execute("CREATE TABLE IF NOT EXISTS control_verification ("
                " ts TEXT PRIMARY KEY, ok INTEGER, state TEXT, mode TEXT,"
                " message TEXT, expected_json TEXT, actual_json TEXT)")
    # Dauerhafte Laufzeit- und Modellgroessen-Diagnose je Optimierungslauf.
    con.execute("CREATE TABLE IF NOT EXISTS solver_runs ("
                " ts TEXT PRIMARY KEY, seconds REAL, polish_seconds REAL,"
                " slots INTEGER, variables INTEGER, binaries INTEGER,"
                " constraints_count INTEGER, status TEXT, hit_limit INTEGER,"
                " warm_start INTEGER, mip_gap REAL)")
    # Zuletzt publizierter Sollfahrplan und dessen spaetere Ist-Pruefung.
    con.execute("CREATE TABLE IF NOT EXISTS execution_plan ("
                " ts TEXT PRIMARY KEY, issued_at TEXT, grid_w REAL, battery_w REAL,"
                " soc REAL, mode TEXT, charge_limit_w REAL,"
                " discharge_limit_w REAL, grid_charge_w REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS execution_audit ("
                " ts TEXT PRIMARY KEY, checked_at TEXT, ok INTEGER, state TEXT,"
                " message TEXT, planned_json TEXT, actual_json TEXT,"
                " deviations_json TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS pv_source_selection ("
                " ts TEXT PRIMARY KEY, selected TEXT, selected_since TEXT,"
                " reason TEXT, metrics_json TEXT)")
    # Nächtlich gegen die echten E3DC-Zähler validierte Tagesersparnis
    # (savings_check.py --persist). Grundlage der kumulierten, bestätigten
    # Ersparnis im Dashboard. Ein Datensatz je Kalendertag (lokal).
    con.execute("CREATE TABLE IF NOT EXISTS savings_validated ("
                " day TEXT PRIMARY KEY, computed_ts TEXT, n_slots INTEGER,"
                " import_kwh REAL, export_kwh REAL, meter_cost_eur REAL,"
                " baseline_cost_eur REAL, saved_eur REAL,"
                " tracker_delta_eur REAL, balance_ok INTEGER,"
                " baseline_end_soc_wh REAL)")
    # Migration: Spalte in bereits bestehenden Tabellen ergänzen (idempotent).
    try:
        con.execute("ALTER TABLE savings_validated "
                    "ADD COLUMN baseline_end_soc_wh REAL")
    except sqlite3.OperationalError:
        pass
    con.commit()
    return con


def latest_pv_forecast_issue(path: str, sources=None) -> Optional[pd.Timestamp]:
    """Erstellungszeit der ältesten Komponente des jüngsten PV-Quellsatzes."""
    try:
        con = _con(path)
        srcs = list(dict.fromkeys(sources or []))
        if srcs:
            marks = ",".join("?" for _ in srcs)
            rows = con.execute(
                f"SELECT source, MAX(issue_ts) FROM pv_forecast_archive "
                f"WHERE source IN ({marks}) GROUP BY source", srcs).fetchall()
            if len(rows) != len(srcs):
                value = None
            else:
                value = min(row[1] for row in rows if row[1])
        else:
            row = con.execute(
                "SELECT MAX(issue_ts) FROM pv_forecast_archive").fetchone()
            value = row[0] if row else None
        con.close()
    except Exception:
        value = None
    return pd.Timestamp(value) if value else None


def latest_weather_forecast_issue(path: str) -> Optional[pd.Timestamp]:
    try:
        con = _con(path)
        row = con.execute(
            "SELECT MAX(issue_ts) FROM weather_forecast_archive").fetchone()
        con.close()
        return pd.Timestamp(row[0]) if row and row[0] else None
    except Exception:
        return None


def read_pv_source_selection(path: str) -> Optional[dict]:
    try:
        con = _con(path)
        row = con.execute(
            "SELECT ts, selected, selected_since, reason, metrics_json "
            "FROM pv_source_selection ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        row = None
    if not row:
        return None
    return {"ts": row[0], "selected": row[1], "selected_since": row[2],
            "reason": row[3], "metrics": json.loads(row[4] or "{}")}


def write_pv_source_selection(path: str, ts, selected: str, reason: str,
                              metrics: dict) -> dict:
    now = pd.Timestamp(ts)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    now_iso = now.tz_convert("UTC").isoformat()
    previous = read_pv_source_selection(path)
    since = (previous.get("selected_since") if previous and
             previous.get("selected") == selected else now_iso)
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO pv_source_selection VALUES(?,?,?,?,?)",
        (now_iso, str(selected), since, str(reason),
         json.dumps(metrics or {}, separators=(",", ":"))))
    con.commit()
    con.close()
    return {"ts": now_iso, "selected": selected, "selected_since": since,
            "reason": reason, "metrics": metrics}


def write_savings_validated(path: str, day: str, r: dict) -> None:
    """Eine gegen die Zähler validierte Tagesersparnis ablegen (idempotent je
    Kalendertag). r = reconcile()-Ergebnis (savings_validate)."""
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO savings_validated VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (str(day), pd.Timestamp(r.get("computed_ts")).isoformat()
         if r.get("computed_ts") else None,
         int(r.get("n_slots", 0)),
         r["meter"]["import_kwh"], r["meter"]["export_kwh"],
         r["meter"]["net_cost_eur"], r["baseline"]["net_cost_eur"],
         r["saved_eur"], (r.get("tracker") or {}).get("cost_delta_eur"),
         1 if r["balance"]["ok"] else 0,
         r["baseline"].get("end_soc_wh")))
    con.commit()
    con.close()


def read_savings_baseline_soc(path: str, day: str) -> Optional[float]:
    """Baseline-End-SoC (Wh) eines gespeicherten Tages - zum Verketten der
    durchgehenden Ohne-EMS-Bilanz über Tagesgrenzen. None, wenn nicht vorhanden."""
    try:
        con = _con(path)
        row = con.execute("SELECT baseline_end_soc_wh FROM savings_validated "
                          "WHERE day = ?", (str(day),)).fetchone()
        con.close()
    except Exception:
        return None
    return float(row[0]) if row and row[0] is not None else None


def read_savings_validated(path: str, start_day: Optional[str] = None) -> pd.DataFrame:
    """Validierte Tagesersparnisse (optional ab start_day, YYYY-MM-DD) lesen."""
    try:
        con = _con(path)
        if start_day:
            rows = con.execute(
                "SELECT day, n_slots, import_kwh, export_kwh, meter_cost_eur,"
                " baseline_cost_eur, saved_eur, tracker_delta_eur, balance_ok"
                " FROM savings_validated WHERE day >= ? ORDER BY day",
                (str(start_day),)).fetchall()
        else:
            rows = con.execute(
                "SELECT day, n_slots, import_kwh, export_kwh, meter_cost_eur,"
                " baseline_cost_eur, saved_eur, tracker_delta_eur, balance_ok"
                " FROM savings_validated ORDER BY day").fetchall()
        con.close()
    except Exception:
        rows = []
    cols = ["day", "n_slots", "import_kwh", "export_kwh", "meter_cost_eur",
            "baseline_cost_eur", "saved_eur", "tracker_delta_eur", "balance_ok"]
    return pd.DataFrame(rows, columns=cols)


def write_solver_run(path: str, ts, result) -> None:
    """Solver-Lauf persistent sichern (auch ueber Dienstneustarts hinweg)."""
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO solver_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (key, float(result.solver_seconds), float(result.solver_polish_seconds),
         int(result.solver_slots), int(result.solver_variables),
         int(result.solver_binaries), int(result.solver_constraints),
         str(result.status), int(bool(result.solver_hit_limit)),
         int(bool(result.solver_warm_start)), result.solver_mip_gap))
    con.commit()
    con.close()


def read_solver_runs(path: str, limit: int = 24) -> list[dict]:
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, seconds, polish_seconds, slots, variables, binaries, "
            "constraints_count, status, hit_limit, warm_start, mip_gap "
            "FROM solver_runs ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        con.close()
    except Exception:
        rows = []
    names = ("ts", "seconds", "polish_seconds", "slots", "variables",
             "binaries", "constraints", "status", "hit_limit", "warm_start",
             "mip_gap")
    return [dict(zip(names, row)) for row in rows]


def write_execution_plan(path: str, issued_at, table: pd.DataFrame,
                         initial_soc_percent: float | None = None) -> int:
    """Publizierten Sollfahrplan fuer den spaeteren Ist-Vergleich sichern."""
    if table is None or table.empty:
        return 0
    issue = pd.Timestamp(issued_at).tz_convert("UTC").isoformat()
    previous_soc = initial_soc_percent
    rows = []
    for ts, row in table.iterrows():
        key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
        grid = float(row.get("grid_import_w", 0.0) or 0.0) - float(
            row.get("grid_export_w", 0.0) or 0.0)
        battery = (float(row.get("batt_dc_charge_w", 0.0) or 0.0)
                   + float(row.get("batt_ac_charge_w", 0.0) or 0.0)
                   - float(row.get("batt_discharge_w", 0.0) or 0.0))
        rows.append((key, issue, grid, battery, previous_soc,
                     str(row.get("mode", "auto")),
                     row.get("batt_charge_limit_w"),
                     row.get("batt_discharge_limit_w"),
                     row.get("batt_grid_charge_w")))
        value = row.get("house_soc_percent")
        if value is not None and pd.notna(value):
            previous_soc = float(value)
    con = _con(path)
    con.executemany(
        "INSERT OR REPLACE INTO execution_plan VALUES(?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)


def read_execution_plan_slot(path: str, ts) -> Optional[dict]:
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        row = con.execute(
            "SELECT issued_at, grid_w, battery_w, soc, mode, charge_limit_w, "
            "discharge_limit_w, grid_charge_w FROM execution_plan WHERE ts=?",
            (key,)).fetchone()
        con.close()
    except Exception:
        row = None
    if not row:
        return None
    names = ("issued_at", "grid_w", "battery_w", "soc", "mode",
             "charge_limit_w", "discharge_limit_w", "grid_charge_w")
    return dict(zip(names, row))


def write_execution_audit(path: str, ts, audit: dict) -> None:
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    checked = pd.Timestamp(audit.get("checked_at", pd.Timestamp.now(tz="UTC")))
    if checked.tzinfo is None:
        checked = checked.tz_localize("UTC")
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO execution_audit VALUES(?,?,?,?,?,?,?,?)",
        (key, checked.tz_convert("UTC").isoformat(), int(bool(audit.get("ok"))),
         str(audit.get("state", "unknown")), str(audit.get("message", "")),
         json.dumps(audit.get("planned") or {}, separators=(",", ":")),
         json.dumps(audit.get("actual") or {}, separators=(",", ":")),
         json.dumps(audit.get("deviations") or {}, separators=(",", ":"))))
    con.commit()
    con.close()


def read_execution_audits(path: str, limit: int = 8) -> list[dict]:
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, checked_at, ok, state, message, planned_json, actual_json, "
            "deviations_json FROM execution_audit ORDER BY ts DESC LIMIT ?",
            (int(limit),)).fetchall()
        con.close()
    except Exception:
        rows = []
    return [{"ts": r[0], "checked_at": r[1], "ok": bool(r[2]),
             "state": r[3], "message": r[4],
             "planned": json.loads(r[5] or "{}"),
             "actual": json.loads(r[6] or "{}"),
             "deviations": json.loads(r[7] or "{}")} for r in rows]


def write_last_control(path: str, ts, mapping: Dict[str, float]) -> None:
    """Aktuellen Steuerbefehl (Slot-Zeitstempel + Feld->Wert) sichern."""
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    con = _con(path)
    con.execute(
        "INSERT INTO last_control(id, ts, cmd_json) VALUES(1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, cmd_json=excluded.cmd_json",
        (key, json.dumps({k: (None if v is None else float(v))
                          for k, v in mapping.items()})))
    con.commit()
    con.close()


def read_last_control(path: str, tz: str):
    """Zuletzt gesicherten Steuerbefehl lesen. Rückgabe (ts_lokal, mapping) oder
    (None, None)."""
    try:
        con = _con(path)
        row = con.execute("SELECT ts, cmd_json FROM last_control WHERE id=1").fetchone()
        con.close()
    except Exception:
        return None, None
    if not row or not row[0]:
        return None, None
    try:
        ts = pd.Timestamp(row[0]).tz_convert(tz)
        return ts, json.loads(row[1])
    except Exception:
        return None, None


def write_control_verification(path: str, status: dict) -> None:
    """Ergebnis einer E3DC-Rücklesekontrolle protokollieren."""
    ts = pd.Timestamp(status.get("checked_at", pd.Timestamp.now(tz="UTC")))
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ok = status.get("ok")
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO control_verification("
        "ts, ok, state, mode, message, expected_json, actual_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts.tz_convert("UTC").isoformat(), None if ok is None else int(bool(ok)),
         str(status.get("state", "unknown")), str(status.get("mode", "")),
         str(status.get("message", "")),
         json.dumps(status.get("expected") or {}, separators=(",", ":")),
         json.dumps(status.get("actual") or {}, separators=(",", ":"))))
    con.commit()
    con.close()


def read_latest_control_verification(path: str, tz: str):
    """Letzte E3DC-Rücklesebestätigung oder ``None`` lesen."""
    try:
        con = _con(path)
        row = con.execute(
            "SELECT ts, ok, state, mode, message, expected_json, actual_json "
            "FROM control_verification ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        row = None
    if not row:
        return None
    return {
        "checked_at": pd.Timestamp(row[0]).tz_convert(tz).isoformat(),
        "ok": None if row[1] is None else bool(row[1]),
        "state": row[2], "mode": row[3], "message": row[4],
        "expected": json.loads(row[5] or "{}"),
        "actual": json.loads(row[6] or "{}"),
    }
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


def write_optimizer_forecast_archive(
        path: str, issue_time, series: Dict[str, pd.Series],
        estimated: Optional[Dict[str, pd.Series]] = None) -> int:
    """Einen unveränderlichen Snapshot der Optimierer-Eingänge speichern.

    Alle Signale erhalten dieselbe hochauflösende ``issue_ts``. Dadurch kann
    ein Backtest später genau einen konsistenten, vor dem Origin bekannten
    Satz laden, statt Werte aus verschiedenen Prognoseläufen zu mischen.
    """
    issue = pd.Timestamp(issue_time)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue_iso = issue.tz_convert("UTC").isoformat()
    clean = {str(name): pd.Series(values, dtype="float64")
             for name, values in series.items()}
    if not clean:
        return 0
    index = pd.DatetimeIndex([])
    for values in clean.values():
        index = index.union(pd.DatetimeIndex(values.index))
    if index.empty:
        return 0
    index = index.sort_values()
    timestamps = []
    for target in index:
        ts = pd.Timestamp(target)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        timestamps.append(ts.tz_convert("UTC").isoformat())
    values_payload = {}
    count = 0
    for name, values in clean.items():
        aligned = values.reindex(index)
        encoded = [None if pd.isna(value) else float(value)
                   for value in aligned]
        values_payload[name] = encoded
        count += sum(value is not None for value in encoded)
    estimated_payload = {}
    for name, values in (estimated or {}).items():
        estimated_payload[str(name)] = [
            bool(value) if not pd.isna(value) else False
            for value in pd.Series(values).reindex(index)]
    payload = zlib.compress(json.dumps({
        "timestamps": timestamps,
        "series": values_payload,
        "estimated": estimated_payload,
    }, separators=(",", ":"), allow_nan=False).encode("utf-8"), level=6)
    con = _con(path)
    con.execute(
        "INSERT OR IGNORE INTO optimizer_forecast_snapshots(issue_ts, payload) "
        "VALUES(?, ?)", (issue_iso, sqlite3.Binary(payload)))
    con.commit()
    con.close()
    return count


def read_optimizer_forecast_asof(path: str, issue_time, start, end, tz: str):
    """Jüngsten vollständigen Optimierer-Snapshot vor ``issue_time`` lesen.

    Rückgabe ``(snapshot_issue, frame)``. Der DataFrame enthält je Signal eine
    Spalte sowie ``<signal>_estimated``. Es wird bewusst nur EINE issue_ts
    ausgewählt; fehlende Slots bleiben damit sichtbar und werden nicht aus
    neueren oder älteren Läufen ergänzt.
    """
    origin = pd.Timestamp(issue_time)
    if origin.tzinfo is None:
        origin = origin.tz_localize("UTC")
    origin_iso = origin.tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        row = con.execute(
            "SELECT issue_ts, payload FROM optimizer_forecast_snapshots "
            "WHERE issue_ts <= ? ORDER BY issue_ts DESC LIMIT 1",
            (origin_iso,)).fetchone()
        con.close()
    except Exception:
        row = None
    if not row:
        return None, pd.DataFrame()
    selected, blob = row
    try:
        payload = json.loads(zlib.decompress(blob).decode("utf-8"))
        idx = pd.to_datetime(payload["timestamps"], utc=True,
                             format="ISO8601").tz_convert(tz)
        values = pd.DataFrame(payload["series"], index=idx, dtype="float64")
        for name, flags in payload.get("estimated", {}).items():
            values[f"{name}_estimated"] = pd.Series(
                flags, index=idx, dtype="bool")
        begin, finish = pd.Timestamp(start).tz_convert(tz), pd.Timestamp(end).tz_convert(tz)
        values = values[(values.index >= begin) & (values.index < finish)]
    except Exception:
        return None, pd.DataFrame()
    return pd.Timestamp(selected).tz_convert(tz), values.sort_index()


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
                     sources=None, require_complete: bool = False,
                     expected_sources=None) -> pd.Series:
    """Kombinierte PV-Vorhersage [start, end) auf dem Slot-Raster (W).
    which: 'pv' | 'p10' | 'p90'. combine: 'sum' (Arrays addieren) | 'mean'
    (redundante Quellen mitteln). Gröbere Quellschritte werden ZEITLICH auf das
    Slot-Raster interpoliert (auflösungsagnostisch: Solcast 30-min ODER pvlib
    stündlich); nach dem letzten Punkt NaN. Leer, wenn nichts vorhanden.

    sources: NUR diese Quellen einbeziehen (Filter). WICHTIG, seit Solcast und
    das pvlib-Schattenmodell GLEICHZEITIG in dieselbe Tabelle schreiben - ohne
    Filter würde ein blindes SUM(pv_w) GROUP BY ts beide Quellenarten addieren
    (an gemeinsamen Zeitstempeln -> Verdopplung/Sägezahn). require_complete:
    nur Zeitpunkte behalten, an denen ALLE Quellen liefern (dropna how=any).
    expected_sources: veralteter Alias (filtern UND require_complete zugleich)."""
    col = {"pv": "pv_w", "p10": "pv10_w", "p90": "pv90_w"}[which]
    agg = "sum" if combine == "sum" else "avg"
    if expected_sources is not None:          # Rückwärtskompatibler Alias
        sources, require_complete = expected_sources, True
    s_utc = (pd.Timestamp(start) - pd.Timedelta(hours=1)).tz_convert("UTC").isoformat()
    # Rechts einen Quellschritt mitlesen: Der Slot direkt vor ``end`` braucht
    # bei 30-/60-minütlichen Quellen den Stützpunkt exakt auf ``end`` zur
    # Interpolation. Die frühere SQL-Grenze < end ließ deshalb abhängig von
    # der Viertelstunde genau den letzten PV- und p10-Slot als NaN stehen.
    # Das Ausgaberaster bleibt [start, end); limit_area="inside" extrapoliert
    # weiterhin niemals über den letzten echten Quellwert hinaus.
    e_utc = (pd.Timestamp(end) + pd.Timedelta(hours=1)).tz_convert(
        "UTC").isoformat()
    try:
        con = _con(path)
        srcs = list(dict.fromkeys(sources or []))
        if srcs:
            marks = ",".join("?" for _ in srcs)
            raw_rows = con.execute(
                f"SELECT ts, source, {col} FROM pv_forecast "
                f"WHERE ts >= ? AND ts < ? AND {col} IS NOT NULL "
                f"AND source IN ({marks}) ORDER BY ts",
                (s_utc, e_utc, *srcs)).fetchall()
            if raw_rows:
                frame = pd.DataFrame(raw_rows, columns=["ts", "source", "value"])
                wide = frame.pivot_table(index="ts", columns="source",
                                         values="value", aggfunc="last")
                wide = wide.reindex(columns=srcs)
                if require_complete:
                    # nur Zeitpunkte, an denen ALLE Quellen liefern
                    wide = wide.dropna(how="any")
                combined = (wide.sum(axis=1) if combine == "sum"
                            else wide.mean(axis=1))
                rows = list(combined.dropna().items())
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
    # Zeitliche Interpolation auf das Slot-Raster: bridge't sowohl Solcasts
    # 30-min als auch pvlibs stündliche Quellschritte glatt. limit deckt bis
    # ~90 min Lücke ab; limit_area="inside" verhindert Extrapolation über den
    # letzten echten Punkt hinaus (danach bleibt NaN -> löst die Schätzung /
    # Frische-Erkennung aus, wie bisher). ffill(limit=1) hielt vorher nur EINEN
    # Sub-Slot -> bei Stundenwerten NaN-Löcher alle 30 min (Zackenkurve).
    step_min = max(1.0, float(slot_minutes))
    limit = max(1, int(90.0 / step_min))
    allidx = src.index.union(grid)
    return (src.reindex(allidx).interpolate(method="time", limit=limit,
                                            limit_area="inside").reindex(grid))


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
