"""Optionale direkte RSCP-Anbindung des E3DC-Hauskraftwerks.

RSCP ist das (AES-verschlüsselte) Binärprotokoll des E3DC. Statt es selbst zu
implementieren, kapselt dieses Modul die etablierte Bibliothek ``pye3dc``
(``pip install pye3dc``) hinter einer schlanken, für dieses EMS passenden
Schnittstelle:

  * read_live()             – aktuelle Werte (SoC, PV, Last, Netz, Akku)
  * read_house_load_15min() – 15-min-Hauslast (Energiebilanz) für die Historie
  * apply_control()         – Steuerung: Netzladen/-entladen (Mode 3/4, mit
                              10-s-Watchdog) bzw. Lade-/Entlade-Limits

ALLES OPTIONAL und per config.e3dc_rscp abschaltbar (Default aus). Ist die
Bibliothek nicht installiert oder die Verbindung nicht möglich, wird das ruhig
geloggt und der bisherige Weg (InfluxDB lesen, MQTT/Homey steuern) läuft weiter.

Verifiziert gegen echte Hardware (pye3dc 0.10):
  * poll(): production.solar(+add)=PV, consumption.house=Last,
    consumption.battery=Akku (+Laden), production.grid=Netz (+Bezug).
  * EMS_REQ_SET_POWER-Modi (Referenz RSCP-MQTT-Bridge, an Hardware verifiziert):
    0=auto, 1=idle, 2=discharge, 3=charge(PV), 4=grid_charge. Wert = Leistung
    in W (Gesamt; PV zuerst, Netz für den Rest). Watchdog: ~alle 5 s neu senden,
    sonst fällt der E3DC nach 10 s auf auto zurück. Anlaufzeit bis ~30 s.
    Verifiziert: Mode 4 Netzladen @8 kW (Netzbezug), Mode 2 Entladen @3 kW
    (Akku -3 kW, Export).
  * apply_control(): Netzladen -> Mode 4, Netz-Entladen -> Mode 2 (nur bei
    allow_grid_discharge). Reine Lade-/Entlade-BEGRENZUNG über set_power_limits
    (persistent, kein Watchdog) – NICHT über die Modi.
  * Steuerung NUR mit control_enabled: true – sie greift real in den Speicher ein.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

import pandas as pd

from .config import Config

log = logging.getLogger("ems.rscp")

WATCHDOG_RESEND_S = 5.0   # < 10 s E3DC-Watchdog


class E3DCLink:
    """Dünne, defensive Kapsel um pye3dc (lokale RSCP-Verbindung)."""

    def __init__(self, config: Config):
        self.cfg = config
        self.rc = config.e3dc_rscp
        self._e3dc = None
        self._live_cache: Optional[dict] = None
        # Alle RSCP-I/O über eine Verbindung -> mit Lock serialisieren
        # (Watchdog-Thread und Hauptthread greifen sonst gleichzeitig zu).
        self._lock = threading.Lock()
        # Watchdog-Steuerung (Mode 3/4 alle 5 s neu senden)
        self._wd_thread: Optional[threading.Thread] = None
        self._wd_stop = threading.Event()
        self._wd_mode = 0
        self._wd_value = 0

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
        # Watchdog stoppen und – falls Steuerung aktiv war – auf auto zurück,
        # damit der Akku nach dem Beenden nicht in einem Manuell-Modus hängt.
        self._wd_stop.set()
        if self._wd_thread is not None:
            self._wd_thread.join(timeout=2)
            self._wd_thread = None
        try:
            if self._e3dc is not None and self.rc.control_enabled and self._wd_mode != 0:
                self._set_power(0, 0)
        except Exception:  # pragma: no cover
            pass
        self._wd_mode = 0
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
            with self._lock:
                e = self._connect()
                poll = e.poll()
            self._live_cache = self._map_live(poll)
            return self._live_cache
        except Exception as exc:
            log.warning("RSCP read_live fehlgeschlagen (%s).", exc)
            return None

    def read_system_limits(self) -> dict:
        """Statische Anlagengrenzen vom E3DC (einmalig beim Start) – nur die
        verlässlichen W-Werte aus get_system_info/get_power_settings. Die
        Kapazitätsfelder aus get_battery_data sind mehrdeutig (Ah/kWh) und
        werden bewusst NICHT verwendet. Rückgabe: {config-Feld: W}."""
        with self._lock:
            e = self._connect()
            info = e.get_system_info() or {}
            ps = e.get_power_settings() or {}
        out: dict = {}
        if info.get("maxAcPower"):
            out["inverter_max_ac_power_w"] = float(info["maxAcPower"])
        if info.get("maxBatChargePower"):
            out["max_charge_w"] = float(info["maxBatChargePower"])
        if info.get("maxBatDischargePower"):
            out["max_discharge_w"] = float(info["maxBatDischargePower"])
        if ps.get("dischargeStartPower") is not None:
            out["min_discharge_w"] = float(ps["dischargeStartPower"])
        return out

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
        t = pd.Timestamp(start).floor("15min")
        end = pd.Timestamp(end).floor("15min")
        while t < end:
            try:
                with self._lock:
                    e = self._connect()
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

    # ---- Steuerung ---------------------------------------------------- #
    def _set_power(self, mode: int, value: int) -> None:
        """Roh-Befehl EMS_REQ_SET_POWER (verifiziert: 0=auto, 1=idle, 2=Entladen,
        3=Laden (PV), 4=Netzladen; Wert = Gesamt-Leistung, PV zuerst, Netz für den Rest)."""
        from e3dc._rscpTags import RscpTag, RscpType
        with self._lock:
            e = self._connect()
            e.sendRequest((RscpTag.EMS_REQ_SET_POWER, RscpType.Container, [
                (RscpTag.EMS_REQ_SET_POWER_MODE, RscpType.UChar8, int(mode)),
                (RscpTag.EMS_REQ_SET_POWER_VALUE, RscpType.Int32, int(value))]),
                keepAlive=True)

    def _set_limits(self, enable: bool, max_charge=None, max_discharge=None) -> None:
        with self._lock:
            e = self._connect()
            e.set_power_limits(enable=bool(enable),
                               max_charge=(int(max_charge) if enable else None),
                               max_discharge=(int(max_discharge) if enable else None),
                               keepAlive=True)

    def _watchdog_loop(self) -> None:
        """Sendet aktiven Modus alle ~5 s neu (E3DC-Watchdog 10 s). Bei auto still."""
        while not self._wd_stop.wait(WATCHDOG_RESEND_S):
            mode, value = self._wd_mode, self._wd_value
            if mode in (2, 3, 4):
                try:
                    self._set_power(mode, value)
                except Exception as exc:  # pragma: no cover
                    log.warning("RSCP-Watchdog-Sendung fehlgeschlagen (%s).", exc)

    def _ensure_watchdog(self) -> None:
        if self._wd_thread is None or not self._wd_thread.is_alive():
            self._wd_stop.clear()
            self._wd_thread = threading.Thread(target=self._watchdog_loop,
                                               name="rscp-watchdog", daemon=True)
            self._wd_thread.start()

    def apply_control(self, row) -> None:
        """Plan-Slot -> E3DC-Steuerung. Nur mit control_enabled=true.
        Aktives Netzladen/-entladen über Mode 3/4 (mit 10-s-Watchdog);
        reine Lade-/Entlade-BEGRENZUNG über persistente Limits (kein Watchdog).
        GREIFT REAL IN DEN SPEICHER EIN."""
        if not self.rc.control_enabled:
            return
        hb = self.cfg.house_battery
        gc = float(row.get("batt_grid_charge_w", 0.0))
        gd = float(row.get("batt_grid_discharge_w", 0.0))
        try:
            if gc > 5.0:
                # Netzladen: Mode 4 (grid_charge), Wert = Gesamt-Ladeleistung
                # (PV zuerst, Netz für den Rest). Verifiziert @8 kW.
                total = round(float(row.get("batt_dc_charge_w", 0.0)) + gc)
                self._set_limits(False)          # Limits aus, Mode regelt
                self._wd_mode, self._wd_value = 4, total
                self._ensure_watchdog()
                self._set_power(4, total)
                log.info("RSCP: Netzladen aktiv, Mode 4, %d W (Watchdog).", total)
            elif gd > 5.0 and self.cfg.optimization.allow_grid_discharge:
                # Netz-Entladen: Mode 2 (discharge), Wert = Entladeleistung.
                # Verifiziert @3 kW (Akku -3042 W, Export).
                val = round(float(row.get("batt_discharge_w", 0.0)))
                self._set_limits(False)
                self._wd_mode, self._wd_value = 2, val
                self._ensure_watchdog()
                self._set_power(2, val)
                log.info("RSCP: Netz-Entladen aktiv, Mode 2, %d W (Watchdog).", val)
            else:
                # auto + persistente Lade-/Entlade-Limits gemäß Plan
                if self._wd_mode != 0:
                    self._wd_mode, self._wd_value = 0, 0
                    self._set_power(0, 0)        # aktiv auf auto zurück
                cl = float(row.get("batt_charge_limit_w", hb.max_dc_charge_w))
                dl = float(row.get("batt_discharge_limit_w", hb.max_discharge_w))
                limited = cl < hb.max_dc_charge_w - 1 or dl < hb.max_discharge_w - 1
                self._set_limits(limited, cl, dl)
                log.debug("RSCP: auto, Limit aktiv=%s (Laden≤%.0f, Entladen≤%.0f).",
                          limited, cl, dl)
        except Exception as exc:
            log.warning("RSCP-Steuerung fehlgeschlagen (%s).", exc)
