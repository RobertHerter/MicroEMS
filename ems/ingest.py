"""Externe Einspeisung von Live- und Historienwerten (REST-Ingest).

Ermöglicht den Betrieb OHNE RSCP/InfluxDB: ein externes System schickt die sonst
vom E3DC/InfluxDB kommenden Werte per HTTP POST /api/ingest/<kind> in den lokalen
Speicher. Live-Werte liegen in einem In-Memory-Cache (run_once liest ihn als
Ersatz für e3dc.read_live), Historien gehen direkt in die lokale SQLite.

Payload je kind (JSON):
  live         {"soc_percent":.., "pv_w":.., "house_load_w":.., "grid_w":.., "battery_w":..}
  house_load   {"values": {"<UTC-ISO>": <W>, ...}}
  temperature  {"values": {"<UTC-ISO>": <°C>, ...}}
  spot         {"values": {"<UTC-ISO>": <ct/kWh netto>, ...}}
  actuals      {"values": {"<ISO>": {"pv_w":..,"house_w":..,"grid_w":..,"battery_w":..,"soc":..}}}
  pv_forecast  {"source":"extern", "issue_time":"<ISO>",
                "values": {"<ISO>": {"pv_w":..,"pv10_w":..,"pv90_w":..}}}
Zeitstempel werden auf UTC-ISO normalisiert (naiv -> als UTC interpretiert).
"""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd

from . import local_history

log = logging.getLogger("ems.ingest")

_lock = threading.Lock()
_live: dict = {}
_LIVE_KEYS = ("soc_percent", "pv_w", "house_load_w", "grid_w", "battery_w")

KINDS = ("live", "house_load", "temperature", "spot", "actuals", "pv_forecast")


def set_live(d: dict) -> None:
    clean = {k: float(d[k]) for k in _LIVE_KEYS if d.get(k) is not None}
    with _lock:
        _live.clear()
        _live.update(clean)
        _live["_ts"] = time.monotonic()


def get_live(max_age_s: float = 1800.0):
    """Zuletzt eingespeiste Live-Werte (oder None, wenn keine/zu alt)."""
    with _lock:
        if not _live or (time.monotonic() - _live.get("_ts", 0.0) > max_age_s):
            return None
        return {k: v for k, v in _live.items() if k != "_ts"}


def _utc_iso(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC").isoformat()


def _values(payload: dict) -> dict:
    v = payload.get("values", payload) if isinstance(payload, dict) else {}
    return {_utc_iso(k): float(x) for k, x in v.items() if x is not None}


def ingest(config, kind: str, payload: dict) -> str:
    """Einen Ingest-Aufruf verarbeiten. Rückgabe: kurze Status-Meldung.
    Wirft KeyError bei unbekanntem kind."""
    db = config.e3dc_rscp.history_db_path
    if kind == "live":
        set_live(payload)
        return f"live aktualisiert ({', '.join(sorted(k for k in payload if k in _LIVE_KEYS))})"
    if kind == "house_load":
        return f"{local_history.write_house_load(db, _values(payload))} Fenster"
    if kind == "temperature":
        return f"{local_history.write_temperature(db, _values(payload))} Werte"
    if kind == "spot":
        return f"{local_history.write_spot(db, _values(payload))} Werte"
    if kind == "actuals":
        rows = payload.get("values", {}) or {}
        for ts, r in rows.items():
            local_history.write_actuals(db, _utc_iso(ts), {
                "pv_w": r.get("pv_w"),
                "house_load_w": r.get("house_w", r.get("house_load_w")),
                "grid_w": r.get("grid_w"), "battery_w": r.get("battery_w"),
                "soc_percent": r.get("soc", r.get("soc_percent"))})
        return f"{len(rows)} Ist-Zeilen"
    if kind == "pv_forecast":
        src = str(payload.get("source", "extern"))
        m = {}
        for ts, r in (payload.get("values", {}) or {}).items():
            if isinstance(r, (list, tuple)):
                m[_utc_iso(ts)] = (float(r[0]), float(r[1]), float(r[2]))
            else:
                pv = float(r["pv_w"])
                m[_utc_iso(ts)] = (pv, float(r.get("pv10_w", pv)), float(r.get("pv90_w", pv)))
        n = local_history.write_pv_forecast(db, src, m)
        issue_time = payload.get("issue_time", pd.Timestamp.now(tz="UTC"))
        archived = local_history.write_pv_forecast_archive(
            db, src, issue_time, m)
        return f"{n} Werte, {archived} Zukunftswerte archiviert"
    raise KeyError(kind)
