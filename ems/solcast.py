"""PV-Vorhersage direkt von Solcast (rooftop sites) statt InfluxDB.

Mehrere API-Keys und Resourcen werden unterstützt und beim Auslesen kombiniert:
  * combine="sum"  -> verschiedene physische Arrays (Ost/West) werden je Slot addiert
  * combine="mean" -> dieselbe Anlage über mehrere Keys (nur mehr Abrufe) -> mitteln

Abruf-Budget & Verteilung: je Key sind nur `calls_per_key_per_day` Abrufe erlaubt
(Free-Tier 10). Jede Quelle (Key+Resource) wird gleichmäßig über ein lokales
Tageslicht-Fenster verteilt abgerufen (key_budget / Quellen-je-Key mal pro Tag),
sodass die Prognose tagsüber durch Solcasts Nowcasting laufend schärfer wird.
Zwischen den Abrufen wird der letzte Forecast (lokale SQLite) gehalten.

pv_estimate (kW) -> W; pv_estimate10/90 = P10/P90-Bänder. Solcast-Periode 30-min
wird beim Auslesen auf das Slot-Raster gehalten (ems/local_history.read_pv_forecast).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Dict, Tuple

import pandas as pd

from . import local_history

log = logging.getLogger("ems.solcast")

_BASE = "https://api.solcast.com.au/rooftop_sites/{rid}/forecasts?format=json"

# In-Memory-Cooldown je Quelle nach Fehler (z.B. 429), verhindert Retry-Schleifen.
_error_cooldown: Dict[str, pd.Timestamp] = {}

_SIGNAL_WHICH = {"pv_forecast": "pv", "pv_forecast_p10": "p10",
                 "pv_forecast_p90": "p90"}


def _period_minutes(p: str) -> int:
    p = str(p or "").upper().replace("PT", "")
    try:
        if p.endswith("H"):
            return int(float(p[:-1]) * 60)
        if p.endswith("M"):
            return int(p[:-1])
    except ValueError:
        pass
    return 30


def fetch_forecast(api_key: str, resource_id: str,
                   timeout: float = 30.0) -> Dict[str, Tuple[float, float, float]]:
    """Forecast einer Resource -> {UTC-ISO-Periodenstart: (pv_w, p10_w, p90_w)}."""
    req = urllib.request.Request(
        _BASE.format(rid=resource_id),
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    out: Dict[str, Tuple[float, float, float]] = {}
    for f in payload.get("forecasts", []) or []:
        pe = f.get("period_end")
        if pe is None or f.get("pv_estimate") is None:
            continue
        end = pd.Timestamp(pe)
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        start = (end - pd.Timedelta(minutes=_period_minutes(f.get("period", "PT30M")))
                 ).tz_convert("UTC")
        pv = float(f["pv_estimate"]) * 1000.0
        p10 = float(f.get("pv_estimate10", f["pv_estimate"])) * 1000.0
        p90 = float(f.get("pv_estimate90", f["pv_estimate"])) * 1000.0
        out[start.isoformat()] = (pv, p10, p90)
    return out


def refresh(config) -> None:
    """Fällige Quellen abrufen (Budget + gleichmäßige Verteilung im Fenster)."""
    sc = config.solcast
    if not (sc.enabled and sc.sources):
        return
    db = config.e3dc_rscp.history_db_path
    tz = config.general.timezone
    now = pd.Timestamp.now(tz=tz)
    start_h, end_h = sc.window_start_hour, sc.window_end_hour
    full_day = not (0 <= start_h < end_h <= 24) or (start_h == 0 and end_h == 24)
    if not full_day and not (start_h <= now.hour < end_h):
        return                                   # außerhalb des Abruf-Fensters
    window_secs = 24 * 3600 if full_day else (end_h - start_h) * 3600
    since_iso = now.normalize().tz_convert("UTC").isoformat()   # lokaler Tagesbeginn
    # Quellen je Key (fürs anteilige Budget)
    per_key: Dict[str, int] = {}
    for s in sc.sources:
        per_key[s.api_key] = per_key.get(s.api_key, 0) + 1

    fetched = 0
    for s in sc.sources:
        skey = f"{s.api_key}:{s.resource_id}"
        cd = _error_cooldown.get(skey)
        if cd is not None and now < cd:
            continue
        used = local_history.solcast_calls_since(db, s.api_key, since_iso)
        if used >= sc.calls_per_key_per_day:
            continue                             # Tagesbudget des Keys erschöpft
        per_source = max(1, sc.calls_per_key_per_day // per_key[s.api_key])
        interval = window_secs / per_source
        last = local_history.last_solcast_fetch(db, s.api_key, s.resource_id)
        if last is not None and (now - last).total_seconds() < interval * 0.9:
            continue                             # noch nicht fällig
        try:
            data = fetch_forecast(s.api_key, s.resource_id)
            local_history.write_pv_forecast(db, s.resource_id, data)
            local_history.log_solcast_call(db, s.api_key, s.resource_id,
                                           now.tz_convert("UTC").isoformat())
            _error_cooldown.pop(skey, None)
            fetched += 1
        except urllib.error.HTTPError as exc:
            _error_cooldown[skey] = now + pd.Timedelta(hours=1)
            log.warning("Solcast %s: HTTP %s – Abruf ausgesetzt (nutze Cache).",
                        s.resource_id, exc.code)
        except Exception as exc:
            _error_cooldown[skey] = now + pd.Timedelta(minutes=30)
            log.warning("Solcast %s: Abruf fehlgeschlagen (%s) – nutze Cache.",
                        s.resource_id, exc)
    if fetched:
        log.info("Solcast: %d Quelle(n) aktualisiert (combine=%s).", fetched, sc.combine)


def available(config, repo, signal: str) -> bool:
    """Ist das PV-Signal verfügbar? Bei solcast.enabled lokal (p10/p90 inklusive)."""
    if config.solcast.enabled and signal in _SIGNAL_WHICH:
        return True
    return repo.signal_available(signal)


def read_pv_signal(config, repo, signal: str, start, end) -> pd.Series:
    """PV-Signal [start, end): kombinierte Solcast-Quellen (lokal) oder InfluxDB."""
    if config.solcast.enabled and signal in _SIGNAL_WHICH:
        s = local_history.read_pv_forecast(
            config.e3dc_rscp.history_db_path, start, end, config.general.timezone,
            config.general.slot_minutes, config.solcast.combine, _SIGNAL_WHICH[signal])
        # Übergangslücke (noch kein Abruf im Cache): auf InfluxDB zurückfallen,
        # solange dort vorhanden – verhindert NaN-PV vor dem ersten Solcast-Abruf.
        if s.empty and repo is not None and repo.signal_available(signal):
            return repo.read_slots(signal, start, end)
        return s
    return repo.read_slots(signal, start, end)
