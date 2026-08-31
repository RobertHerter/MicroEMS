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
import logging
import sqlite3
import zlib
from typing import Dict, Optional

import numpy as np
import pandas as pd
from .loads import load_power_columns

log = logging.getLogger("ems.local_history")


def _con(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS house_load ("
                " ts TEXT PRIMARY KEY, w REAL NOT NULL)")
    # Live-Ist-Werte des E3DC je Zyklus (Ersatz für die InfluxDB-Ist-Signale).
    con.execute("CREATE TABLE IF NOT EXISTS actuals ("
                " ts TEXT PRIMARY KEY, pv_w REAL, house_w REAL, grid_w REAL,"
                " battery_w REAL, soc REAL)")
    # Erweiterte BMS-/RSCP-Diagnose je Optimierungslauf. Bewusst getrennt von
    # ``actuals``: Der operative SoC bleibt dort das stabile Standardsignal,
    # während diese Rohwerte Firmware-/Batterie-spezifisch fehlen dürfen.
    con.execute("CREATE TABLE IF NOT EXISTS battery_diagnostics ("
                " ts TEXT PRIMARY KEY, soc_operational REAL, soc_ems REAL,"
                " rsoc_real REAL, usable_capacity_ah REAL,"
                " usable_remaining_capacity_ah REAL,"
                " full_charge_capacity_ah REAL, remaining_capacity_ah REAL,"
                " voltage_v REAL, current_a REAL,"
                " specified_capacity_wh REAL, soc_source TEXT)")
    # Hochaufgelöste E3DC-Livewerte für den Kurzfrist-Nowcast. Im Gegensatz zu
    # ``actuals`` (ein Diagnose-Snapshot je Optimierungszyklus) werden diese
    # Werte etwa alle fünf Sekunden geschrieben und später zeitgewichtet auf
    # das EMS-Slotraster integriert.
    con.execute("CREATE TABLE IF NOT EXISTS live_samples ("
                " ts TEXT PRIMARY KEY, pv_w REAL, house_w REAL, grid_w REAL,"
                " battery_w REAL, wallbox_w REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_samples_ts "
                "ON live_samples(ts)")
    # DC-Leistung je PV-Strang, als SLOTMITTEL. poll() fasst alle Straenge zu
    # einem Wert zusammen; getrennt lassen sich Ausrichtung und Neigung je Feld
    # bestimmen (aus der Summe ist das unterbestimmt) und die Prognoseguete je
    # Feld messen. Bewusst Slotmittel statt Momentanwerte - eine Stichprobe am
    # Slotanfang liegt auf der Vormittagsflanke systematisch daneben.
    con.execute("CREATE TABLE IF NOT EXISTS pv_strings ("
                " ts TEXT NOT NULL, idx INTEGER NOT NULL, power_w REAL,"
                " PRIMARY KEY (ts, idx))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pv_strings_ts "
                "ON pv_strings(ts)")
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
    # Deutschlandweite Wetter-Indizes (ems/gridweather.py) als Treiber des
    # Börsenpreises: gewichtete Windleistung, Globalstrahlung und Temperatur.
    # Prognosewerte werden je Zyklus überschrieben, die Historie kommt per
    # Backfill aus dem ERA5-Archiv und ist die Lerngrundlage des Preismodells.
    con.execute("CREATE TABLE IF NOT EXISTS grid_weather ("
                " ts TEXT PRIMARY KEY, wind_index REAL, solar_index REAL,"
                " temp_index REAL)")
    # Publizierte Heiz-FREIGABE thermischer Lasten je Zyklus (1 = mindestens
    # eine Stufe freigegeben, 0 = sicher aus). Grundlage der Thermomodell-
    # Kalibrierung (ems/pool_calibration.py): nur in sicher-aus-Phasen lässt
    # sich der Wärmehaushalt (Verlust + Solareintrag) sauber fitten - bei
    # Freigabe "an" entscheidet das WP-Thermostat selbst, ob geheizt wird.
    con.execute("CREATE TABLE IF NOT EXISTS load_cmd ("
                " name TEXT, ts TEXT, permit INTEGER, PRIMARY KEY(name, ts))")
    # Echte Rückmeldung je thermischer Stufe (Kompressorstatus/Leistung) und
    # gesendeter Befehl. Trennt Heizfreigabe klar vom realen Betrieb.
    con.execute("CREATE TABLE IF NOT EXISTS load_feedback ("
                " name TEXT, stage TEXT, ts TEXT, actual_on INTEGER,"
                " power_w REAL, fresh INTEGER NOT NULL, age_seconds REAL,"
                " PRIMARY KEY(name, stage, ts))")
    con.execute("CREATE TABLE IF NOT EXISTS load_stage_cmd ("
                " name TEXT, stage TEXT, ts TEXT, commanded_on INTEGER,"
                " PRIMARY KEY(name, stage, ts))")
    con.execute("CREATE TABLE IF NOT EXISTS thermal_calibration ("
                " name TEXT, ts TEXT, status TEXT, n_windows INTEGER, r2 REAL,"
                " old_json TEXT, fitted_json TEXT, applied_json TEXT,"
                " message TEXT, PRIMARY KEY(name, ts))")
    # Bedien- und Systemereignisse fuer das Dashboard. Bewusst kompakt und
    # lokal: keine Zugangsdaten/Payloads, nur nachvollziehbare Aktionen.
    con.execute("CREATE TABLE IF NOT EXISTS dashboard_event ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                " kind TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL,"
                " details_json TEXT NOT NULL DEFAULT '{}')")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dashboard_event_ts "
                "ON dashboard_event(ts DESC)")
    # Rollierender Verlauf der Debug-Schnappschüsse (komprimiertes JSON je Lauf),
    # damit auch ein ÄLTERER infeasibler/falscher Plan mit allen Eingaben zum
    # Reproduzieren versendet werden kann - nicht nur der letzte. Enthält keine
    # Zugangsdaten (die Config im Payload ist bereits bereinigt).
    con.execute("CREATE TABLE IF NOT EXISTS debug_snapshot ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, generated TEXT NOT NULL,"
                " status TEXT, infeasible INTEGER DEFAULT 0, reason TEXT,"
                " n_violations INTEGER DEFAULT 0, payload BLOB NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_debug_snapshot_gen "
                "ON debug_snapshot(generated DESC)")
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
                " discharge_limit_w REAL, grid_charge_w REAL, details_json TEXT)")
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
        pass   # Spalte existiert bereits - erwartet
    try:
        con.execute("ALTER TABLE execution_plan ADD COLUMN details_json TEXT")
    except sqlite3.OperationalError:
        pass   # Spalte existiert bereits - erwartet
    con.execute("CREATE TABLE IF NOT EXISTS forecast_accuracy_daily ("
                " day TEXT PRIMARY KEY, computed_ts TEXT,"
                " pv_wape REAL, pv_bias_w REAL, pv_n INTEGER,"
                " load_wape REAL, load_bias_w REAL, load_n INTEGER)")
    # Unveränderliche Wochenstände der Prognosekalibrierung. Damit zeigt das
    # Dashboard nicht nur den letzten YAML-Stand, sondern auch, wann sich
    # Faktoren und Unsicherheitsbänder verändert haben.
    con.execute("CREATE TABLE IF NOT EXISTS calibration_history ("
                " ts TEXT PRIMARY KEY, report_json TEXT NOT NULL,"
                " profile_json TEXT NOT NULL)")
    con.commit()
    return con


