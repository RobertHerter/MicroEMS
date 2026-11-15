"""HTTP-Server des Dashboards samt Routing und Zugriffsschutz.

Aus ems/main.py ausgelagert (reine Verschiebung). ``ems.main`` re-exportiert
alle Namen; der Logger bleibt ``ems.main``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time as _time
from collections import deque

import numpy as np
import pandas as pd

from .config import Config
from .optimizer import request_solver_cancel, solver_is_running

log = logging.getLogger("ems.main")



def _cross_site_post(headers) -> bool:
    """Stammt ein POST von einer FREMDEN Webseite (CSRF)?

    Browser senden bei seitenfremden POSTs - auch bei einfachen Formularen mit
    ``text/plain`` - den Header ``Origin`` (moderne zusaetzlich
    ``Sec-Fetch-Site``). Passt dessen Host nicht zum angesprochenen ``Host``,
    wird abgelehnt. Geraete ohne Browser (Node-RED, Homey, curl) senden keinen
    ``Origin`` und bleiben unberuehrt."""
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site == "cross-site":
        return True
    origin = (headers.get("Origin") or "").strip()
    if not origin:
        return False
    if origin.lower() == "null":       # sandboxed iframe, data:-/file:-Seite
        return True
    try:
        from urllib.parse import urlsplit
        origin_host = (urlsplit(origin).netloc or "").lower()
    except ValueError:
        return True
    host = (headers.get("Host") or "").strip().lower()
    return not origin_host or origin_host != host


class _RateLimiter:
    """Sliding-Window-Zähler: höchstens ``max_events`` Ereignisse in ``window_s``
    Sekunden. Die Zeit wird per ``now`` injiziert, damit er ohne echte Uhr
    testbar ist. ``max_events <= 0`` bedeutet unbegrenzt. Thread-sicher, weil der
    Dashboard-Server mehrere Requests parallel bedienen kann."""

    def __init__(self, max_events: int, window_s: float = 60.0):
        self.max_events = int(max_events)
        self.window_s = float(window_s)
        self._events = deque()
        self._lock = threading.Lock()

    def allow(self, now: float) -> bool:
        if self.max_events <= 0:
            return True
        with self._lock:
            cutoff = now - self.window_s
            while self._events and self._events[0] <= cutoff:
                self._events.popleft()
            if len(self._events) >= self.max_events:
                return False
            self._events.append(now)
            return True


def _resolve_post_route(path: str, config):
    """Reine POST-Routing-/Gating-Entscheidung (ohne Body/IO): was der Handler
    ausführt. Deskriptor:
      ('control', action) | ('ingest', kind) | ('error', code, message).
    Gating über dashboard.controls_enabled / dashboard.ingest_enabled – so ist
    das Freigabe-Verhalten (403) ohne HTTP-Server testbar."""
    if path in ("/api/config/validate", "/api/config/save"):
        from .config_editor import editor_block_reason
        reason = editor_block_reason(config)
        if reason:
            return ("error", 403, f"Konfigurationseditor gesperrt: {reason}")
        return ("config", path.rsplit("/", 1)[-1])
    if path.startswith("/api/control/"):
        if not getattr(config.dashboard, "controls_enabled", False):
            return ("error", 403, "Steuerung deaktiviert (dashboard.controls_enabled)")
        return ("control", path[len("/api/control/"):].strip("/"))
    if path == "/api/whatif":
        # Simulation rechnet einen Solve (CPU) -> wie die Steuerung gated.
        if not getattr(config.dashboard, "controls_enabled", False):
            return ("error", 403, "Steuerung deaktiviert (dashboard.controls_enabled)")
        return ("whatif",)
    if not getattr(config.dashboard, "ingest_enabled", False):
        return ("error", 403, "Ingest deaktiviert (dashboard.ingest_enabled)")
    if not path.startswith("/api/ingest/"):
        return ("error", 404, "Nur /api/ingest/<kind> oder /api/control/<action>")
    return ("ingest", path[len("/api/ingest/"):].strip("/"))


def static_asset_allowed(path: str, dashboard_file: str) -> bool:
    """Darf dieser Pfad statisch aus dem Arbeitsverzeichnis ausgeliefert werden?

    Erlaubnisliste, keine Sperrliste. Das Basisverzeichnis des HTTP-Servers ist
    das Verzeichnis der Dashboard-Datei - im Standardsetup ``/opt/ems``, und dort
    liegen ``config.yaml`` (InfluxDB-, MQTT- und E3DC-Zugangsdaten, Solcast-
    Schluessel), das Overlay und die komplette History-Datenbank. Vorher gab
    ``SimpleHTTPRequestHandler`` jeden Pfad darin heraus; bei leerem
    ``dashboard.password`` ohne jede Anmeldung, und ``dashboard.host`` steht per
    Standard auf 0.0.0.0. Icon und Manifest laufen ueber die "raw"-Route, nicht
    hierueber.

    Weil nur bekannte Namen durchkommen, greifen auch Traversal-Versuche nicht -
    weder ``../`` noch prozentkodierte Varianten.
    """
    name = str(path).split("?")[0].split("#")[0].lstrip("/")
    return name in {dashboard_file, "plotly.min.js"}


def _resolve_get_route(path: str, config, *, has_schedule_runner: bool):
    """Reine GET-Routing-Entscheidung (ohne IO): welchen Endpoint der Handler
    bedient. Deskriptor:
      ('raw', 'manifest'|'icon'|'sw') | ('live',) | ('status', path) |
      ('json', obj, code) | ('schedule',) | ('file', 'data'|'report') |
      ('version',) | None (statische Datei / Index).
    Enthält das Gating für battery-schedule (controls_enabled / Runner
    vorhanden) und api_data.json (api_enabled) – ohne Socket/Datei-IO."""
    if path in ("/config", "/config/", "/api/config.json"):
        from .config_editor import editor_block_reason
        reason = editor_block_reason(config)
        if reason:
            return ("json", {"error": f"Konfigurationseditor gesperrt: {reason}"}, 403)
        return ("config_page",) if path != "/api/config.json" else ("config_data",)
    if path in ("/archiv", "/archiv/", "/archive"):
        return ("archive_page",)
    if path == "/api/archive-runs.json":
        return ("archive_list",)
    if path == "/api/archive-run.json":
        return ("archive_run",)
    if path == "/manifest.webmanifest":
        return ("raw", "manifest")
    if path == "/app-icon.svg":
        return ("raw", "icon")
    if path == "/sw.js":
        return ("raw", "sw")
    if path == "/api/live.json":
        return ("live",)
    if path in ("/api/status.json", "/api/mode-comparison.json", "/api/events.json",
                "/api/savings-history.json", "/api/forecast-accuracy.json",
                "/api/forecast-analysis.json",
                "/api/battery-health.json", "/api/plan-value.json",
                "/api/load-profiles.json"):
        return ("status", path)
    if path == "/api/battery-schedule.json":
        if not getattr(config.dashboard, "controls_enabled", False):
            return ("json", {"status": "disabled"}, 403)
        if not has_schedule_runner:
            return ("json", {"status": "unavailable"}, 503)
        return ("schedule",)
    if getattr(config.dashboard, "api_enabled", False) and path == "/api/data.json":
        return ("file", "data")
    if path == "/version":
        return ("version",)
    if path == "/report.json":
        return ("file", "report")
    if path == "/api/debug-snapshots.json":
        return ("debug_list",)
    return None


def start_dashboard_server(config: Config, publisher=None, e3dc=None,
                           config_path: str = "config.yaml",
                           schedule_runner=None) -> None:
    """Startet im Dienstmodus einen kleinen HTTP-Server, der das Dashboard
    im Browser abrufbar macht (http://<host>:<port>/). Läuft als Daemon-Thread.

    publisher/e3dc: Laufzeit-Mechanik für die interaktive Steuerung
    (/api/control/*); config_path: Basis für die Overlay-Persistenz."""
    import functools
    import http.server
    import os
    import threading
    import base64
    import hmac
    # Spaet ueber ems.main nachschlagen: dort liegt der Laufzeitzustand, und
    # Tests ersetzen Namen per monkeypatch auf ems.main (kein Zirkelimport,
    # weil erst beim Aufruf importiert wird).
    from . import main as _main

    out = os.path.abspath(config.dashboard.output_path)
    directory = os.path.dirname(out) or "."
    fname = os.path.basename(out)
    snap_path = os.path.abspath(config.report.snapshot_path)

    # Ein gemeinsamer Cache für alle Browser: auch bei mehreren geöffneten
    # Dashboards wird E3/DC höchstens einmal je Live-Intervall gepollt. Die
    # E3DCLink-interne Sperre serialisiert diese Abfrage mit Optimierung und
    # Steuer-Watchdog auf derselben persistenten RSCP-Verbindung.
    live_interval = max(0.0, float(getattr(
        config.dashboard, "live_refresh_seconds", 5.0) or 0.0))
    live_guard = threading.Lock()
    live_cache = {"at": 0.0, "data": None}
    daily_cache = {"at": 0.0, "day": None, "data": {}}
    plan_summary_cache = {"mtime": None, "frame": None, "data": {}}
    # Rate-Limit für den realen Steuerpfad (/api/control/*), schützt vor
    # versehentlichem/böswilligem Befehlssturm. 0 = unbegrenzt.
    control_limiter = _RateLimiter(
        getattr(config.dashboard, "control_rate_limit_per_min", 60), 60.0)

    pwa_manifest = json.dumps({
        "name": "E3DC EMS Steuerung", "short_name": "E3DC EMS",
        "description": "Livewerte, Prognose und Steuerung des E3/DC EMS",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#111820", "theme_color": "#1769c2",
        "icons": [{"src": "/app-icon.svg", "sizes": "any",
                   "type": "image/svg+xml", "purpose": "any maskable"}],
    }, ensure_ascii=False).encode("utf-8")
    pwa_icon = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="104" fill="#1769c2"/>
<path d="M292 48 119 285h116l-28 179 186-257H278z" fill="#fff8d8"/>
</svg>'''
    service_worker = b'''const CACHE="e3dc-ems-v1";
self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(["/plotly.min.js","/app-icon.svg"])).then(()=>self.skipWaiting())));
self.addEventListener("activate",e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener("fetch",e=>{const u=new URL(e.request.url);if(u.origin!==location.origin)return;if(u.pathname==="/plotly.min.js"||u.pathname==="/app-icon.svg")e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));});'''

    def _plan_live_summary(now_ts) -> dict:
        """PV-Tagesprognose und Preis aus dem zuletzt erzeugten API-Plan."""
        api_path = os.path.join(directory, "api_data.json")
        try:
            mtime = os.path.getmtime(api_path)
            frame = plan_summary_cache.get("frame")
            if plan_summary_cache["mtime"] != mtime or frame is None:
                with open(api_path, encoding="utf-8") as fh:
                    rows = json.load(fh)
                frame = pd.DataFrame(rows)
                frame["timestamp"] = pd.to_datetime(
                    frame["timestamp"], utc=True, format="ISO8601").dt.tz_convert(
                        config.general.timezone)
            today = frame[frame["timestamp"].dt.date == now_ts.date()]
            pv = (pd.to_numeric(today["pv_w"], errors="coerce").dropna()
                  if "pv_w" in today else pd.Series(dtype=float))
            summary = {
                "pv_forecast_today_kwh": round(
                    float(pv.sum()) * config.general.dt_hours / 1000.0, 2)
                if not pv.empty else None,
                "pv_forecast_until_now_kwh": None,
                "current_price_ct_kwh": None,
            }
            if not today.empty and "pv_w" in today:
                slot = pd.Timedelta(minutes=config.general.slot_minutes)
                expected_wh = 0.0
                for row in today.itertuples():
                    value = pd.to_numeric(getattr(row, "pv_w", None),
                                          errors="coerce")
                    if pd.isna(value):
                        continue
                    overlap = max(pd.Timedelta(0), min(
                        row.timestamp + slot, now_ts) - row.timestamp)
                    expected_wh += float(value) * overlap.total_seconds() / 3600.0
                summary["pv_forecast_until_now_kwh"] = round(
                    expected_wh / 1000.0, 2)
            if "price_ct_kwh" in frame and not frame.empty:
                pos = (frame["timestamp"] - now_ts).abs().idxmin()
                price = pd.to_numeric(frame.loc[pos, "price_ct_kwh"], errors="coerce")
                if pd.notna(price):
                    summary["current_price_ct_kwh"] = round(float(price), 3)
            plan_summary_cache.update(mtime=mtime, frame=frame, data=summary)
            return dict(summary)
        except Exception as exc:
            log.debug("Live-Planwerte nicht lesbar (%s).", exc)
            return dict(plan_summary_cache["data"])

    def _daily_energy_summary(now_ts, mono) -> dict:
        day = now_ts.date().isoformat()
        if (daily_cache["day"] == day and daily_cache["data"]
                and mono - daily_cache["at"] < 60.0):
            return dict(daily_cache["data"])
        try:
            meter = e3dc.read_energy_total(now_ts.normalize(), now_ts)
            if meter:
                summary = {
                    "pv_yield_today_kwh": round(meter["pv_wh"] / 1000.0, 2),
                    "house_consumption_today_kwh": round(
                        meter["load_wh"] / 1000.0, 2),
                    "grid_import_today_kwh": round(
                        meter["grid_import_wh"] / 1000.0, 2),
                    "grid_export_today_kwh": round(
                        meter["grid_export_wh"] / 1000.0, 2),
                    "battery_charge_today_kwh": round(
                        meter["bat_in_wh"] / 1000.0, 2),
                    "battery_discharge_today_kwh": round(
                        meter["bat_out_wh"] / 1000.0, 2),
                    "energy_balance_residual_kwh": (
                        round(meter["balance_residual_wh"] / 1000.0, 3)
                        if meter.get("balance_residual_wh") is not None else None),
                    "energy_balance_ok": meter.get("balance_ok"),
                    "daily_energy_updated": now_ts.isoformat(),
                }
                daily_cache.update(at=mono, day=day, data=summary)
        except Exception as exc:
            log.debug("Live-Tagesenergien nicht lesbar (%s).", exc)
        return dict(daily_cache["data"])

    def _read_dashboard_live():
        if e3dc is None or live_interval <= 0.0:
            return None
        with live_guard:
            mono = _time.monotonic()
            if (live_cache["data"] is not None
                    and mono - live_cache["at"] < live_interval * 0.9):
                return dict(live_cache["data"])
            live = e3dc.read_live(force=True)
            if not live:
                return None
            fields = ("soc_percent", "pv_w", "house_load_w", "grid_w",
                      "battery_w", "wallbox_w")
            data = {k: (float(live[k]) if live.get(k) is not None else None)
                    for k in fields}
            data["updated"] = pd.Timestamp.now(
                tz=config.general.timezone).isoformat()
            now_ts = pd.Timestamp(data["updated"])
            data.update(_plan_live_summary(now_ts))
            data.update(_daily_energy_summary(now_ts, mono))
            actual = data.get("pv_yield_today_kwh")
            expected = data.get("pv_forecast_until_now_kwh")
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                data["pv_deviation_today_kwh"] = round(actual - expected, 2)
                data["pv_deviation_today_percent"] = (
                    round((actual - expected) * 100.0 / expected, 1)
                    if expected >= 0.1 else None)
            data["e3dc_control_enabled"] = bool(
                config.e3dc_rscp.control_enabled)
            # Zusatz-Kacheln: Pool-Ist-Temperatur (wenn thermische Last aktiv)
            # und aktuelle Außentemperatur (Open-Meteo-Cache).
            try:
                from .local_history import read_load_temp, read_temperature
                db = config.e3dc_rscp.history_db_path
                tz = config.general.timezone
                now_ts = pd.Timestamp.now(tz=tz)
                start = now_ts - pd.Timedelta(hours=6)
                pool = next((ld for ld in getattr(config, "controllable_loads", [])
                             if ld.type == "thermal" and ld.enabled and ld.temp_signal),
                            None)
                if pool is not None:
                    s = read_load_temp(db, pool.name, start, now_ts, tz).dropna()
                    data["pool_temp_c"] = float(s.iloc[-1]) if not s.empty else None
                if config.weather.enabled:
                    freq = f"{config.general.slot_minutes}min"
                    st = read_temperature(db, start,
                                          now_ts + pd.Timedelta(minutes=15), tz, freq)
                    st = st[st.index <= now_ts].dropna()
                    data["outdoor_temp_c"] = float(st.iloc[-1]) if not st.empty else None
            except Exception as exc:  # darf die Live-Kacheln nie stören
                log.debug("Live-Zusatztemperaturen nicht lesbar (%s).", exc)
            live_cache["at"], live_cache["data"] = mono, data
            return dict(data)

    def _power_profile(value):
        """Dashboard-Payload -> sichere 15-min-Leistungskurve oder None."""
        if value in (None, ""):
            return None
        if not isinstance(value, (list, tuple)):
            raise ValueError("power_profile_w muss eine Liste sein")
        if len(value) > 192:
            raise ValueError("Leistungskurve darf höchstens 192 Werte enthalten")
        out = [float(x) for x in value]
        if any(not np.isfinite(x) or x < 0.0 or x > 100000.0 for x in out):
            raise ValueError("Leistungskurve enthält ungültige Wattwerte")
        return out or None

    # Kernparameter, die im Dashboard je Lasttyp editierbar sind (Whitelist).
    _LOAD_PARAMS = {
        "thermal": {"target_c": float, "min_c": float, "max_c": float,
                   "surface_m2": float, "solar_absorption": float,
                   "min_on_minutes": int, "min_off_minutes": int},
        "deferrable": {"power_w": float, "runtime_minutes": float,
                       "window_from_hour": int, "window_to_hour": int,
                       "deadline_hours": float,
                       "power_profile_w": _power_profile},
    }

    def _find_load(name):
        for ld in getattr(config, "controllable_loads", []):
            if ld.name == name:
                return ld
        raise ValueError(f"Unbekannte Last: {name!r}")

    def _handle_control(action: str, payload: dict):
        """Dünne Hülle um _dispatch_control (Modulebene) mit den Laufzeit-
        Abhängigkeiten dieses Servers."""
        return _main._dispatch_control(
            action, payload, config=config, publisher=publisher, e3dc=e3dc,
            config_path=config_path, schedule_runner=schedule_runner,
            find_load=_find_load, load_params=_LOAD_PARAMS)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def _authed(self) -> bool:
            """Basic-Auth prüfen (Dashboard-username/password). True = ok; bei
            Fehlschlag wird 401 gesendet und False zurückgegeben."""
            u, p = config.dashboard.username, config.dashboard.password
            if not (u and p):
                return True
            expected = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
            # compare_digest statt ==: der Stringvergleich bricht beim ersten
            # abweichenden Byte ab und verraet damit ueber die Antwortzeit, wie
            # weit ein Rateversuch gekommen ist. Im LAN theoretisch, aber der
            # zeitkonstante Vergleich kostet nichts.
            if hmac.compare_digest(
                    self.headers.get("Authorization") or "", expected):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="EMS"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _reply(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _raw_reply(self, body: bytes, content_type: str, cache="no-cache"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body_json(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self):
            if not self._authed():
                return
            if _cross_site_post(self.headers):
                # CSRF: eine fremde Webseite im Browser eines Heimnetz-Geraets
                # darf keine Steuerbefehle absetzen (gespeicherte Basic-Auth-
                # Daten sendet der Browser sonst mit).
                self._reply({"error": "Fremdseiten-Anfrage abgelehnt"}, 403)
                return
            path = self.path.split("?")[0]
            route = _resolve_post_route(path, config)
            if route[0] == "error":
                self.send_error(route[1], route[2])
                return
            if route[0] == "config":
                from .config_editor import MAX_CONFIG_BYTES, save_document
                from .config_editor import validate_document
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != \
                        "application/json":
                    self._reply({"error": "Content-Type muss application/json sein"}, 415)
                    return
                if self.headers.get("X-EMS-Config") != "1":
                    self._reply({"error": "Sicherheitsheader X-EMS-Config fehlt"}, 403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_CONFIG_BYTES:
                    self._reply(
                        {"error": "Ungültige Größe (maximal 2 MiB)"}, 413)
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                    document = payload.get("config")
                    if route[1] == "validate":
                        result = validate_document(document)
                    else:
                        result = save_document(
                            config_path, document, str(payload.get("revision", "")))
                except RuntimeError as exc:
                    self._reply({"error": str(exc)}, 409)
                    return
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._reply({"error": str(exc)}, 400)
                    return
                except Exception as exc:
                    log.exception("Konfiguration konnte nicht verarbeitet werden")
                    self._reply({"error": f"Speichern fehlgeschlagen: {exc}"}, 500)
                    return
                self._reply({"status": "ok", "result": result})
                if route[1] == "save":
                    _main._record_dashboard_event(
                        config, "config", "Konfiguration im Dashboard gespeichert",
                        details={"backup": result.get("backup")})

                    def _request_restart():
                        log.info("Konfiguration gespeichert – geordneter "
                                 "Dienst-Neustart wird angefordert.")
                        _main._service_restart_requested.set()
                        if solver_is_running():
                            request_solver_cancel()
                        if publisher is not None:
                            publisher.recalc_event.set()

                    threading.Timer(0.5, _request_restart).start()
                return
            # What-if-Simulation: rechnet einen Solve mit geänderten Parametern,
            # ohne zu steuern/persistieren (rate-limitiert wie die Steuerung).
            if route[0] == "whatif":
                if not control_limiter.allow(_time.monotonic()):
                    self.send_error(429, "Zu viele Simulationen – bitte kurz warten")
                    return
                try:
                    result = _main._run_whatif(self._body_json())
                except ValueError as exc:
                    self.send_error(409, f"Simulation nicht möglich: {exc}")
                    return
                except Exception as exc:
                    self.send_error(500, f"Simulationsfehler: {exc}")
                    return
                self._reply({"status": "ok", "result": result})
                return
            # Interaktive Steuerung (/api/control/<action>): Lasten, Modus, Akku.
            if route[0] == "control":
                if not control_limiter.allow(_time.monotonic()):
                    self.send_error(429, "Zu viele Steuerbefehle – bitte kurz warten")
                    return
                action = route[1]
                try:
                    payload = self._body_json()
                    result = _handle_control(action, payload)
                except KeyError:
                    self.send_error(404, f"Unbekannte Steuer-Aktion: {action}")
                    return
                except ValueError as exc:
                    self.send_error(400, f"Steuer-Fehler: {exc}")
                    return
                except Exception as exc:
                    self.send_error(500, f"Steuer-Fehler: {exc}")
                    return
                labels = {
                    "recalc": "Manuelle Planneuberechnung angefordert",
                    "e3dc_control": ("E3/DC-Steuerung aktiviert" if payload.get("enabled")
                                      else "E3/DC-Steuerung deaktiviert"),
                    "mode": f"Optimierungsmodus auf {payload.get('strategy', '')} geändert",
                    "load": f"Last {payload.get('name', '')} geändert",
                    "battery": "Akku-Handbetrieb geändert",
                    "battery_schedule": "Manuelle Akkuplanung geändert",
                }
                _main._record_dashboard_event(
                    config, action, labels.get(action, f"Steueraktion {action}"),
                    details={"action": action})
                self._reply({"status": "ok", "action": action, "result": result})
                return
            # Ingest-API: Live-/Historienwerte extern einspielen (ohne RSCP/Influx).
            kind = route[1]
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                from .ingest import ingest as _do_ingest
                msg = _do_ingest(config, kind, payload)
            except KeyError:
                self.send_error(404, f"Unbekannter Ingest-Typ: {kind}")
                return
            except Exception as exc:
                self.send_error(400, f"Ingest-Fehler: {exc}")
                return
            body = json.dumps({"status": "ok", "kind": kind, "result": msg}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path, content_type, not_found, disposition=None):
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                self.send_error(404, not_found)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authed():
                return
            path = self.path.split("?")[0]
            # Routing + Gating (ohne IO) modulweit in _resolve_get_route -> dort
            # testbar; hier nur noch mit den Laufzeit-Ressourcen ausführen.
            route = _resolve_get_route(
                path, config, has_schedule_runner=schedule_runner is not None)
            if route is not None:
                kind = route[0]
                if kind == "raw":
                    asset = {
                        "manifest": (pwa_manifest,
                                     "application/manifest+json; charset=utf-8", "no-cache"),
                        "icon": (pwa_icon, "image/svg+xml; charset=utf-8",
                                 "public, max-age=86400"),
                        "sw": (service_worker,
                               "application/javascript; charset=utf-8", "no-cache"),
                    }[route[1]]
                    self._raw_reply(asset[0], asset[1], asset[2])
                    return
                if kind == "live":
                    # E3/DC-Livewerte für die Kacheln (startet keinen Optimierer).
                    data = _read_dashboard_live()
                    self._reply(data if data is not None else {"status": "unavailable"},
                                200 if data is not None else 503)
                    return
                if kind == "status":
                    from urllib.parse import parse_qs, urlparse
                    query = parse_qs(urlparse(self.path).query)
                    obj, code = _main._status_api_payload(route[1], config, query)
                    self._reply(obj, code)
                    return
                if kind == "json":
                    self._reply(route[1], route[2])
                    return
                if kind == "config_page":
                    from .config_editor import editor_html
                    self._raw_reply(
                        editor_html(), "text/html; charset=utf-8", "no-store")
                    return
                if kind == "archive_page":
                    from .archive import archive_html
                    self._raw_reply(
                        archive_html(), "text/html; charset=utf-8", "no-store")
                    return
                if kind == "archive_list":
                    from .archive import list_runs
                    try:
                        items = list_runs(config)
                    except Exception:
                        items = []
                    self._reply({"snapshots": items})
                    return
                if kind == "archive_run":
                    from urllib.parse import parse_qs, urlparse

                    from .archive import run_detail
                    q = parse_qs(urlparse(self.path).query)
                    ts = (q.get("ts") or [None])[0]
                    try:
                        detail = run_detail(config, ts)
                    except Exception as exc:
                        log.exception("Archivlauf nicht aufbereitbar")
                        self._reply({"error": str(exc)}, 500)
                        return
                    if detail is None:
                        self._reply({"error": "Kein Lauf zu diesem Zeitpunkt"}, 404)
                        return
                    self._reply(detail)
                    return
                if kind == "config_data":
                    from .config_editor import editor_payload
                    try:
                        self._reply(editor_payload(config_path))
                    except Exception as exc:
                        log.exception("Konfiguration konnte nicht gelesen werden")
                        self._reply({"error": str(exc)}, 500)
                    return
                if kind == "schedule":
                    self._reply(schedule_runner.snapshot())
                    return
                if kind == "version":
                    # mtime der Dashboard-Datei; die Seite pollt und lädt bei Änderung neu.
                    try:
                        body = ("%.0f" % os.path.getmtime(out)).encode()
                    except OSError:
                        body = b"0"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if kind == "file" and route[1] == "data":
                    self._send_file(os.path.join(directory, "api_data.json"),
                                    "application/json; charset=utf-8",
                                    "Noch keine API-Daten vorhanden")
                    return
                if kind == "debug_list":
                    from .local_history import list_debug_snapshots
                    try:
                        items = list_debug_snapshots(
                            config.e3dc_rscp.history_db_path,
                            config.general.timezone)
                    except Exception:
                        items = []
                    self._reply({"snapshots": items})
                    return
                if kind == "file" and route[1] == "report":
                    # Ohne ?ts= den letzten Lauf (Datei, Abwärtskompatibilität);
                    # mit ?ts=<generated> einen ÄLTEREN Lauf aus dem Verlauf.
                    from urllib.parse import parse_qs, urlparse
                    q = parse_qs(urlparse(self.path).query)
                    ts = (q.get("ts") or [None])[0]
                    if ts:
                        from .local_history import read_debug_snapshot
                        snap = read_debug_snapshot(
                            config.e3dc_rscp.history_db_path, ts)
                        if not snap:
                            self.send_error(404, "Kein Schnappschuss zu diesem Zeitpunkt")
                            return
                        body = json.dumps(snap, ensure_ascii=False,
                                          default=str).encode("utf-8")
                        safe = re.sub(r"[^0-9T]", "-", ts)[:19]
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header(
                            "Content-Disposition",
                            f'attachment; filename="ems_debug_{safe}.json"')
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self._send_file(snap_path, "application/json; charset=utf-8",
                                    "Noch kein Debug-Schnappschuss vorhanden",
                                    'attachment; filename="last_run_debug.json"')
                    return
            if self.path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
                self.path = "/" + fname
            if not static_asset_allowed(self.path, fname):
                self.send_error(404, "Not Found")
                return
            return super().do_GET()

        def end_headers(self):
            # HTML/Version immer revalidieren, damit reload() die neue Datei
            # holt; das lokale plotly.min.js darf der Browser cachen.
            if not self.path.endswith(".js"):
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            super().end_headers()

        def log_message(self, *a):  # ruhig bleiben
            pass

    handler = functools.partial(Handler, directory=directory)
    try:
        srv = http.server.ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)
    except OSError as exc:
        log.warning("Dashboard-Server konnte nicht starten (%s).", exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Dashboard-Server läuft: http://%s:%d/ (Datei: %s)",
             config.dashboard.host, config.dashboard.port, fname)
