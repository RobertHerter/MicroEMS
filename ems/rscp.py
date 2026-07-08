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
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

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

    def read_history_15min(self, days: int) -> List[dict]:
        """15-min-Bilanzen (Energie Wh je Fenster) über die letzten `days` Tage
        via get_db_data_timestamp (900-s-Fenster). ACHTUNG: 1 RSCP-Aufruf je
        Fenster -> 96/Tag (ein Jahr ≈ 35 000). Für Backfill `days` bewusst
        begrenzen; der Timer holt nur die letzten Tage nach."""
        out: List[dict] = []
        try:
            e = self._connect()
        except Exception as exc:
            log.warning("RSCP-Verbindung für 15-min-Historie fehlgeschlagen (%s).", exc)
            return out
        now = datetime.now().replace(second=0, microsecond=0)
        now = now.replace(minute=(now.minute // 15) * 15)
        t = now - timedelta(days=days)
        while t < now:
            ts = int(t.timestamp())
            try:
                d = e.get_db_data_timestamp(startTimestamp=ts, timespanSeconds=900,
                                            keepAlive=True)
            except Exception as exc:  # pragma: no cover
                log.debug("RSCP 15-min %s nicht lesbar (%s).", t, exc)
                t += timedelta(minutes=15)
                continue
            if d:
                row = {"ts": t.isoformat()}
                row.update({k: v for k, v in d.items()
                            if isinstance(v, (int, float))})
                out.append(row)
            t += timedelta(minutes=15)
        return out

    def read_history_daily(self, days: int) -> List[dict]:
        """Historische Tagesbilanzen (Energie) über die letzten `days` Tage.
        RSCP liefert Tages-/Monatsaggregate – kein 15-min-Raster."""
        out: List[dict] = []
        try:
            e = self._connect()
        except Exception as exc:
            log.warning("RSCP-Verbindung für Historie fehlgeschlagen (%s).", exc)
            return out
        today = date.today()
        for d in range(days, 0, -1):
            day = today - timedelta(days=d)
            try:
                data = e.get_db_data(startDate=day, timespan="DAY")
            except Exception as exc:  # pragma: no cover
                log.debug("RSCP-Historie %s nicht lesbar (%s).", day, exc)
                continue
            row = {"date": day.isoformat()}
            row.update({k: v for k, v in (data or {}).items()
                        if isinstance(v, (int, float))})
            out.append(row)
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