def write_forecast_accuracy(path: str, day: str, acc: dict) -> None:
    """Tages-Prognosegüte (WAPE/Bias PV+Last) idempotent je Kalendertag ablegen.
    `acc` = Rückgabe von observability.forecast_accuracy."""
    pv = acc.get("pv") or {}
    load = acc.get("load") or {}
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO forecast_accuracy_daily VALUES(?,?,?,?,?,?,?,?)",
        (str(day), pd.Timestamp.now(tz="UTC").isoformat(),
         pv.get("wape_pct"), pv.get("bias_w"), int(pv.get("n") or 0),
         load.get("wape_pct"), load.get("bias_w"), int(load.get("n") or 0)))
    con.commit()
    con.close()


def read_forecast_accuracy(path: str, days: int = 30) -> list:
    """Letzte `days` Tages-Prognosegüte-Punkte (aufsteigend) für den Trend."""
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT day, pv_wape, pv_bias_w, load_wape, load_bias_w"
            " FROM forecast_accuracy_daily ORDER BY day DESC LIMIT ?",
            (int(days),)).fetchall()
        con.close()
    except Exception:
        rows = []
    cols = ["day", "pv_wape", "pv_bias_w", "load_wape", "load_bias_w"]
    return [dict(zip(cols, r)) for r in reversed(rows)]


def write_calibration_snapshot(path: str, generated, report: dict,
                               profile: Optional[dict] = None) -> None:
    """Einen Kalibrierungslauf unveränderlich und idempotent archivieren."""
    stamp = pd.Timestamp(generated)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    stamp = stamp.tz_convert("UTC").isoformat()
    con = _con(path)
    con.execute(
        "INSERT OR IGNORE INTO calibration_history"
        "(ts,report_json,profile_json) VALUES(?,?,?)",
        (stamp, json.dumps(report or {}, default=str, separators=(",", ":")),
         json.dumps(profile or {}, default=str, separators=(",", ":"))))
    con.commit()
    con.close()


def read_calibration_history(path: str, limit: int = 26) -> list:
    """Letzte Kalibrierungsstände chronologisch aufsteigend lesen."""
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts,report_json,profile_json FROM calibration_history "
            "ORDER BY ts DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        con.close()
    except Exception:
        rows = []
    out = []
    for stamp, report_text, profile_text in reversed(rows):
        try:
            out.append({
                "generated": stamp,
                "report": json.loads(report_text),
                "profile": json.loads(profile_text),
            })
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return out


def latest_forecast_accuracy_day(path: str) -> Optional[str]:
    """Jüngster gespeicherter Prognosegüte-Tag (YYYY-MM-DD) oder None."""
    try:
        con = _con(path)
        row = con.execute(
            "SELECT day FROM forecast_accuracy_daily ORDER BY day DESC LIMIT 1"
        ).fetchone()
        con.close()
    except Exception:
        row = None
    return row[0] if row else None


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
                         initial_soc_percent: float | None = None,
                         static_export_limit_w: float | None = None,
                         dashboard_series: Optional[Dict[str, pd.Series]] = None) -> int:
    """Publizierten Sollfahrplan fuer den spaeteren Ist-Vergleich sichern.

    Zukunftsslots werden bei jeder Neuberechnung aktualisiert. Ein bereits
    begonnener Slot wird dagegen nur beim ersten Lauf innerhalb dieses Slots
    festgeschrieben. Sonst würde ein Restart/Recalc kurz vor Slotende den
    ursprünglichen, fast den ganzen Slot wirksamen Plan rückwirkend ersetzen
    und im Dashboard wie ein Soll/Ist-Versatz aussehen.
    """
    if table is None or table.empty:
        return 0
    issue = pd.Timestamp(issued_at).tz_convert("UTC").isoformat()
    previous_soc = initial_soc_percent
    rows = []
    load_cols = load_power_columns(table.columns)
    dashboard_cols = {
        "house_load_w", "house_load_p10_w", "house_load_p90_w",
        "pv_w", "pv10_w", "pv90_w", "price_ct_kwh", "price_estimated",
        "feedin_ct_kwh", "spot_price_ct_kwh", "house_soc_percent",
        "batt_dc_charge_w", "batt_ac_charge_w", "batt_discharge_w",
        "batt_grid_discharge_w", "batt_charge_limit_w",
        "batt_discharge_limit_w", "batt_grid_charge_w", "car_charge_w",
        "grid_import_w", "grid_export_w", "export_line_w", "pv_curtail_w",
        "mode", "decision_reason", "execution_path", "execution_label",
        "execution_detail", "decision_energy_kwh", "decision_value_ct",
        "decision_reference_time",
    }
    dashboard_cols.update(name for name in table.columns
                          if name.startswith("load_"))
    extra = {str(name): pd.Series(values).reindex(table.index)
             for name, values in (dashboard_series or {}).items()}

    def _json_value(value):
        if value is None or (not isinstance(value, (str, bool))
                             and pd.isna(value)):
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)

    for ts, row in table.iterrows():
        key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
        grid = float(row.get("grid_import_w", 0.0) or 0.0) - float(
            row.get("grid_export_w", 0.0) or 0.0)
        battery = (float(row.get("batt_dc_charge_w", 0.0) or 0.0)
                   + float(row.get("batt_ac_charge_w", 0.0) or 0.0)
                   - float(row.get("batt_discharge_w", 0.0) or 0.0))
        total_load = (float(row.get("house_load_w", 0.0) or 0.0)
                      + float(row.get("car_charge_w", 0.0) or 0.0)
                      + sum(float(row.get(name, 0.0) or 0.0) for name in load_cols))
        line = row.get("export_line_w")
        export_limit = (float(line) if line is not None and pd.notna(line)
                        else static_export_limit_w)
        details = {
            "pv_w": float(row.get("pv_w", 0.0) or 0.0),
            "load_w": total_load,
            "grid_export_w": float(row.get("grid_export_w", 0.0) or 0.0),
            "export_limit_w": export_limit,
            "pv_curtail_w": float(row.get("pv_curtail_w", 0.0) or 0.0),
            "execution_path": row.get("execution_path"),
            "execution_label": row.get("execution_label"),
        }
        dashboard = {
            name: _json_value(row.get(name))
            for name in dashboard_cols if name in row.index
        }
        for name, values in extra.items():
            value = values.get(ts)
            if value is not None and not pd.isna(value):
                dashboard[name] = _json_value(value)
        details["dashboard"] = {
            name: value for name, value in dashboard.items()
            if value is not None
        }
        rows.append((key, issue, grid, battery, previous_soc,
                     str(row.get("mode", "auto")),
                     row.get("batt_charge_limit_w"),
                     row.get("batt_discharge_limit_w"),
                     row.get("batt_grid_charge_w"),
                     json.dumps(details, separators=(",", ":"))))
        value = row.get("house_soc_percent")
        if value is not None and pd.notna(value):
            previous_soc = float(value)
    con = _con(path)
    replace_rows = []
    for values in rows:
        target = pd.Timestamp(values[0])
        issued = pd.Timestamp(values[1])
        if target <= issued:
            existing = con.execute(
                "SELECT issued_at FROM execution_plan WHERE ts=?",
                (values[0],)).fetchone()
            # Ein Eintrag aus einem früheren Slot war nur eine Zukunftsplanung
            # und darf beim tatsächlichen Slotbeginn einmal ersetzt werden.
            # Ein Plan, der bereits innerhalb des Zielslots publiziert wurde,
            # bleibt dagegen als historisches Soll unverändert.
            if existing:
                previous_issue = pd.Timestamp(existing[0])
                if previous_issue >= target:
                    continue
        replace_rows.append(values)
    con.executemany(
        "INSERT OR REPLACE INTO execution_plan("
        "ts,issued_at,grid_w,battery_w,soc,mode,charge_limit_w,"
        "discharge_limit_w,grid_charge_w,details_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)", replace_rows)
    con.commit()
    con.close()
    return len(replace_rows)


