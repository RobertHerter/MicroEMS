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
  ems/setpoint/batt_dc_charge_w
  ems/setpoint/batt_ac_charge_w
  ems/setpoint/batt_discharge_w
  ems/setpoint/car_charge_w
  ems/setpoint/mode                 -> "charge" | "discharge" | "idle"
  ems/setpoint/updated              -> ISO-Zeitstempel des Slots
  ems/schedule                      -> komplette Tabelle als JSON
"""
from __future__ import annotations

import json
import logging
from typing import Dict

import pandas as pd

from .config import Config

log = logging.getLogger("ems.mqtt")


class HomeyMqttPublisher:
    def __init__(self, config: Config):
        self.cfg = config.mqtt
        self._client = None

    def _connect(self):
        import socket
        import paho.mqtt.client as mqtt

        # Schnelle Erreichbarkeitsprüfung -> klarer, sofortiger Fehler statt
        # langem Blockieren, falls der Broker nicht erreichbar ist.
        socket.create_connection((self.cfg.host, self.cfg.port), timeout=5).close()

        # paho-mqtt >= 2.0 verlangt die Angabe der Callback-API-Version;
        # ältere Versionen kennen den Parameter nicht.
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        except (AttributeError, TypeError):
            client = mqtt.Client()
        if self.cfg.username:
            client.username_pw_set(self.cfg.username, self.cfg.password)
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
            # Fail-safe: Sollwerte NIE retainen (s. Modul-Docstring).
            for key, value in setpoints.items():
                self._pub(f"{base}/setpoint/{key}", value, retain=False)
            log.info("MQTT Steuerbefehle publiziert (Slot %s): %s", idx[pos], setpoints)

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
