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
  ems/cmd/departure_time  EINGEHEND: "HH:MM" - Abfahrtzeit für die Optimierung
                      setzen; ""/"default" = zurück auf Konfigurationswert.
                      Von Homey mit Retain senden -> übersteht EMS-Neustarts.
  ems/cmd/target_soc  EINGEHEND: Ziel-SoC in % (1..100); ""/"default" = zurück
                      auf Konfigurationswert. Ebenfalls retained senden.
  ems/vehicle/departure_time, ems/vehicle/target_soc_percent
                      AUSGEHEND: aktuell wirksame Werte (Rückmeldung für Homey).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import time as dtime
from typing import Dict, Optional

import pandas as pd

from .config import Config

log = logging.getLogger("ems.mqtt")

_RESET_WORDS = ("", "-", "auto", "default", "reset")


class HomeyMqttPublisher:
    def __init__(self, config: Config):
        self.cfg = config.mqtt
        self.vehicle = config.vehicle
        self._client = None
        # Von Homey per ems/cmd/# steuerbar:
        self.recalc_event = threading.Event()
        self.car_boost = False
        self.departure_override: Optional[dtime] = None
        self.target_soc_override: Optional[float] = None
        self._veh_defaults = (config.vehicle.departure_time,
                              config.vehicle.target_soc_percent)

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

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        # Signatur kompatibel zu Callback-API v1 (4 Argumente) und v2 (5).
        client.subscribe(f"{self.cfg.base_topic}/cmd/#", qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", "replace").strip().lower()
        except Exception:  # pragma: no cover
            payload = ""
        if msg.topic.endswith("/cmd/recalc"):
            log.info("MQTT-Kommando: sofortige Neuberechnung angefordert.")
            self.recalc_event.set()
        elif msg.topic.endswith("/cmd/car_boost"):
            self.car_boost = payload in ("1", "true", "on", "an")
            log.info("MQTT-Kommando: car_boost = %s.", self.car_boost)
            self.recalc_event.set()   # Sollwerte sofort neu publizieren
        elif msg.topic.endswith("/cmd/departure_time"):
            if payload in _RESET_WORDS:
                self.departure_override = None
                log.info("MQTT-Kommando: Abfahrtzeit zurück auf Konfigwert (%s).",
                         self._veh_defaults[0].strftime("%H:%M"))
            else:
                try:
                    hh, mm = payload.split(":")[:2]
                    self.departure_override = dtime(int(hh), int(mm))
                except (ValueError, IndexError):
                    log.warning("MQTT-Kommando: ungültige Abfahrtzeit '%s' "
                                "(erwartet HH:MM).", payload)
                    return
                log.info("MQTT-Kommando: Abfahrtzeit = %s.",
                         self.departure_override.strftime("%H:%M"))
            self.recalc_event.set()
        elif msg.topic.endswith("/cmd/target_soc"):
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

    def apply_vehicle_overrides(self, veh) -> None:
        """Überträgt die per MQTT gesetzten Overrides (oder die Konfigurations-
        Standardwerte) auf die Fahrzeug-Konfiguration des nächsten Laufs."""
        veh.departure_time = self.departure_override or self._veh_defaults[0]
        veh.target_soc_percent = (self.target_soc_override
                                  if self.target_soc_override is not None
                                  else self._veh_defaults[1])

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
                "time": pd.Timestamp.now().astimezone().isoformat(),
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

    def publish(self, table: pd.DataFrame, current_ts: pd.Timestamp) -> None:
        """Publiziert Sollwerte des aktuellen Slots und optional die Tabelle."""
        if not self.cfg.enabled:
            log.info("MQTT deaktiviert – überspringe Publish.")
            return

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

            # Wirksame Fahrzeug-Parameter zurückmelden (Rückmeldung für Homey,
            # z.B. nach ems/cmd/departure_time bzw. /target_soc).
            self._pub(f"{base}/vehicle/departure_time",
                      self.vehicle.departure_time.strftime("%H:%M"),
                      retain=self.cfg.retain)
            self._pub(f"{base}/vehicle/target_soc_percent",
                      float(self.vehicle.target_soc_percent),
                      retain=self.cfg.retain)

        if self.cfg.publish_schedule_json:
            payload = table.copy()

            def _conv(v):
                return v if isinstance(v, str) else float(v)

            payload_json = {
                "generated": pd.Timestamp.now(tz=idx.tz).isoformat(),
                "slots": [
                    {"time": ts.isoformat(), **{k: _conv(v) for k, v in r.items()}}
                    for ts, r in payload.iterrows()
                ],
            }
            self._pub(f"{base}/schedule", json.dumps(payload_json),
                      retain=self.cfg.retain)
            log.info("MQTT Zeitplan (%d Slots) publiziert.", len(payload))