def read_execution_plan_slot(path: str, ts) -> Optional[dict]:
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        row = con.execute(
            "SELECT issued_at, grid_w, battery_w, soc, mode, charge_limit_w, "
            "discharge_limit_w, grid_charge_w, details_json "
            "FROM execution_plan WHERE ts=?",
            (key,)).fetchone()
        con.close()
    except Exception:
        row = None
    if not row:
        return None
    names = ("issued_at", "grid_w", "battery_w", "soc", "mode",
             "charge_limit_w", "discharge_limit_w", "grid_charge_w",
             "details_json")
    out = dict(zip(names, row))
    details = json.loads(out.pop("details_json") or "{}")
    out.update(details)
    return out


def read_execution_plan_range(path: str, start, end, tz: str) -> pd.DataFrame:
    """Je Zielslot den zuletzt publizierten Sollfahrplan fürs Dashboard lesen.

    Neuere Einträge enthalten die vollständigen Prognose-/Steuerspalten im
    ``dashboard``-Teil von details_json. Ältere Einträge werden aus den schon
    vorhandenen aggregierten Werten bestmöglich rekonstruiert.
    """
    begin = pd.Timestamp(start).tz_convert("UTC").isoformat()
    finish = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts,issued_at,grid_w,battery_w,soc,mode,charge_limit_w,"
            "discharge_limit_w,grid_charge_w,details_json "
            "FROM execution_plan WHERE ts>=? AND ts<? ORDER BY ts",
            (begin, finish)).fetchall()
        con.close()
    except Exception:
        rows = []
    records = []
    for row in rows:
        (ts, issued, grid, battery, soc, mode, charge_limit,
         discharge_limit, grid_charge, details_text) = row
        try:
            details = json.loads(details_text or "{}")
        except (TypeError, ValueError):
            details = {}
        dashboard = details.get("dashboard")
        record = dict(dashboard) if isinstance(dashboard, dict) else {}
        record["issued_at"] = issued
        record.setdefault("mode", mode)
        record.setdefault("house_soc_percent", soc)
        record.setdefault("planned_grid_w", grid)
        record.setdefault("planned_battery_w", battery)
        record.setdefault("batt_charge_limit_w", charge_limit)
        record.setdefault("batt_discharge_limit_w", discharge_limit)
        record.setdefault("batt_grid_charge_w", grid_charge)
        record.setdefault("pv_w", details.get("pv_w"))
        record.setdefault("pv_curtail_w", details.get("pv_curtail_w"))
        record.setdefault("export_line_w", details.get("export_limit_w"))
        record.setdefault("execution_path", details.get("execution_path"))
        record.setdefault("execution_label", details.get("execution_label"))
        export = record.get("grid_export_w", details.get("grid_export_w"))
        if export is not None:
            record.setdefault("grid_export_w", export)
            if grid is not None:
                record.setdefault("grid_import_w", float(grid) + float(export))
        # Kompatibilität für alte Datensätze: die aggregierte Akkuleistung
        # reicht für eine ehrliche Sollkurve und eine grobe Balkenzerlegung.
        if battery is not None:
            value = float(battery)
            ac = max(0.0, min(value, float(grid_charge or 0.0)))
            record.setdefault("batt_ac_charge_w", ac)
            record.setdefault("batt_dc_charge_w", max(0.0, value - ac))
            record.setdefault("batt_discharge_w", max(0.0, -value))
        record["_timestamp"] = pd.Timestamp(ts).tz_convert(tz)
        records.append(record)
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records).set_index("_timestamp").sort_index()
    frame.index.name = None
    return frame


def read_optimizer_forecast_origins(path: str, start, end, tz: str,
                                    slot_minutes: int) -> pd.DataFrame:
    """Den finalen Prognosesnapshot jedes vergangenen Planungsslots lesen.

    Bei mehreren Intraday-Neuberechnungen innerhalb eines Slots gewinnt der
    jüngste Snapshot. Für den Zielslot wird damit genau die Prognose sichtbar,
    die unmittelbar vor dessen zuletzt publiziertem Plan verwendet wurde.
    """
    begin = pd.Timestamp(start).tz_convert("UTC")
    finish = pd.Timestamp(end).tz_convert("UTC")
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT issue_ts,payload FROM optimizer_forecast_snapshots "
            "WHERE issue_ts>=? AND issue_ts<? ORDER BY issue_ts",
            (begin.isoformat(), finish.isoformat())).fetchall()
        con.close()
    except Exception:
        rows = []
    freq = f"{max(1, int(slot_minutes))}min"
    selected = {}
    for issue_text, blob in rows:
        issue = pd.Timestamp(issue_text).tz_convert(tz)
        selected[issue.floor(freq)] = blob
    records = []
    for target, blob in selected.items():
        try:
            payload = json.loads(zlib.decompress(blob).decode("utf-8"))
            idx = pd.to_datetime(payload["timestamps"], utc=True,
                                 format="ISO8601").tz_convert(tz)
            pos = idx.get_indexer([target])[0]
            if pos < 0:
                continue
            record = {"_timestamp": target}
            for name, values in payload.get("series", {}).items():
                value = values[pos]
                if value is not None:
                    record[name] = float(value)
            for name, values in payload.get("estimated", {}).items():
                record[f"{name}_estimated"] = bool(values[pos])
            records.append(record)
        except Exception:
            continue
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records).set_index("_timestamp").sort_index()
    frame.index.name = None
    return frame


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
    con.execute(
        "INSERT INTO battery_diagnostics("
        "ts,soc_operational,soc_ems,rsoc_real,usable_capacity_ah,"
        "usable_remaining_capacity_ah,full_charge_capacity_ah,"
        "remaining_capacity_ah,voltage_v,current_a,specified_capacity_wh,"
        "soc_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts) DO UPDATE SET "
        "soc_operational=excluded.soc_operational,soc_ems=excluded.soc_ems,"
        "rsoc_real=excluded.rsoc_real,"
        "usable_capacity_ah=excluded.usable_capacity_ah,"
        "usable_remaining_capacity_ah=excluded.usable_remaining_capacity_ah,"
        "full_charge_capacity_ah=excluded.full_charge_capacity_ah,"
        "remaining_capacity_ah=excluded.remaining_capacity_ah,"
        "voltage_v=excluded.voltage_v,current_a=excluded.current_a,"
        "specified_capacity_wh=excluded.specified_capacity_wh,"
        "soc_source=excluded.soc_source",
        (key, live.get("soc_percent"), live.get("soc_ems_percent"),
         live.get("rsoc_real_percent"), live.get("usable_capacity_ah"),
         live.get("usable_remaining_capacity_ah"),
         live.get("full_charge_capacity_ah"),
         live.get("remaining_capacity_ah"), live.get("battery_voltage_v"),
         live.get("battery_current_a"), live.get("specified_capacity_wh"),
         live.get("soc_source")))
    con.commit()
    con.close()


