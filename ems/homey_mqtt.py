"""MQTT-Ausgabe der Steuerbefehle an Homey.

Publiziert die Sollwerte des aktuell laufenden Slots als Einzeltopics
(damit Homey-Flows sie über die MQTT-Client-App direkt auf Geräte-Capabilities
mappen können) sowie optional die komplette 48-h-Steuertabelle als JSON.

Fail-safe: Sollwerte werden bewusst OHNE Retain-Flag publiziert. Fällt das EMS
aus, liefert der Broker Neu-Verbindern keine veralteten Steuerbefehle mehr aus;
Homey erhält dann schlicht keine Updates und kann (per Flow-Watchdog auf das
Alter von setpoint/updated) in den Eigenverbrauchs-Automatikmodus zurückfallen.
Nur die Zeitplan-Tabelle (ems/schedule, reine Info) wird gemäß mqtt.retain
retained.

Liveness: Die Verbindung bleibt im Loop-Betrieb stehen. Beim Connect wird ein
Last Will hinterlegt (ems/status = "offline", retained) und nach dem Connect
ems/status = "online" (retained) publiziert. Stirbt der Prozess abnormal
(Absturz, Stromausfall, kill), setzt der BROKER den Status selbst auf
"offline" - Homey kann direkt darauf triggern statt nur auf Update-Alter.
close() setzt "offline" beim regulären Beenden.

Topic-Schema (base_topic = "ems"):
  ems/setpoint/batt_charge_limit_w      Ladelimit (Hardware-Max = frei laufen)
  ems/setpoint/batt_discharge_limit_w   Entladelimit
  ems/setpoint/batt_grid_charge_w       Netzladen erzwingen
  ems/setpoint/batt_grid_discharge_w    Netz-Entladen
  ems/setpoint/charge_limited           true/false
  ems/setpoint/discharge_limited        true/false
  ems/setpoint/car_charge_w
  ems/setpoint/mode                     "auto" | "grid_charge" | "hold" | ...
  ems/setpoint/updated                  ISO-Zeitstempel des Slots
  ems/schedule                          komplette Tabelle als JSON (retained)
  ems/status                            "online" | "offline" (retained, Last Will)
  ems/alert                             Störungen als JSON {level, message, time}
  ems/cmd/recalc      EINGEHEND: sofortige Neuberechnung anstoßen (Payload egal)
  ems/cmd/car_boost   EINGEHEND: "1"/"0" - Auto sofort mit Max-Leistung laden
                      (überschreibt car_charge_w, bis Ziel-SoC erreicht/Boost aus)
  ems/cmd/car_departure_time  EINGEHEND: "HH:MM" - Abfahrtzeit für die
                      Optimierung setzen; ""/"default" = zurück auf Konfig-
                      wert; "off"/"urlaub" = Urlaubsmodus: KEINE Abfahrten ->
                      der Ziel-SoC wird nicht mehr erzwungen (Auto lädt nur
                      noch, wenn es sich lohnt, z.B. bei Negativpreisen).
                      Von Homey mit Retain senden -> übersteht EMS-Neustarts.
  ems/cmd/car_target_soc  EINGEHEND: Ziel-SoC in % (1..100); ""/"default" =
                      zurück auf Konfigurationswert. Ebenfalls retained senden.
  ems/cmd/min_soc     EINGEHEND: Haus-Akku Minimum-SoC in % (z.B. Reserve vor
                      Sturm/Ausfall hochsetzen); ""/"default" = Konfigwert.
  ems/cmd/max_soc     EINGEHEND: Haus-Akku Maximum-SoC in % (Akku schonen);
                      ""/"default" = Konfigwert. min >= max wird verworfen.
  ems/vehicle/departure_time, ems/vehicle/target_soc_percent,
  ems/battery/min_soc_percent, ems/battery/max_soc_percent
                      AUSGEHEND: aktuell wirksame Werte (Rückmeldung für Homey).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, time as dtime
from typing import Dict, Optional

import pandas as pd

from .config import Config

log = logging.getLogger("ems.mqtt")


def _slug(name: str) -> str:
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "load"

_RESET_WORDS = ("", "-", "auto", "default", "reset")
_OFF_WORDS = ("off", "aus", "keine", "none", "urlaub", "holiday")


class HomeyMqttPublisher:
    def __init__(self, config: Config):
        self.cfg = config.mqtt
        self.vehicle = config.vehicle
        self._client = None
        self._connected_at = None
        # Von Homey per ems/cmd/# steuerbar:
        self.recalc_event = threading.Event()
        self.car_boost = False
        self.departure_override: Optional[dtime] = None
        self.departure_disabled = False   # Urlaubsmodus: keine Abfahrten
        self.target_soc_override: Optional[float] = None
        self._veh_defaults = (config.vehicle.departure_time,
                              config.vehicle.target_soc_percent,
                              config.vehicle.departure_times)
        self.battery = config.house_battery
        self.min_soc_override: Optional[float] = None
        self.max_soc_override: Optional[float] = None
        self._batt_defaults = (config.house_battery.min_soc_percent,
                               config.house_battery.max_soc_percent)
        # Steuerbare Lasten: MQTT-Ist-Temperaturen (thermische Lasten) cachen.
        self.loads = getattr(config, "controllable_loads", [])
        self._tz = config.general.timezone
        self.load_temps: Dict[str, float] = {}
        # temp_signal (MQTT-Topic der Ist-Temperatur) unabhängig von enabled
        # abonnieren, damit ein per MQTT nachträglich aktivierter Pool sofort
        # einen Startwert hat.
        self._temp_topics = [ld.temp_signal for ld in self.loads
                             if ld.type == "thermal" and ld.temp_signal]
        self.load_feedback: Dict[str, dict] = {}
        # Ein gemeinsamer Leistungsmesser darf mehrere Stufen ableiten, z.B.
        # Gesamtleistung >10 W = Grundstufe und >700 W = zweite Stufe.
        self._feedback_topics: Dict[str, list[tuple]] = {}
        for ld in self.loads:
            if ld.type != "thermal":
                continue
            for st in ld.stages:
                label = f"{ld.name}/{st.name}"
                if st.feedback_topic:
                    self._feedback_topics.setdefault(st.feedback_topic, []).append(
                        (label, "state", st.feedback_on_threshold_w))
                if st.power_topic:
                    self._feedback_topics.setdefault(st.power_topic, []).append(
                        (label, "power", st.feedback_on_threshold_w))
        # Enable/Disable je Last per ems/cmd/load/<slug>; leer = Konfigwert.
        self.load_overrides: Dict[str, bool] = {}
        self._load_defaults = {_slug(ld.name): ld.enabled for ld in self.loads}

    def _connect(self):
        import socket
        import paho.mqtt.client as mqtt

        # Schnelle Erreichbarkeitsprüfung -> klarer, sofortiger Fehler statt
        # langem Blockieren, falls der Broker nicht erreichbar ist.
        socket.create_connection((self.cfg.host, self.cfg.port), timeout=5).close()

        # paho-mqtt >= 2.0: Callback-API v2; ältere Versionen kennen den
        # Parameter nicht -> Fallback auf die alte Signatur.
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            client = mqtt.Client()
        if self.cfg.username:
            client.username_pw_set(self.cfg.username, self.cfg.password)
        # Kommandos von Homey entgegennehmen; on_connect abonniert (auch nach
        # einem automatischen Reconnect) das cmd-Topic neu.
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        # Last Will: der Broker setzt den Status selbst auf "offline", sobald
        # die Verbindung abnormal endet (Prozess-Absturz, Stromausfall, Netz-
        # verlust). retained, damit auch spätere Abonnenten den Zustand sehen.
        status_topic = f"{self.cfg.base_topic}/status"
        client.will_set(status_topic, "offline", qos=1, retain=True)
        client.connect(self.cfg.host, self.cfg.port, keepalive=60)
        client.loop_start()
        info = client.publish(status_topic, "online", qos=1, retain=True)
        try:
            info.wait_for_publish(timeout=5)
        except Exception:  # pragma: no cover
            pass
        log.info("MQTT verbunden, Status 'online' (Last Will: %s = offline).",
                 status_topic)
        return client

    def get_load_temp(self, topic: str) -> Optional[float]:
        """Zuletzt per MQTT empfangene Ist-Temperatur zu einem Topic (oder None)."""
        return self.load_temps.get(topic)

    def get_load_feedback(self, label: str, max_age_minutes: float = 20.0
                          ) -> Optional[dict]:
        """Letzte echte Stufenrückmeldung mit Frischebewertung."""
        item = self.load_feedback.get(label)
        if not item:
            return None
        out = dict(item)
        now = pd.Timestamp.now(tz="UTC")
        limit_s = max(0.0, float(max_age_minutes)) * 60.0
        power_ts, state_ts = out.get("power_updated"), out.get("state_updated")
        power_age = ((now - power_ts).total_seconds() if power_ts is not None
                     else float("inf"))
        state_age = ((now - state_ts).total_seconds() if state_ts is not None
                     else float("inf"))
        if out.get("power_w") is not None and power_age <= limit_s:
            out["on"] = bool(float(out["power_w"]) >=
                             out.get("threshold_w", 50.0))
            age_s = power_age
        else:
            age_s = state_age
        out["age_seconds"] = max(0.0, age_s)
        out["fresh"] = age_s <= limit_s and out.get("on") is not None
        return out

    def all_load_feedback(self) -> dict:
        out = {}
        for ld in self.loads:
            if ld.type != "thermal":
                continue
            for st in ld.stages:
                label = f"{ld.name}/{st.name}"
                item = self.get_load_feedback(label, ld.feedback_max_age_minutes)
                if item is not None:
                    out[label] = item
        return out

    def _load_lanes(self):
        """Alle konfigurierten steuerbaren Lasten als [(label, column, enabled,
        topic)] – auch deaktivierte, damit deren Zustand (aus) publiziert wird."""
        from .loads import _slug as _col
        lanes = []
        for ld in self.loads:
            if ld.type == "thermal":
                sg = _col(ld.name)
                for st in ld.stages:
                    lanes.append({"label": f"{ld.name}/{st.name}",
                                  "column": f"load_{sg}_{_col(st.name)}_w",
                                  "enabled": ld.enabled, "topic": st.control_topic,
                                  "thermostat": bool(ld.thermostat),
                                  "temp_signal": ld.temp_signal,
                                  "target_c": float(ld.target_c)})
            else:
                lanes.append({"label": ld.name,
                              "column": f"load_{_col(ld.name)}_w",
                              "enabled": ld.enabled, "topic": ld.control_topic})
        return lanes

    def _lane_command(self, lane: dict, planned_on: bool) -> int:
        """Schaltbefehl einer Last-Stufe aus dem Heizplan ableiten.

        Bei Lasten mit EIGENEM Thermostat (thermostat: true) ist das Signal eine
        Heiz-FREIGABE: liegt die Ist-Temperatur auf/über target_c, hält der
        Thermostat die WP ohnehin aus -> Freigabe bleibt AN (weniger Schalt-
        spiele; heizt sofort, falls die Temperatur unerwartet fällt). "Aus"
        nur, wenn Heizen aktiv verhindert werden soll (kein Heiz-Slot geplant
        UND T unter target_c, d.h. der Thermostat WÜRDE sonst heizen)."""
        if planned_on:
            return 1
        if lane.get("thermostat") and lane.get("temp_signal"):
            t_ist = self.get_load_temp(lane["temp_signal"])
            if t_ist is not None and float(t_ist) >= lane.get("target_c", 1e9):
                return 1
        return 0

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        # Signatur kompatibel zu Callback-API v1 (4 Argumente) und v2 (5).
        self._connected_at = pd.Timestamp.now(tz="UTC")
        client.subscribe(f"{self.cfg.base_topic}/cmd/#", qos=1)
        for topic in self._temp_topics:      # Pool-/Puffer-Ist-Temperatur
            client.subscribe(topic, qos=1)
        for topic in self._feedback_topics:
            client.subscribe(topic, qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", "replace").strip().lower()
        except Exception:  # pragma: no cover
            payload = ""
        if msg.topic in self._temp_topics:   # Ist-Temperatur einer thermischen Last
            try:
                self.load_temps[msg.topic] = float(payload.replace(",", "."))
            except ValueError:
                pass
            return
        if msg.topic in self._feedback_topics:
            try:
                now = pd.Timestamp.now(tz="UTC")
                numeric = (float(payload.replace(",", "."))
                           if any(kind == "power" for _, kind, _
                                  in self._feedback_topics[msg.topic]) else None)
                for label, kind, threshold in self._feedback_topics[msg.topic]:
                    item = self.load_feedback.setdefault(label, {
                        "on": None, "power_w": None, "threshold_w": threshold})
                    item["threshold_w"] = threshold
                    if kind == "power":
                        item["power_w"] = numeric
                        item["power_updated"] = now
                    else:
                        if payload in ("1", "true", "on", "an", "ein", "running"):
                            item["on"] = True
                        elif payload in ("0", "false", "off", "aus", "stopped"):
                            item["on"] = False
                        else:
                            item["on"] = float(payload.replace(",", ".")) > 0.5
                        item["state_updated"] = now
                    item["updated"] = now
            except (TypeError, ValueError):
                log.warning("Ungültige Last-Rückmeldung %s=%r.", msg.topic, payload)
            return
        if "/cmd/load/" in msg.topic:         # steuerbare Last an/aus (enable/disable)
            slug = msg.topic.split("/cmd/load/", 1)[1].strip("/").lower()
            if payload in _RESET_WORDS:
                self.load_overrides.pop(slug, None)
                log.info("MQTT: Last '%s' -> Konfigurationswert.", slug)
            else:
                on = payload in ("1", "true", "on", "an", "ein", "enable", "enabled")
                self.load_overrides[slug] = on
                log.info("MQTT: Last '%s' %s.", slug, "aktiviert" if on else "deaktiviert")
            self.recalc_event.set()
            return
        if msg.topic.endswith("/cmd/recalc"):
            log.info("MQTT-Kommando: sofortige Neuberechnung angefordert.")
            self.recalc_event.set()
        elif msg.topic.endswith("/cmd/car_boost"):
            self.car_boost = payload in ("1", "true", "on", "an")
            log.info("MQTT-Kommando: car_boost = %s.", self.car_boost)
            self.recalc_event.set()   # Sollwerte sofort neu publizieren
        elif msg.topic.endswith("/cmd/car_departure_time"):
            if payload in _OFF_WORDS:
                self.departure_disabled = True
                self.departure_override = None
                log.info("MQTT-Kommando: Abfahrten deaktiviert (Urlaubsmodus) – "
                         "Ziel-SoC wird nicht mehr erzwungen.")
            elif payload in _RESET_WORDS:
                self.departure_disabled = False
                self.departure_override = None
                log.info("MQTT-Kommando: Abfahrtzeit zurück auf Konfigwert (%s).",
                         self._veh_defaults[0].strftime("%H:%M"))
            else:
                try:
                    hh, mm = payload.split(":")[:2]
                    dep = dtime(int(hh), int(mm))
                except (ValueError, IndexError):
                    log.warning("MQTT-Kommando: ungültige Abfahrtzeit '%s' "
                                "(erwartet HH:MM, 'off' oder 'default').", payload)
                    return
                self.departure_override = dep
                self.departure_disabled = False
                log.info("MQTT-Kommando: Abfahrtzeit = %s.", dep.strftime("%H:%M"))
            self.recalc_event.set()
        elif msg.topic.endswith("/cmd/car_target_soc"):
            if payload in _RESET_WORDS:
                self.target_soc_override = None
                log.info("MQTT-Kommando: Ziel-SoC zurück auf Konfigwert (%.0f %%).",
                         self._veh_defaults[1])
            else:
                try:
                    v = float(payload.replace(",", ".").rstrip("%"))
                except ValueError:
                    log.warning("MQTT-Kommando: ungültiger Ziel-SoC '%s'.", payload)
                    return
                if not 1.0 <= v <= 100.0:
                    log.warning("MQTT-Kommando: Ziel-SoC %.0f außerhalb 1..100 %%.", v)
                    return
                self.target_soc_override = v
                log.info("MQTT-Kommando: Ziel-SoC = %.0f %%.", v)
            self.recalc_event.set()
        elif msg.topic.endswith("/cmd/min_soc"):
            self._set_batt_soc_override("min", payload)
        elif msg.topic.endswith("/cmd/max_soc"):
            self._set_batt_soc_override("max", payload)

    def _set_batt_soc_override(self, which: str, payload: str) -> None:
        attr = f"{which}_soc_override"
        if payload in _RESET_WORDS:
            setattr(self, attr, None)
            log.info("MQTT-Kommando: Haus-Akku %s_soc zurück auf Konfigwert.", which)
        else:
            try:
                v = float(payload.replace(",", ".").rstrip("%"))
            except ValueError:
                log.warning("MQTT-Kommando: ungültiger %s_soc '%s'.", which, payload)
                return
            if not 0.0 <= v <= 100.0:
                log.warning("MQTT-Kommando: %s_soc %.0f außerhalb 0..100 %%.", which, v)
                return
            setattr(self, attr, v)
            log.info("MQTT-Kommando: Haus-Akku %s_soc = %.0f %%.", which, v)
        self.recalc_event.set()

    def apply_battery_overrides(self, hb) -> None:
        """Überträgt die per MQTT gesetzten Haus-Akku-SoC-Grenzen (oder die
        Konfigurations-Standardwerte) auf die Konfiguration des nächsten Laufs.
        Inkonsistente Grenzen (min >= max) werden verworfen."""
        mn = (self.min_soc_override if self.min_soc_override is not None
              else self._batt_defaults[0])
        mx = (self.max_soc_override if self.max_soc_override is not None
              else self._batt_defaults[1])
        if mn >= mx:
            log.warning("Ungültige SoC-Grenzen (min %.0f >= max %.0f %%) – "
                        "nutze Konfigurationswerte.", mn, mx)
            mn, mx = self._batt_defaults
        hb.min_soc_percent = mn
        hb.max_soc_percent = mx

    def apply_vehicle_overrides(self, veh) -> None:
        """Überträgt die per MQTT gesetzten Overrides (oder die Konfigurations-
        Standardwerte) auf die Fahrzeug-Konfiguration des nächsten Laufs.
        Ein departure_time-Override gilt für ALLE Wochentage (übersteuert
        auch die Je-Wochentag-Tabelle departure_times). Urlaubsmodus ('off'):
        keine Abfahrten -> kein Ziel-SoC-Zwang."""
        if self.departure_disabled:
            veh.departure_time = self._veh_defaults[0]
            veh.departure_times = {d: None for d in range(7)}
        elif self.departure_override is not None:
            veh.departure_time = self.departure_override
            veh.departure_times = None
        else:
            veh.departure_time = self._veh_defaults[0]
            veh.departure_times = self._veh_defaults[2]
        veh.target_soc_percent = (self.target_soc_override
                                  if self.target_soc_override is not None
                                  else self._veh_defaults[1])

    def apply_load_overrides(self, loads) -> None:
        """Überträgt per MQTT (ems/cmd/load/<slug>) gesetzte Enable/Disable-
        Overrides auf die steuerbaren Lasten; sonst der Konfigurationswert."""
        for ld in loads or []:
            slug = _slug(ld.name)
            if slug in self.load_overrides:
                ld.enabled = self.load_overrides[slug]
            else:
                ld.enabled = self._load_defaults.get(slug, ld.enabled)

    def wait_for_recalc(self, timeout: float) -> bool:
        """Wartet bis zu `timeout` Sekunden; True, wenn währenddessen per
        MQTT-Kommando eine sofortige Neuberechnung angefordert wurde."""
        triggered = self.recalc_event.wait(max(0.0, timeout))
        if triggered:
            self.recalc_event.clear()
        return triggered

    def publish_alert(self, level: str, message: str) -> None:
        """Publiziert eine Störung/Warnung (ems/alert) für Homey-Push-Flows."""
        if not self.cfg.enabled:
            return
        try:
            self._ensure_connected()
            payload = json.dumps({
                "level": level, "message": message,
                # stdlib-datetime: astimezone() ohne Argument hängt die lokale
                # Zeitzone an (pd.Timestamp.astimezone() verlangt zwingend ein tz
                # und warf sonst bei JEDEM Alarm -> Alarme kamen nie an).
                "time": datetime.now().astimezone().isoformat(),
            })
            self._pub(f"{self.cfg.base_topic}/alert", payload, retain=False)
            log.info("MQTT-Alarm publiziert: [%s] %s", level, message)
        except Exception as exc:
            log.warning("MQTT-Alarm fehlgeschlagen (%s).", exc)

    def _ensure_connected(self) -> None:
        """Persistente Verbindung: nur (neu) verbinden, wenn nötig."""
        if self._client is not None:
            try:
                if self._client.is_connected():
                    return
            except Exception:  # pragma: no cover
                pass
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # pragma: no cover
                pass
            self._client = None
            log.warning("MQTT-Verbindung verloren – verbinde neu.")
        self._client = self._connect()

    def start(self) -> None:
        """Empfangsverbindung früh starten, bevor der erste EMS-Lauf rechnet."""
        if self.cfg.enabled:
            self._ensure_connected()

    def feedback_alarm_ready(self, grace_seconds: float = 30.0) -> bool:
        """Fehlalarme vermeiden, solange MQTT nach Start/Reconnect anläuft."""
        if self._connected_at is None:
            return False
        age = (pd.Timestamp.now(tz="UTC") - self._connected_at).total_seconds()
        return age >= max(0.0, float(grace_seconds))

    def close(self) -> None:
        """Sauber beenden: Status 'offline' setzen und Verbindung trennen
        (beim regulären Disconnect verschickt der Broker den Last Will nicht)."""
        if self._client is None:
            return
        try:
            info = self._client.publish(f"{self.cfg.base_topic}/status",
                                        "offline", qos=1, retain=True)
            info.wait_for_publish(timeout=5)
        except Exception:  # pragma: no cover
            pass
        try:
            self._client.loop_stop()
            self._client.disconnect()
        finally:
            self._client = None

    def _pub(self, topic: str, payload, retain: bool) -> None:
        info = self._client.publish(topic, payload, qos=self.cfg.qos, retain=retain)
        # Auf Zustellbestätigung warten: ohne Retain gibt es keine zweite Chance
        # über den Broker-Speicher, daher darf disconnect() den Versand nicht
        # abschneiden.
        try:
            info.wait_for_publish(timeout=5)
        except Exception:  # pragma: no cover - ältere paho-Versionen ohne timeout
            pass

    def publish(self, table: pd.DataFrame, current_ts: pd.Timestamp,
                load_mqtt_map=None):
        """Publiziert Sollwerte des aktuellen Slots und optional die Tabelle.
        Rückgabe: publizierte Last-Befehle {Lane-Label: 1|0|'aus'} (leer, wenn
        MQTT aus) - z.B. fürs load_cmd-Log der Thermomodell-Kalibrierung."""
        loadsp: Dict[str, object] = {}
        if not self.cfg.enabled:
            log.info("MQTT deaktiviert – überspringe Publish.")
            return loadsp

        base = self.cfg.base_topic
        self._ensure_connected()
        # aktuellen Slot bestimmen
        idx = table.index
        pos = idx.get_indexer([current_ts], method="ffill")[0]
        if pos < 0:
            pos = 0
        row = table.iloc[pos]

        if self.cfg.publish_setpoints:
            # E3DC-Steuerbefehle: Limits nur, wenn tatsächlich begrenzt werden
            # soll; sonst Hardware-Maximum ("frei laufen"). grid_charge_w > 0
            # erzwingt Netzladen.
            charge_limit = float(row["batt_charge_limit_w"])
            dis_limit = float(row["batt_discharge_limit_w"])
            grid_charge = float(row["batt_grid_charge_w"])
            grid_discharge = float(row.get("batt_grid_discharge_w", 0.0))
            setpoints: Dict[str, object] = {
                "batt_charge_limit_w": charge_limit,
                "batt_discharge_limit_w": dis_limit,
                "batt_grid_charge_w": grid_charge,        # Netzladen (Akku <- Netz)
                "batt_grid_discharge_w": grid_discharge,  # Netz-Entladen (Akku -> Netz)
                "charge_limited": bool(row["charge_limited"]),
                "discharge_limited": bool(row["discharge_limited"]),
                "car_charge_w": float(row.get("car_charge_w", 0.0)),
                "mode": str(row["mode"]),
                "updated": idx[pos].isoformat(),
            }
            # car_boost (per ems/cmd/car_boost): Auto sofort mit Max-Leistung
            # laden, bis der Ziel-SoC erreicht ist - unabhängig vom Optimierer.
            if self.car_boost:
                car_soc = row.get("car_soc_percent")
                if car_soc is not None and pd.notna(car_soc) and \
                        float(car_soc) >= self.vehicle.target_soc_percent:
                    self.car_boost = False
                    log.info("car_boost: Ziel-SoC erreicht – Boost beendet.")
                else:
                    setpoints["car_charge_w"] = float(self.vehicle.max_charge_w)
                    log.info("car_boost aktiv: car_charge_w = %.0f W erzwungen.",
                             self.vehicle.max_charge_w)

            # Fail-safe: Sollwerte NIE retainen (s. Modul-Docstring).
            for key, value in setpoints.items():
                self._pub(f"{base}/setpoint/{key}", value, retain=False)
            log.info("MQTT Steuerbefehle publiziert (Slot %s): %s", idx[pos], setpoints)

            # Steuerbare Lasten: on/off je Slot unter ems/loads/<name> für JEDE
            # konfigurierte Last (auch deaktiviert -> 0). Diese Monitoring-Topics
            # werden RETAINED (immer sichtbar, wie das ems/loads-JSON). Der
            # eigentliche Schaltbefehl ans externe Topic bleibt fail-safe (nur bei
            # aktiver Last, nie retained).
            lanes = self._load_lanes()
            if lanes:
                for e in lanes:
                    col, en = e["column"], e["enabled"]
                    planned = bool(en and col in table.columns
                                   and float(row[col]) > 5.0)
                    on = self._lane_command(e, planned) if en else 0
                    self._pub(f"{base}/loads/{_slug(e['label'])}", on,
                              retain=self.cfg.retain)
                    if en and e.get("topic"):
                        self._pub(e["topic"], on, retain=False)
                    loadsp[e["label"]] = on if en else "aus"
                log.info("MQTT Last-Sollwerte publiziert: %s", loadsp)

            # Wirksame Fahrzeug-Parameter zurückmelden (Rückmeldung für Homey,
            # z.B. nach ems/cmd/car_departure_time bzw. /target_soc).
            dep_str = ("off" if not self.vehicle.has_any_departure
                       else self.vehicle.departure_time.strftime("%H:%M"))
            self._pub(f"{base}/vehicle/departure_time", dep_str,
                      retain=self.cfg.retain)
            self._pub(f"{base}/vehicle/target_soc_percent",
                      float(self.vehicle.target_soc_percent),
                      retain=self.cfg.retain)
            self._pub(f"{base}/battery/min_soc_percent",
                      float(self.battery.min_soc_percent), retain=self.cfg.retain)
            self._pub(f"{base}/battery/max_soc_percent",
                      float(self.battery.max_soc_percent), retain=self.cfg.retain)

        if self.cfg.publish_schedule_json:
            # Schlank halten: Homey kappt große Payloads. Nur konfigurierte
            # Felder, optional auf schedule_max_hours begrenzt, kompaktes JSON.
            payload = table
            if self.cfg.schedule_max_hours and len(payload) > 1:
                end = current_ts + pd.Timedelta(hours=self.cfg.schedule_max_hours)
                payload = payload[payload.index <= end]
            cols = [c for c in self.cfg.schedule_fields if c in payload.columns]

            def _conv(v):
                if isinstance(v, str):
                    return v
                fv = float(v)
                return round(fv, 1) if pd.notna(fv) else None

            payload_json = {
                "generated": pd.Timestamp.now(tz=idx.tz).isoformat(),
                "slots": [
                    {"time": ts.isoformat(),
                     **{c: _conv(r[c]) for c in cols}}
                    for ts, r in payload.iterrows()
                ],
            }
            data = json.dumps(payload_json, separators=(",", ":"))
            self._pub(f"{base}/schedule", data, retain=self.cfg.retain)
            log.info("MQTT Zeitplan (%d Slots, %d Felder, %.0f KB) publiziert.",
                     len(payload), len(cols), len(data) / 1024)

            # Steuerbare Lasten als eigene JSON-Liste (wie schedule): aktueller
            # Sollwert + Zeitplan (on/off) je Last – inkl. deaktivierter (enabled).
            lanes = self._load_lanes()
            if lanes:
                lp = table
                if self.cfg.schedule_max_hours and len(lp) > 1:
                    end = current_ts + pd.Timedelta(hours=self.cfg.schedule_max_hours)
                    lp = lp[lp.index <= end]

                def _on(r, e):
                    return (1 if (e["enabled"] and e["column"] in table.columns
                                  and float(r[e["column"]]) > 5.0) else 0)

                loads_json = {
                    "generated": pd.Timestamp.now(tz=idx.tz).isoformat(),
                    "current": [
                        {"name": e["label"], "enabled": e["enabled"],
                         "topic": e.get("topic"), "on": _on(row, e),
                         "power_w": (round(float(row[e["column"]]), 1)
                                     if (e["enabled"] and e["column"] in table.columns)
                                     else 0.0)}
                        for e in lanes],
                    "slots": [
                        {"time": ts.isoformat(),
                         **{_slug(e["label"]): _on(r, e) for e in lanes}}
                        for ts, r in lp.iterrows()],
                }
                self._pub(f"{base}/loads", json.dumps(loads_json, separators=(",", ":")),
                          retain=self.cfg.retain)
        return loadsp
