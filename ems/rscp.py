"""Optionale direkte RSCP-Anbindung des E3DC-Hauskraftwerks.

RSCP ist das (AES-verschlüsselte) Binärprotokoll des E3DC. Statt es selbst zu
implementieren, kapselt dieses Modul die etablierte Bibliothek ``pye3dc``
(``pip install pye3dc``) hinter einer schlanken, für dieses EMS passenden
Schnittstelle:

  * read_live()          – aktuelle Werte (SoC, PV, Last, Netz, Akku-Leistung)
  * read_history_daily() – historische Tagesbilanzen (Energie)
  * apply_setpoints()    – Steuerung: Lade-/Entladeleistung begrenzen

ALLES OPTIONAL und per config.e3dc_rscp abschaltbar (Default aus). Ist die
Bibliothek nicht installiert oder die Verbindung nicht möglich, wird das ruhig
geloggt und der bisherige Weg (InfluxDB lesen, MQTT/Homey steuern) läuft weiter.

WICHTIG (nicht gegen echte Hardware getestet):
  * Die Feldnamen aus pye3dc.poll() können je Version/Modell abweichen – das
    Mapping ist in _map_live() zentralisiert und leicht anzupassen.
  * Vorzeichen von Netz/Akku ggf. über e3dc_rscp.grid_sign / batt_sign drehen.
  * Steuerung (apply_setpoints) NUR mit control_enabled: true und nach Prüfung
    auf dem Gerät aktivieren – sie greift real in den Speicher ein.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

from .config import Config

log = logging.getLogger("ems.rscp")


class E3DCLink:
    """Dünne, defensive Kapsel um pye3dc (lokale RSCP-Verbindung)."""

    def __init__(self, config: Config):
        self.cfg = config
        self.rc = config.e3dc_rscp
        self._e3dc = None
        self._live_cache: Optional[dict] = None

    # ------------------------------------------------------------------ #
    def _connect(self):
        if self._e3dc is not None:
            return self._e3dc
        from e3dc import E3DC  # lazy: nur wenn wirklich genutzt
        cfg_extra = {}
        # pye3dc erwartet für CONNECT_LOCAL: username/password (Portal-Login),
        # ipAddress und key (RSCP-Passwort, im E3DC unter Einstellungen gesetzt).
        self._e3dc = E3DC(
            E3DC.CONNECT_LOCAL,
            username=self.rc.username,
            password=self.rc.password,
            ipAddress=self.rc.host,
            key=self.rc.key,
            configuration=cfg_extra,
        )
        log.info("RSCP-Verbindung zum E3DC (%s) aufgebaut.", self.rc.host)
        return self._e3dc

    def close(self) -> None:
        try:
            if self._e3dc is not None and hasattr(self._e3dc, "disconnect"):
                self._e3dc.disconnect()
        except Exception:  # pragma: no cover
            pass
        self._e3dc = None

    # ------------------------------------------------------------------ #
    def _map_live(self, poll: dict) -> dict:
        """pye3dc.poll() -> interne Einheiten (W, %).
        Gegen echte Hardware (pye3dc 0.10) verifiziert:
          production.solar (+ .add) = PV, consumption.house = Hauslast,
          consumption.battery = Akku (+ = Laden, - = Entladen),
          production.grid = Netz (+ = Bezug, - = Einspeisung).
        Zentralisiert, damit Abweichungen je Modell/Version hier korrigierbar."""
        prod = poll.get("production", {}) or {}
        cons = poll.get("consumption", {}) or {}
        gs = float(self.rc.grid_sign)
        bs = float(self.rc.batt_sign)
        grid = prod.get("grid")
        batt = cons.get("battery")
        pv = (prod.get("solar") or 0.0) + (prod.get("add") or 0.0)
        return {
            "soc_percent": poll.get("stateOfCharge"),
            "pv_w": pv,
            "house_load_w": cons.get("house"),
            "grid_w": (gs * grid if grid is not None else None),
            "battery_w": (bs * batt if batt is not None else None),
            "wallbox_w": cons.get("wallbox"),
        }

    def read_live(self, force: bool = False) -> Optional[dict]:
        """Aktuelle Werte (gecacht je Zyklus). None bei Fehler."""
        if self._live_cache is not None and not force:
            return self._live_cache
        try:
            e = self._connect()
            self._live_cache = self._map_live(e.poll())
            return self._live_cache
        except Exception as exc:
            log.warning("RSCP read_live fehlgeschlagen (%s).", exc)
            return None

    @staticmethod
    def _house_load_w(x: dict) -> float:
        """Hauslast (W) aus den Energie-Aggregaten eines 15-min-Fensters.
        Energiebilanz (Wh/15min -> W, *4):
          Last = PV + Akku-Entladung + Netzbezug - Akku-Ladung - Einspeisung.
        (Gegen InfluxDB verifiziert: Bias ~-44 W, Fenster-MAE ~520 W - mittelt
        sich in der Ähnliche-Tage-Prognose heraus.)"""
        wh = (x.get("solarProduction", 0.0) + x.get("bat_power_out", 0.0)
              + x.get("grid_power_out", 0.0) - x.get("bat_power_in", 0.0)
              - x.get("grid_power_in", 0.0))
        return max(0.0, wh * 4.0)

    def read_house_load_15min(self, start, end) -> Dict[str, float]:
        """15-min-Hauslast (W) je Fenster in [start, end) als {UTC-ISO -> W}.
        Ein RSCP-Aufruf je 15-min-Fenster -> Zeitraum bewusst begrenzen
        (Backfill: eigenes Skript; zyklisch: nur wenige Fenster)."""
        out: Dict[str, float] = {}
        try:
            e = self._connect()
        except Exception as exc:
            log.warning("RSCP-Verbindung für Hauslast-Historie fehlgeschlagen (%s).", exc)
            return out
        t = pd.Timestamp(start).floor("15min")
        end = pd.Timestamp(end).floor("15min")
        while t < end:
            try:
                d = e.get_db_data_timestamp(
                    startTimestamp=int(t.timestamp()), timespanSeconds=900,
                    keepAlive=True)
            except Exception as exc:  # pragma: no cover
                log.debug("RSCP Hauslast %s nicht lesbar (%s).", t, exc)
                t += pd.Timedelta(minutes=15)
                continue
            if d:
                out[t.tz_convert("UTC").isoformat()] = self._house_load_w(d)
            t += pd.Timedelta(minutes=15)
        return out

    def apply_setpoints(self, row) -> bool:
        """Steuerung per RSCP: Lade-/Entladeleistung begrenzen.

        Nur mit control_enabled=true. Greift REAL in den Speicher ein. Bildet
        die EMS-Befehle auf pye3dc.set_power_limits ab:
          charge_limit  -> maxChargePower
          discharge_limit-> maxDischargePower
        Netzladen (grid_charge_w > 0) über set_power_limits nicht direkt
        möglich -> Hinweis; wer es braucht, ergänzt set_manual_charge.
        """
        if not self.rc.control_enabled:
            return False
        try:
            e = self._connect()
            cl = float(row.get("batt_charge_limit_w", 0.0))
            dl = float(row.get("batt_discharge_limit_w", 0.0))
            hb = self.cfg.house_battery
            # "frei laufen" (Limit = Hardware-Max) -> Begrenzung deaktivieren.
            limit_active = (cl < hb.max_dc_charge_w - 1 or dl < hb.max_discharge_w - 1)
            e.set_power_limits(
                enable=bool(limit_active),
                max_charge=int(cl) if limit_active else None,
                max_discharge=int(dl) if limit_active else None,
            )
            gc = float(row.get("batt_grid_charge_w", 0.0))
            if gc > 1.0:
                log.info("RSCP: Netzladen %.0f W angefordert – set_power_limits "
                         "deckt das nicht ab (ggf. set_manual_charge ergänzen).", gc)
            log.info("RSCP-Steuerung gesetzt: Limit aktiv=%s, Laden≤%.0f, "
                     "Entladen≤%.0f W.", limit_active, cl, dl)
            return True
        except Exception as exc:
            log.warning("RSCP-Steuerung fehlgeschlagen (%s).", exc)
            return False