def read_battery_diagnostics(path: str, limit: int = 96) -> list[dict]:
    """Letzte erweiterte RSCP-Batteriewerte, chronologisch aufsteigend."""
    columns = [
        "ts", "soc_operational", "soc_ems", "rsoc_real",
        "usable_capacity_ah", "usable_remaining_capacity_ah",
        "full_charge_capacity_ah", "remaining_capacity_ah",
        "voltage_v", "current_a", "specified_capacity_wh", "soc_source",
    ]
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT " + ",".join(columns) + " FROM battery_diagnostics "
            "ORDER BY ts DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        con.close()
    except Exception:
        rows = []
    return [dict(zip(columns, row)) for row in reversed(rows)]


def write_live_sample(path: str, ts, live: dict) -> None:
    """Einen hochaufgelösten E3DC-Livewert für den Nowcast sichern."""
    if not live:
        return
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO live_samples("
        "ts,pv_w,house_w,grid_w,battery_w,wallbox_w) VALUES(?,?,?,?,?,?)",
        (stamp.tz_convert("UTC").isoformat(), live.get("pv_w"),
         live.get("house_load_w"), live.get("grid_w"),
         live.get("battery_w"), live.get("wallbox_w")))
    con.commit()
    con.close()


def settle_actual_slots(path: str, averages: "pd.DataFrame") -> int:
    """Abgeschlossene Slots in ``actuals`` auf die 5-s-Mittel nachziehen.

    ``write_actuals`` legt je Zyklus EINEN Momentanwert beim Slotzeitstempel ab.
    Leistung ist aber eine Intervallgroesse: auf einer steilen Flanke liegt die
    Stichprobe am Slotanfang systematisch unter dem Slotmittel (PV-Vormittag)
    bzw. darueber (Nachmittag) - eine Verzerrung mit Richtung, kein Rauschen.
    Das Dashboard legt deshalb laengst die Slotmittel darueber; alles, was
    ``read_actual`` direkt liest (Archiv, Kalibrierung, Drift, Heatmaps,
    Planwert), sah bisher weiter den Momentanwert.

    Nachgezogen werden nur die LEISTUNGEN. ``soc`` bleibt unberuehrt: der ist
    ein Zustand am Slotanfang, ein Slotmittel wuerde ihn um einen halben Slot
    verschieben und genau die Ausrichtung zerstoeren, die
    ``quality.planned_soc_on_measurement_axis`` herstellt. In ``live_samples``
    steht er ohnehin nicht.

    Nur BESTEHENDE Zeilen werden aktualisiert - fuer einen Slot ohne Zyklus
    gaebe es keinen SoC, und eine Zeile mit halben Daten waere schlechter als
    keine. Idempotent: mehrfach aufgerufen aendert sich nichts.
    """
    if averages is None or averages.empty:
        return 0
    columns = (("pv_w", "pv_w"), ("house_w", "house_w"),
               ("grid_w", "grid_w"), ("battery_w", "battery_w"))
    rows = []
    for stamp, row in averages.iterrows():
        values = [(None if pd.isna(row.get(src)) else float(row[src]))
                  for src, _target in columns]
        if all(value is None for value in values):
            continue
        rows.append((*values, pd.Timestamp(stamp).tz_convert("UTC").isoformat()))
    if not rows:
        return 0
    assignments = ", ".join(
        f"{target}=COALESCE(?, {target})" for _src, target in columns)
    con = _con(path)
    cur = con.executemany(
        f"UPDATE actuals SET {assignments} WHERE ts=?", rows)
    con.commit()
    count = int(cur.rowcount if cur.rowcount is not None else 0)
    con.close()
    return count


def write_pv_strings(path: str, ts, werte: dict) -> int:
    """Slotmittel je Strang ablegen. ``werte``: {Strang-Index: Watt}."""
    if not werte:
        return 0
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    key = stamp.tz_convert("UTC").isoformat()
    zeilen = [(key, int(i), float(w)) for i, w in werte.items()
              if w is not None and pd.notna(w)]
    if not zeilen:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO pv_strings(ts, idx, power_w) VALUES(?,?,?) "
        "ON CONFLICT(ts, idx) DO UPDATE SET power_w=excluded.power_w", zeilen)
    con.commit()
    con.close()
    return len(zeilen)


def read_pv_strings(path: str, start, end, tz: str) -> "pd.DataFrame":
    """Strangleistungen als Frame (Spalte je Strang) im lokalen Zeitraster."""
    begin = pd.Timestamp(start).tz_convert("UTC").isoformat()
    finish = pd.Timestamp(end).tz_convert("UTC").isoformat()
    con = _con(path)
    rows = con.execute(
        "SELECT ts, idx, power_w FROM pv_strings WHERE ts>=? AND ts<? "
        "ORDER BY ts", (begin, finish)).fetchall()
    con.close()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["ts", "idx", "power_w"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
    out = frame.pivot(index="ts", columns="idx", values="power_w")
    out.index = out.index.tz_convert(tz)
    out.columns = [f"string_{i}" for i in out.columns]
    return out.sort_index()


def prune_live_samples(path: str, before) -> int:
    """Alte 5-s-Werte begrenzen; wird nur je Optimierungszyklus aufgerufen."""
    stamp = pd.Timestamp(before)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    con = _con(path)
    cur = con.execute("DELETE FROM live_samples WHERE ts < ?",
                      (stamp.tz_convert("UTC").isoformat(),))
    con.commit()
    count = int(cur.rowcount if cur.rowcount is not None else 0)
    con.close()
    return count


def read_live_slot_averages(path: str, start, end, tz: str,
                            slot_minutes: int, min_coverage_seconds: float = 180.0,
                            max_gap_seconds: float = 30.0) -> pd.DataFrame:
    """5-s-Livewerte zeitgewichtet auf EMS-Slots integrieren.

    Nur tatsächlich von benachbarten Messpunkten abgedeckte Zeit zählt.
    Lücken über ``max_gap_seconds`` werden nicht überbrückt. Ein Slot erscheint
    erst ab ``min_coverage_seconds`` belastbarer Abdeckung.
    """
    begin = pd.Timestamp(start).tz_convert("UTC")
    finish = pd.Timestamp(end).tz_convert("UTC")
    pad = pd.Timedelta(seconds=max(1.0, float(max_gap_seconds)))
    con = _con(path)
    rows = con.execute(
        "SELECT ts,pv_w,house_w,grid_w,battery_w,wallbox_w "
        "FROM live_samples WHERE ts>=? AND ts<=? ORDER BY ts",
        ((begin - pad).isoformat(), finish.isoformat())).fetchall()
    con.close()
    fields = ("pv_w", "house_w", "grid_w", "battery_w", "wallbox_w")
    if len(rows) < 2:
        return pd.DataFrame(columns=fields, dtype="float64")
    points = [(pd.Timestamp(row[0]).tz_convert("UTC"), row[1:])
              for row in rows]
    freq = pd.Timedelta(minutes=int(slot_minutes))
    energy: dict[tuple[pd.Timestamp, str], float] = {}
    covered: dict[tuple[pd.Timestamp, str], float] = {}
    for (t0, values0), (t1, values1) in zip(points, points[1:]):
        gap = (t1 - t0).total_seconds()
        if gap <= 0.0 or gap > float(max_gap_seconds):
            continue
        left, right = max(t0, begin), min(t1, finish)
        if left >= right:
            continue
        cursor = left
        while cursor < right:
            slot = cursor.floor(freq)
            boundary = min(slot + freq, right)
            a = (cursor - t0).total_seconds() / gap
            b = (boundary - t0).total_seconds() / gap
            seconds = (boundary - cursor).total_seconds()
            for pos, field in enumerate(fields):
                v0, v1 = values0[pos], values1[pos]
                if v0 is None or v1 is None:
                    continue
                try:
                    start_v = float(v0) + a * (float(v1) - float(v0))
                    end_v = float(v0) + b * (float(v1) - float(v0))
                except (TypeError, ValueError):
                    continue
                key = (slot, field)
                energy[key] = energy.get(key, 0.0) + (
                    0.5 * (start_v + end_v) * seconds)
                covered[key] = covered.get(key, 0.0) + seconds
            cursor = boundary
    data = {}
    for field in fields:
        values = {}
        for (slot, name), watt_seconds in energy.items():
            if name != field:
                continue
            seconds = covered.get((slot, name), 0.0)
            if seconds >= float(min_coverage_seconds):
                values[slot] = watt_seconds / seconds
        data[field] = pd.Series(values, dtype="float64")
    frame = pd.DataFrame(data)
    if frame.empty:
        return frame
    frame.index = pd.DatetimeIndex(frame.index).tz_convert(tz)
    return frame.sort_index()


def read_controllable_load_power(path: str, loads: list, start, end, tz: str,
                                 slot_minutes: int):
    """Gemessene Leistung steuerbarer Lasten auf dem Slotraster.

    Rückgabe ``(power, complete, labels)``. ``complete`` ist nur dort wahr, wo
    jede unabhängige Messquelle einen frischen Wert geliefert hat. Mehrere
    Stufen mit demselben ``power_topic`` teilen einen Gesamtzähler und werden
    deshalb genau einmal bilanziert. Unbekannte Zeiten werden nie als 0 W
    interpretiert.
    """
    lanes = {}
    for load in loads or []:
        if not getattr(load, "enabled", False):
            continue
        if getattr(load, "type", None) == "thermal":
            members = [
                (stage.name, stage.power_w, stage.feedback_topic,
                 stage.power_topic)
                for stage in getattr(load, "stages", [])]
        else:
            profile = getattr(load, "power_profile_w", None) or []
            nominal = (float(getattr(load, "power_w", 0.0) or 0.0)
                       or float(max(profile, default=0.0)))
            members = [("__load__", nominal,
                        getattr(load, "feedback_topic", None),
                        getattr(load, "power_topic", None))]
        for stage_name, nominal, state_topic, power_topic in members:
            if not (state_topic or power_topic):
                continue
            lane = (str(load.name), str(stage_name))
            # Ein power_topic ist ein physischer Messkanal. Wird er für mehrere
            # Schwellen/Stufen verwendet, darf seine Gesamtleistung nicht je
            # Stufe erneut addiert werden.
            source = (("power", str(power_topic)) if power_topic else
                      ("state", str(load.name), str(stage_name)))
            label = (str(load.name) if stage_name == "__load__"
                     else f"{load.name}/{stage_name}")
            lanes[lane] = {
                "nominal_w": float(nominal), "source": source, "label": label,
                "uses_power": bool(power_topic)}
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    freq = f"{int(slot_minutes)}min"
    grid = pd.date_range(begin.floor(freq), finish.ceil(freq),
                         freq=freq, inclusive="left")
    if not lanes:
        return (pd.Series(0.0, index=grid, dtype="float64"),
                pd.Series(True, index=grid, dtype="bool"), [])
    s_utc = begin.tz_convert("UTC").isoformat()
    e_utc = finish.tz_convert("UTC").isoformat()
    con = _con(path)
    names = sorted({name for name, _ in lanes})
    marks = ",".join("?" for _ in names)
    rows = con.execute(
        "SELECT name,stage,ts,actual_on,power_w,fresh FROM load_feedback "
        f"WHERE name IN ({marks}) AND ts>=? AND ts<? ORDER BY ts",
        (*names, s_utc, e_utc)).fetchall()
    con.close()
    by_lane = {key: pd.Series(index=grid, dtype="float64") for key in lanes}
    for name, stage, ts, actual_on, power_w, fresh in rows:
        key = (str(name), str(stage))
        if key not in by_lane or not fresh:
            continue
        stamp = pd.Timestamp(ts).tz_convert(tz).floor(freq)
        if stamp not in by_lane[key].index:
            continue
        value = power_w if lanes[key]["uses_power"] else None
        if value is None and actual_on is not None:
            value = lanes[key]["nominal_w"] if bool(actual_on) else 0.0
        if value is not None:
            by_lane[key].loc[stamp] = max(0.0, float(value))

    groups = {}
    for lane, meta in lanes.items():
        groups.setdefault(meta["source"], []).append(lane)
    columns, labels = {}, []
    for source, members in groups.items():
        member_frame = pd.concat(
            [by_lane[lane] for lane in members], axis=1)
        if source[0] == "power":
            # Identischer gemeinsamer Zähler je Schwellenstufe: einmal den
            # verfügbaren Gesamtwert nehmen. ``max`` ist robust, falls ein
            # MQTT-Zyklus nur eine der Stufen rechtzeitig aktualisiert hat.
            values = member_frame.max(axis=1, skipna=True).where(
                member_frame.notna().any(axis=1))
        else:
            values = member_frame.sum(axis=1, min_count=len(members))
        label_parts = [lanes[lane]["label"] for lane in members]
        label = (" + ".join(label_parts)
                 + (" (gemeinsame Leistung)" if len(members) > 1 else ""))
        columns[label] = values
        labels.append(label)
    frame = pd.DataFrame(columns, index=grid)
    complete = frame.notna().all(axis=1)
    total = frame.sum(axis=1, min_count=len(frame.columns))
    return total.astype("float64"), complete.astype("bool"), labels


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


def write_load_feedback(path: str, ts, name: str, stage: str,
                        feedback: dict) -> None:
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    on = feedback.get("on")
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO load_feedback VALUES(?,?,?,?,?,?,?)",
        (str(name), str(stage), key, None if on is None else int(bool(on)),
         feedback.get("power_w"), int(bool(feedback.get("fresh"))),
         feedback.get("age_seconds")))
    con.commit()
    con.close()


def write_load_stage_cmd(path: str, ts, name: str, stage: str,
                         commanded_on: int) -> None:
    key = pd.Timestamp(ts).tz_convert("UTC").isoformat()
    con = _con(path)
    con.execute("INSERT OR REPLACE INTO load_stage_cmd VALUES(?,?,?,?)",
                (str(name), str(stage), key, int(bool(commanded_on))))
    con.commit()
    con.close()


def read_load_stage_run_state(path: str, name: str, stage: str, now,
                              slot_minutes: int) -> Optional[dict]:
    """Aktuellen publizierten Stufenzustand und seine zusammenhaengende Dauer.

    Die Schalthistorie liegt auf dem EMS-Slotraster und ueberlebt einen
    Dienstneustart. Eine Luecke groesser als ein Slot macht den Zustand bewusst
    unbekannt; eine alte Freigabe darf keine neue Planung sperren.
    """
    slot = pd.Timedelta(minutes=max(1, int(slot_minutes)))
    current = pd.Timestamp(now)
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    cutoff = current.floor(f"{max(1, int(slot_minutes))}min").tz_convert("UTC")
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts,commanded_on FROM load_stage_cmd "
            "WHERE name=? AND stage=? AND ts<=? ORDER BY ts DESC LIMIT 512",
            (str(name), str(stage), cutoff.isoformat())).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return None
    latest_ts = pd.Timestamp(rows[0][0])
    if cutoff - latest_ts > slot * 1.5:
        return None
    state = bool(rows[0][1])
    earliest = latest_ts
    newer = latest_ts
    for raw_ts, raw_state in rows[1:]:
        ts = pd.Timestamp(raw_ts)
        if bool(raw_state) != state or newer - ts > slot * 1.5:
            break
        earliest = ts
        newer = ts
    minutes = max(0.0, (cutoff - earliest).total_seconds() / 60.0)
    return {"on": state, "minutes": minutes}


def read_load_actual_on(path: str, name: str, stages: list[str], start, end,
                        tz: str) -> pd.Series:
    """Realer Gesamt-Heizstatus; nur Slots mit Rückmeldung ALLER Stufen."""
    if not stages:
        return pd.Series(dtype="float64")
    s = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        marks = ",".join("?" for _ in stages)
        rows = con.execute(
            f"SELECT ts, stage, actual_on FROM load_feedback WHERE name=? "
            f"AND stage IN ({marks}) AND ts>=? AND ts<=? AND fresh=1 "
            f"AND actual_on IS NOT NULL ORDER BY ts", (name, *stages, s, e)
        ).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    frame = pd.DataFrame(rows, columns=["ts", "stage", "on"])
    wide = frame.pivot_table(index="ts", columns="stage", values="on",
                             aggfunc="last").reindex(columns=stages).dropna()
    idx = pd.to_datetime(wide.index, utc=True, format="ISO8601")
    return pd.Series(wide.max(axis=1).to_numpy(dtype=float), index=idx
                     ).tz_convert(tz)


def read_load_stage_on(path: str, name: str, stages: list[str], start, end,
                       tz: str) -> dict[str, pd.Series]:
    """Frische reale Ein/Aus-Reihen je Stufe; fehlende Werte bleiben Lücken."""
    if not stages:
        return {}
    s = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        marks = ",".join("?" for _ in stages)
        rows = con.execute(
            f"SELECT ts,stage,actual_on FROM load_feedback WHERE name=? "
            f"AND stage IN ({marks}) AND ts>=? AND ts<=? AND fresh=1 "
            f"AND actual_on IS NOT NULL ORDER BY ts", (name, *stages, s, e)
        ).fetchall()
        con.close()
    except Exception:
        rows = []
    out = {}
    for stage in stages:
        selected = [(r[0], r[2]) for r in rows if r[1] == stage]
        if selected:
            idx = pd.to_datetime([r[0] for r in selected], utc=True,
                                 format="ISO8601")
            out[stage] = pd.Series([r[1] for r in selected], index=idx,
                                   dtype="float64").tz_convert(tz)
    return out


def read_load_stage_power(path: str, name: str, stages: list[str], start, end,
                          tz: str) -> dict[str, pd.Series]:
    """Gemessene elektrische Leistung je rückgekoppelter Last/Stufe."""
    if not stages:
        return {}
    s = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        marks = ",".join("?" for _ in stages)
        rows = con.execute(
            f"SELECT ts,stage,power_w FROM load_feedback WHERE name=? "
            f"AND stage IN ({marks}) AND ts>=? AND ts<=? AND fresh=1 "
            f"AND power_w IS NOT NULL ORDER BY ts", (name, *stages, s, e)
        ).fetchall()
        con.close()
    except Exception:
        rows = []
    out = {}
    for stage in stages:
        selected = [(r[0], r[2]) for r in rows if r[1] == stage]
        if selected:
            idx = pd.to_datetime([r[0] for r in selected], utc=True,
                                 format="ISO8601")
            out[stage] = pd.Series([r[1] for r in selected], index=idx,
                                   dtype="float64").tz_convert(tz)
    return out


def read_latest_load_feedback(path: str, tz: str) -> list[dict]:
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT f.name,f.stage,f.ts,f.actual_on,f.power_w,f.fresh,f.age_seconds "
            "FROM load_feedback f JOIN (SELECT name,stage,MAX(ts) ts "
            "FROM load_feedback GROUP BY name,stage) x "
            "ON f.name=x.name AND f.stage=x.stage AND f.ts=x.ts "
            "ORDER BY f.name,f.stage").fetchall()
        con.close()
    except Exception:
        rows = []
    return [{"name": r[0], "stage": r[1],
             "ts": pd.Timestamp(r[2]).tz_convert(tz).isoformat(),
             "on": None if r[3] is None else bool(r[3]), "power_w": r[4],
             "fresh": bool(r[5]), "age_seconds": r[6]} for r in rows]


def write_thermal_calibration(path: str, name: str, status: dict) -> None:
    ts = pd.Timestamp(status.get("ts", pd.Timestamp.now(tz="UTC")))
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    con = _con(path)
    con.execute(
        "INSERT OR REPLACE INTO thermal_calibration VALUES(?,?,?,?,?,?,?,?,?)",
        (str(name), ts.tz_convert("UTC").isoformat(),
         str(status.get("status", "unknown")), status.get("n_windows"),
         status.get("r2"), json.dumps(status.get("old") or {}),
         json.dumps(status.get("fitted") or {}),
         json.dumps(status.get("applied") or {}),
         str(status.get("message", ""))))
    con.commit()
    con.close()


def read_latest_thermal_calibration(path: str, tz: str) -> list[dict]:
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT c.name,c.ts,c.status,c.n_windows,c.r2,c.old_json,"
            "c.fitted_json,c.applied_json,c.message FROM thermal_calibration c "
            "JOIN (SELECT name,MAX(ts) ts FROM thermal_calibration GROUP BY name) x "
            "ON c.name=x.name AND c.ts=x.ts ORDER BY c.name").fetchall()
        con.close()
    except Exception:
        rows = []
    return [{"name": r[0], "ts": pd.Timestamp(r[1]).tz_convert(tz).isoformat(),
             "status": r[2], "n_windows": r[3], "r2": r[4],
             "old": json.loads(r[5] or "{}"),
             "fitted": json.loads(r[6] or "{}"),
             "applied": json.loads(r[7] or "{}"), "message": r[8]}
            for r in rows]


_EVENT_KEEP_DAYS = 30.0
_EVENT_ROW_CAP = 20000


def write_dashboard_event(path: str, kind: str, message: str, *,
                          level: str = "info", details: Optional[dict] = None,
                          ts=None) -> None:
    """Kompaktes Bedien-/Systemereignis fuer die Weboberflaeche speichern."""
    stamp = pd.Timestamp(ts if ts is not None else pd.Timestamp.now(tz="UTC"))
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    con = _con(path)
    con.execute(
        "INSERT INTO dashboard_event(ts,kind,level,message,details_json) "
        "VALUES(?,?,?,?,?)",
        (stamp.tz_convert("UTC").isoformat(), str(kind), str(level),
         str(message), json.dumps(details or {}, ensure_ascii=False, default=str)))
    # Verlauf nach ZEIT begrenzen, nicht nach Zeilenzahl: bei gemessen 110 bis
    # 160 Ereignissen pro Tag reichten die frueheren 500 Eintraege nur gut drei
    # Tage. Am 31.08.2026 war deshalb nicht mehr feststellbar, wann eine
    # Warnserie begonnen hatte - die aelteste Zeile fiel mit dem ersten Alarm
    # zusammen, was faelschlich wie ein Beginn aussah. 30 Tage sind rund 4500
    # Zeilen und damit unkritisch; die Obergrenze faengt nur den pathologischen
    # Fall (Dauerschleife manueller Neuberechnungen) ab.
    # Anker ist der JUENGSTE Eintrag, nicht die Wanduhr: sonst loescht ein
    # Aufruf mit historischem Zeitstempel (Nachtragen, Test) die eigenen Zeilen.
    neuest = con.execute("SELECT max(ts) FROM dashboard_event").fetchone()[0]
    if neuest:
        cutoff = (pd.Timestamp(neuest)
                  - pd.Timedelta(days=_EVENT_KEEP_DAYS)).isoformat()
        con.execute("DELETE FROM dashboard_event WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM dashboard_event WHERE id NOT IN "
                "(SELECT id FROM dashboard_event ORDER BY id DESC LIMIT ?)",
                (_EVENT_ROW_CAP,))
    con.commit()
    con.close()


def read_dashboard_events(path: str, tz: str, limit: int = 50) -> list[dict]:
    """Neueste Dashboard-Ereignisse, absteigend sortiert."""
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT id,ts,kind,level,message,details_json FROM dashboard_event "
            # Obergrenze grosszuegig: die Anzeige fragt ohnehin nur wenige
            # Zeilen ab, aber eine Diagnose ("seit wann warnt das?") braucht
            # den Verlauf. Mit den frueheren 200 reichte jede Abfrage nur rund
            # 1,5 Tage zurueck - unabhaengig davon, was die Tabelle noch hielt.
            "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 5000)),)
        ).fetchall()
        con.close()
    except Exception:
        rows = []
    out = []
    for row in rows:
        try:
            details = json.loads(row[5] or "{}")
        except (TypeError, ValueError):
            details = {}
        out.append({"id": row[0],
                    "ts": pd.Timestamp(row[1]).tz_convert(tz).isoformat(),
                    "kind": row[2], "level": row[3], "message": row[4],
                    "details": details})
    return out


def write_debug_snapshot(path: str, snap: dict, keep: int = 1000) -> None:
    """Einen Debug-Schnappschuss (komprimiertes JSON) in den rollierenden
    Verlauf schreiben und auf die letzten ``keep`` Läufe begrenzen. So bleibt
    auch ein älterer infeasibler/falscher Plan mit allen Eingaben versendbar.

    1000 Läufe sind bei ~11 kB je Schnappschuss rund 11 MB und reichen für etwa
    zehn Tage – genug, um im Lauf-Archiv (``/archiv``) mehrere Tage zu
    vergleichen."""
    try:
        payload = zlib.compress(
            json.dumps(snap, ensure_ascii=False, default=str).encode("utf-8"),
            level=6)
        con = _con(path)
        con.execute(
            "INSERT INTO debug_snapshot"
            "(generated, status, infeasible, reason, n_violations, payload) "
            "VALUES(?,?,?,?,?,?)",
            (str(snap.get("generated") or pd.Timestamp.now(tz="UTC").isoformat()),
             str(snap.get("status") or ""),
             1 if snap.get("infeasible") else 0,
             (str(snap.get("infeasible_reason")) if snap.get("infeasible_reason")
              else None),
             int(len(snap.get("violations") or [])),
             sqlite3.Binary(payload)))
        con.execute(
            "DELETE FROM debug_snapshot WHERE id NOT IN "
            "(SELECT id FROM debug_snapshot ORDER BY id DESC LIMIT ?)",
            (max(1, int(keep)),))
        con.commit()
        con.close()
    except Exception as exc:   # Debug-Persistenz darf den Lauf nie stören
        log.debug("Debug-Schnappschuss nicht speicherbar: %s", exc)


def list_debug_snapshots(path: str, tz: str, limit: int = 60) -> list[dict]:
    """Kopf-Metadaten der jüngsten Debug-Schnappschüsse (ohne Payload), neueste
    zuerst - für die Auswahl im Debug-Panel."""
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT generated, status, infeasible, reason, n_violations "
            "FROM debug_snapshot ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 1200)),)).fetchall()
        con.close()
    except Exception:
        rows = []
    out = []
    for gen, status, infeasible, reason, nviol in rows:
        try:
            ts = pd.Timestamp(gen).tz_convert(tz).isoformat()
        except Exception:
            ts = str(gen)
        out.append({"generated": str(gen), "ts_local": ts,
                    "status": status or "", "infeasible": bool(infeasible),
                    "reason": reason or "", "n_violations": int(nviol or 0)})
    return out


def read_debug_snapshot(path: str, generated: Optional[str] = None):
    """Vollständigen Debug-Schnappschuss (dekomprimiertes JSON) lesen; ohne
    ``generated`` den neuesten. None, wenn keiner vorhanden."""
    try:
        con = _con(path)
        if generated:
            row = con.execute(
                "SELECT payload FROM debug_snapshot WHERE generated = ? "
                "ORDER BY id DESC LIMIT 1", (str(generated),)).fetchone()
        else:
            row = con.execute(
                "SELECT payload FROM debug_snapshot ORDER BY id DESC LIMIT 1"
            ).fetchone()
        con.close()
    except Exception:
        row = None
    if not row:
        return None
    try:
        return json.loads(zlib.decompress(row[0]).decode("utf-8"))
    except Exception:
        return None


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
    # start/end hart aufs Slot-Boundary legen (floor/ceil): ein aus now
    # abgeleitetes start trägt sonst den Sekunden-Offset des Aufrufers, und ein
    # exaktes reindex(grid) beim Verbraucher (Forecaster/Kalibrierung) träfe
    # keinen Punkt -> alles NaN. So ist das Raster IMMER phasengleich zu den
    # (ebenfalls gefloorten) Verbraucher-Rastern, unabhängig vom Aufrufer.
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz).floor(freq),
                         pd.Timestamp(end).tz_convert(tz).ceil(freq), freq=freq,
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


def write_grid_weather(path: str, mapping: Dict[str, tuple]) -> int:
    """UPSERT der deutschlandweiten Indizes {UTC-ISO -> (wind, solar, temp)}."""
    if not mapping:
        return 0
    con = _con(path)
    con.executemany(
        "INSERT INTO grid_weather(ts, wind_index, solar_index, temp_index) "
        "VALUES(?,?,?,?) ON CONFLICT(ts) DO UPDATE SET "
        "wind_index=excluded.wind_index, solar_index=excluded.solar_index, "
        "temp_index=excluded.temp_index",
        [(k, float(v[0]), float(v[1]), float(v[2]))
         for k, v in mapping.items()])
    con.commit()
    con.close()
    return len(mapping)


def read_grid_weather(path: str, start, end, tz: str,
                      freq: Optional[str] = None) -> pd.DataFrame:
    """Deutschlandweite Indizes [start, end). Ohne ``freq`` als Stundenwerte
    (UTC-Index, so wie gespeichert - das Preismodell rundet selbst auf die
    Stunde); mit ``freq`` zeitlich auf das Slot-Raster interpoliert."""
    s_utc = (pd.Timestamp(start) - pd.Timedelta(hours=2)).tz_convert("UTC").isoformat()
    e_utc = (pd.Timestamp(end) + pd.Timedelta(hours=2)).tz_convert("UTC").isoformat()
    cols = ["wind_index", "solar_index", "temp_index"]
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT ts, wind_index, solar_index, temp_index FROM grid_weather "
            "WHERE ts >= ? AND ts < ? ORDER BY ts", (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.DataFrame(columns=cols)
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    frame = pd.DataFrame([r[1:] for r in rows], index=idx, columns=cols,
                         dtype="float64")
    if freq is None:
        return frame
    local = frame.tz_convert(tz)
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz).floor(freq),
                         pd.Timestamp(end).tz_convert(tz).ceil(freq), freq=freq,
                         inclusive="left")
    if len(grid) == 0:
        return local
    step_min = max(1.0, pd.Timedelta(freq).total_seconds() / 60.0)
    limit = max(1, int(120.0 / step_min))
    return (local.reindex(local.index.union(grid)).interpolate(
            method="time", limit=limit, limit_area="inside").reindex(grid))


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
    # start/end hart aufs Slot-Boundary (floor/ceil), damit das Raster
    # phasengleich zu den Verbraucher-Rastern ist - siehe read_temperature.
    grid = pd.date_range(pd.Timestamp(start).tz_convert(tz).floor(freq),
                         pd.Timestamp(end).tz_convert(tz).ceil(freq), freq=freq,
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


def read_optimizer_forecast_snapshots(path: str, start, end, tz: str,
                                      stride_hours: int = 6) -> list:
    """Produktions-Snapshots in unabhängigen Origin-Blöcken lesen.

    Pro ``stride_hours`` bleibt nur der letzte Lauf erhalten. Das verhindert,
    dass 15-min-Neuberechnungen denselben Zielslot dutzendfach gewichten.
    """
    begin = pd.Timestamp(start).tz_convert("UTC")
    finish = pd.Timestamp(end).tz_convert("UTC")
    con = _con(path)
    rows = con.execute(
        "SELECT issue_ts,payload FROM optimizer_forecast_snapshots "
        "WHERE issue_ts>=? AND issue_ts<? ORDER BY issue_ts",
        (begin.isoformat(), finish.isoformat())).fetchall()
    con.close()
    stride = max(1, int(stride_hours))
    selected = {}
    for issue_text, blob in rows:
        issue = pd.Timestamp(issue_text).tz_convert("UTC")
        selected[issue.floor(f"{stride}h")] = (issue, blob)
    out = []
    for issue, blob in selected.values():
        try:
            payload = json.loads(zlib.decompress(blob).decode("utf-8"))
            idx = pd.to_datetime(payload["timestamps"], utc=True,
                                 format="ISO8601").tz_convert(tz)
            frame = pd.DataFrame(payload["series"], index=idx,
                                 dtype="float64").sort_index()
        except Exception:
            continue
        out.append((issue.tz_convert(tz), frame))
    return out


def read_intraday_hour_ratios(path: str, signal: str, now, days: float,
                              tz: str, max_factor: float = 1.5,
                              min_samples: int = 4) -> dict:
    """Restverhaeltnis Ist/Prognose je Tagesstunde aus den letzten ``days``.

    Quelle sind die schon archivierten Intraday-Fenster (``intraday_window``):
    sie enthalten je Ausgabezeitpunkt Prognose UND Ist, also genau die Paare,
    aus denen sonst das 3-h-Verhaeltnis entsteht. Damit braucht das
    regimegleiche Fenster (dieselbe Stunde der Vortage) keine neu gerechneten
    Prognosen.

    Nur Stunden mit genug Paaren kommen vor; der Aufrufer laesst fehlende
    Stunden bei 1.0. Rueckgabe {Stunde: Verhaeltnis}, geklemmt auf
    [1/max_factor, max_factor].
    """
    if days <= 0:
        return {}
    ende = pd.Timestamp(now)
    if ende.tzinfo is None:
        ende = ende.tz_localize("UTC")
    start = (ende - pd.Timedelta(days=float(days))).tz_convert("UTC").isoformat()
    try:
        con = _con(path)
        rows = con.execute(
            "SELECT target_ts, actual_w, predicted_w FROM intraday_window "
            "WHERE signal = ? AND target_ts >= ? AND target_ts < ? "
            "AND eligible = 1 AND actual_w IS NOT NULL "
            "AND predicted_w IS NOT NULL AND predicted_w > 0",
            (str(signal), start, ende.tz_convert("UTC").isoformat())).fetchall()
        con.close()
    except Exception:
        return {}
    if not rows:
        return {}
    je_stunde: dict = {}
    for ts, actual, predicted in rows:
        try:
            stunde = int(pd.Timestamp(ts).tz_convert(tz).hour)
        except Exception:
            continue
        je_stunde.setdefault(stunde, []).append(float(actual) / float(predicted))
    lo, hi = 1.0 / max(1e-6, max_factor), max(1e-6, max_factor)
    return {h: float(np.clip(float(np.median(v)), lo, hi))
            for h, v in je_stunde.items() if len(v) >= int(min_samples)}


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
