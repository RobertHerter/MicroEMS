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
        # Persistente Lade-/Entlade-Begrenzung aktiv? (EMS_POWER_LIMITS_USED).
        # Anders als Mode 3/4 hat sie KEINEN E3DC-Watchdog -> beim Beenden/Absturz
        # ausdrücklich freigeben, sonst bleibt der Akku unbegrenzt gedrosselt.
        self._limits_active = False
        # Auto-Rücksetz-Timer für manuelles Laden/Entladen (Dashboard).
        self._manual_timer: Optional[threading.Timer] = None
        # Handbetrieb hat bis zum Ablauf Vorrang vor dem normalen Optimierer-
        # Sollwert. RLock schützt Timer-, Scheduler- und Hauptthread gegeneinander.
        self._manual_lock = threading.RLock()
        self._manual_active = False
        self._manual_action: Optional[str] = None
        self._manual_until: Optional[float] = None
        self._last_control_status: Optional[dict] = None
        self._alarm_callback = None
        self._last_watchdog_alarm = 0.0
        self._watchdog_failed = False
        self._curtailment_active = False
        self._curtailment_baseline_percent: Optional[float] = None

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
        if self._manual_timer is not None:
            self._manual_timer.cancel()
            self._manual_timer = None
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
        # Persistente Lade-/Entlade-Limits ausdrücklich freigeben – sie haben
        # keinen E3DC-Watchdog und blieben sonst nach dem Beenden unbegrenzt aktiv
        # (EMS_POWER_LIMITS_USED = false).
        try:
            if self._e3dc is not None and self.rc.control_enabled and self._limits_active:
                self._set_limits(False)
                log.info("RSCP: Lade-/Entlade-Limits beim Beenden freigegeben "
                         "(EMS_POWER_LIMITS_USED=false).")
        except Exception:  # pragma: no cover
            pass
        # PV-Derating ist persistent. Auch bei Fehlern in der restlichen
        # Steuerung beim sauberen Beenden immer auf den gemerkten Normalwert.
        try:
            if (self._e3dc is not None and self._curtailment_active
                    and self.rc.curtailment_control_enabled):
                self._set_derate_percent(self._normal_derate_percent())
                self._curtailment_active = False
                log.info("RSCP: PV-Abregelung beim Beenden aufgehoben.")
        except Exception as exc:  # pragma: no cover
            log.warning("RSCP: PV-Abregelung beim Beenden nicht aufhebbar (%s).", exc)
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
            cap = self._specified_capacity_wh(e)
        out: dict = {}
        if cap:
            out["capacity_wh"] = cap
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
    def _specified_capacity_wh(e):
        """Nominale Akkukapazität (Wh) aus BAT_SPECIFICATION -> BAT_SPECIFIED_CAPACITY.
        Verlässlich in Wh; die usable-Felder aus get_battery_data sind Ah-basiert
        und daher mehrdeutig. None bei Fehler."""
        try:
            from e3dc._rscpLib import rscpFindTag, rscpFindTagIndex
            from e3dc._rscpTags import RscpTag, RscpType
            req = e.sendRequest((RscpTag.BAT_REQ_DATA, RscpType.Container, [
                (RscpTag.BAT_INDEX, RscpType.Uint16, 0),
                (RscpTag.BAT_REQ_SPECIFICATION, RscpType.NoneType, None),
            ]), keepAlive=True)
            spec = rscpFindTag(req, "BAT_SPECIFICATION")
            cap = float(rscpFindTagIndex(spec, "BAT_SPECIFIED_CAPACITY"))
            return cap if cap > 0 else None
        except Exception:
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
        tz = self.cfg.general.timezone
        t = pd.Timestamp(start).floor("15min")
        end = pd.Timestamp(end).floor("15min")
        # In Ortszeit iterieren (tz-aware sicherstellen), damit die DST-korrekte
        # Verschiebung je Slot bestimmt werden kann.
        t = (t.tz_localize(tz) if t.tz is None else t.tz_convert(tz))
        end = (end.tz_localize(tz) if end.tz is None else end.tz_convert(tz))
        while t < end:
            # Die E3DC-Historien-DB (get_db_data_timestamp) liegt in ORTSZEIT: sie
            # interpretiert den übergebenen Epoch als lokale Wandzeit. Ein echter
            # UTC-Epoch liefert das Fenster daher um den Zeitzonen-Offset verschoben
            # (im Sommer +2 h) -> Verbrauchsspitzen landen 2 h zu spät. Korrektur:
            # den Offset aufaddieren (DST-sicher via utcoffset). Der Speicher-
            # Schlüssel bleibt die ECHTE UTC-Zeit des Slots.
            local_epoch = int(t.timestamp() + t.utcoffset().total_seconds())
            try:
                with self._lock:
                    e = self._connect()
                    d = e.get_db_data_timestamp(
                        startTimestamp=local_epoch, timespanSeconds=900,
                        keepAlive=True)
            except Exception as exc:  # pragma: no cover
                log.debug("RSCP Hauslast %s nicht lesbar (%s).", t, exc)
                t += pd.Timedelta(minutes=15)
                continue
            if d:
                w = self._house_load_w(d)
                # Unfertige E3DC-Aggregate können eine negative Bilanz liefern,
                # die _house_load_w defensiv zu 0 begrenzt. Ein echter Haushalt
                # hat hier keine exakt 0 W; nicht persistieren, sondern beim
                # überlappenden Folgelauf mit dem finalen Wert nachholen.
                if w > 0.0:
                    out[t.tz_convert("UTC").isoformat()] = w
                else:
                    log.debug("RSCP Hauslast %s noch unplausibel (%.0f W) – "
                              "wird später erneut gelesen.", t, w)
            t += pd.Timedelta(minutes=15)
        return out

    def read_energy_15min(self, start, end) -> Dict[str, dict]:
        """Gemessene 15-min-Energieaggregate je Fenster in [start, end) als
        {UTC-ISO -> {pv_wh, load_wh, bat_in_wh, bat_out_wh, grid_import_wh,
        grid_export_wh}}. Grundlage der Ersparnis-Gegenprüfung gegen die echten
        Zählerwerte (savings_check.py). Ein RSCP-Aufruf je Fenster -> Zeitraum
        begrenzen. Vorzeichen wie in _house_load_w: grid_power_out=Netzbezug,
        grid_power_in=Einspeisung, bat_power_in=Laden, bat_power_out=Entladen."""
        out: Dict[str, dict] = {}
        tz = self.cfg.general.timezone
        t = pd.Timestamp(start).floor("15min")
        end = pd.Timestamp(end).floor("15min")
        t = (t.tz_localize(tz) if t.tz is None else t.tz_convert(tz))
        end = (end.tz_localize(tz) if end.tz is None else end.tz_convert(tz))
        while t < end:
            local_epoch = int(t.timestamp() + t.utcoffset().total_seconds())
            try:
                with self._lock:
                    e = self._connect()
                    d = e.get_db_data_timestamp(
                        startTimestamp=local_epoch, timespanSeconds=900,
                        keepAlive=True)
            except Exception as exc:  # pragma: no cover
                log.debug("RSCP Energie %s nicht lesbar (%s).", t, exc)
                t += pd.Timedelta(minutes=15)
                continue
            if d:
                pv = float(d.get("solarProduction", 0.0))
                bat_in = float(d.get("bat_power_in", 0.0))
                bat_out = float(d.get("bat_power_out", 0.0))
                grid_imp = float(d.get("grid_power_out", 0.0))
                grid_exp = float(d.get("grid_power_in", 0.0))
                load = pv + bat_out + grid_imp - bat_in - grid_exp
                # Unfertige Aggregate liefern eine unplausible (negative) Bilanz;
                # überspringen und beim überlappenden Folgelauf nachholen.
                if load > 0.0:
                    out[t.tz_convert("UTC").isoformat()] = {
                        "pv_wh": pv, "load_wh": load, "bat_in_wh": bat_in,
                        "bat_out_wh": bat_out, "grid_import_wh": grid_imp,
                        "grid_export_wh": grid_exp}
            t += pd.Timedelta(minutes=15)
        return out

    def read_energy_total(self, start, end) -> Optional[dict]:
        """Summierte E3/DC-Zaehlerenergie fuer einen Zeitraum in Wh.

        Anders als ``read_energy_15min`` ist dafuer nur ein RSCP-Aufruf noetig;
        ideal fuer die laufend aktualisierten Tageswerte im Dashboard.
        """
        tz = self.cfg.general.timezone
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        start = (start.tz_localize(tz) if start.tz is None else start.tz_convert(tz))
        end = (end.tz_localize(tz) if end.tz is None else end.tz_convert(tz))
        span = max(1, int((end - start).total_seconds()))
        local_epoch = int(start.timestamp() + start.utcoffset().total_seconds())
        try:
            with self._lock:
                e = self._connect()
                d = e.get_db_data_timestamp(
                    startTimestamp=local_epoch, timespanSeconds=span,
                    keepAlive=True)
        except Exception as exc:  # pragma: no cover
            log.debug("RSCP Tagesenergie nicht lesbar (%s).", exc)
            return None
        if not d:
            return None
        pv = float(d.get("solarProduction", 0.0) or 0.0)
        bat_in = float(d.get("bat_power_in", 0.0) or 0.0)
        bat_out = float(d.get("bat_power_out", 0.0) or 0.0)
        grid_imp = float(d.get("grid_power_out", 0.0) or 0.0)
        grid_exp = float(d.get("grid_power_in", 0.0) or 0.0)
        balance_load = pv + bat_out + grid_imp - bat_in - grid_exp
        reported_load = float(d.get("consumption", 0.0) or 0.0)
        load = reported_load if reported_load > 0.0 else balance_load
        residual = (balance_load - reported_load
                    if reported_load > 0.0 else None)
        # DC-PV-/Batteriezähler und AC-Haus-/Netzzähler schließen wegen der
        # Wechselrichterverluste nicht exakt. Bis 5 % des Energiedurchsatzes
        # gelten daher als plausibel; größere Abweichungen weisen auf fehlende
        # oder inkonsistente Aggregate hin.
        scale = max(1000.0, abs(pv) + abs(grid_imp) + abs(bat_out))
        balance_ok = (abs(residual) <= max(250.0, 0.05 * scale)
                      if residual is not None else None)
        return {"pv_wh": pv, "load_wh": max(0.0, load),
                "bat_in_wh": bat_in, "bat_out_wh": bat_out,
                "grid_import_wh": grid_imp, "grid_export_wh": grid_exp,
                "balance_load_wh": max(0.0, balance_load),
                "balance_residual_wh": residual, "balance_ok": balance_ok,
                "start": start.isoformat(), "end": end.isoformat()}

    def set_control_enabled(self, enabled: bool) -> dict:
        """Automatische RSCP-Steuerung sicher ein-/ausschalten.

        Beim Ausschalten werden laufende Watchdog-Modi und persistente
        SmartPower-Limits sofort freigegeben, bevor das Laufzeitflag faellt.
        """
        enabled = bool(enabled)
        if enabled:
            self.rc.control_enabled = True
            return {"enabled": True, "released": False,
                    "message": "Automatische E3/DC-Steuerung ist aktiviert."}
        released = False
        with self._manual_lock:
            if self._manual_timer is not None:
                self._manual_timer.cancel()
                self._manual_timer = None
            self._manual_active = False
            self._manual_action = None
            self._manual_until = None
            if self._wd_mode != 0:
                self._set_power(0, 0)
                released = True
            self._wd_mode, self._wd_value = 0, 0
            rc = self._set_limits(False)
            released = rc != -1
            verification = self._verify_limits(
                False, self.cfg.house_battery.max_dc_charge_w,
                self.cfg.house_battery.max_discharge_w, "disabled", force=True)
            self.rc.control_enabled = False
        confirmed = verification.get("ok") is True
        message = ("E3/DC-Steuerung deaktiviert; Limits bestätigt freigegeben."
                   if confirmed else
                   "E3/DC-Steuerung deaktiviert, aber Freigabe nicht bestätigt.")
        return {"enabled": False, "released": released,
                "verified": confirmed, "control_status": verification,
                "message": message}

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

    def _set_limits(self, enable: bool, max_charge=None, max_discharge=None) -> int:
        """Persistente SmartPower-Lade-/Entlade-BEGRENZUNG.

        enable=True setzt zugleich EMS_POWER_LIMITS_USED=true – ohne dieses Flag
        ignoriert der E3DC die gesetzten Max-Werte. enable=False gibt die Grenzen
        wieder frei (EMS_POWER_LIMITS_USED=false). Es gibt KEINEN Watchdog wie bei
        Mode 3/4: die Grenze bleibt bis zum ausdrücklichen Zurücksetzen aktiv
        (deshalb Freigabe in close()). Rückgabe von pye3dc: 0=ok, 1=Wert
        angepasst (nicht-optimal), -1=Fehler."""
        previous_active = self._limits_active
        with self._lock:
            e = self._connect()
            rc = e.set_power_limits(
                enable=bool(enable),
                max_charge=(int(max_charge) if enable else None),
                max_discharge=(int(max_discharge) if enable else None),
                keepAlive=True)
        rc = 0 if rc is None else int(rc)
        self._limits_active = (bool(enable) if rc != -1 else previous_active)
        if enable and rc == -1:
            log.warning("RSCP: Lade-/Entlade-Limit vom E3DC NICHT übernommen "
                        "(EMS_POWER_LIMITS_USED) – Begrenzung greift evtl. nicht.")
        return rc

    def read_control_limits(self) -> dict:
        """Die aktuell am E3DC wirksamen SmartPower-Limits zurücklesen."""
        with self._lock:
            e = self._connect()
            settings = e.get_power_settings(keepAlive=True) or {}
        return {
            "power_limits_used": bool(settings.get("powerLimitsUsed")),
            "max_charge_w": (None if settings.get("maxChargePower") is None
                             else float(settings["maxChargePower"])),
            "max_discharge_w": (None if settings.get("maxDischargePower") is None
                                else float(settings["maxDischargePower"])),
        }

    def read_curtailment(self) -> dict:
        """Aktuelle PV-Abregelgrenze direkt vom E3DC zurücklesen."""
        from e3dc._rscpTags import RscpTag
        with self._lock:
            e = self._connect()
            percent = float(e.sendRequestTag(
                RscpTag.EMS_REQ_DERATE_AT_PERCENT_VALUE, keepAlive=True))
            power = float(e.sendRequestTag(
                RscpTag.EMS_REQ_DERATE_AT_POWER_VALUE, keepAlive=True))
            peak = float(e.sendRequestTag(
                RscpTag.EMS_REQ_INSTALLED_PEAK_POWER, keepAlive=True))
            active = bool(e.sendRequestTag(
                RscpTag.EMS_REQ_IS_PV_DERATING, keepAlive=True))
        return {"percent": percent * 100.0, "power_w": power,
                "installed_peak_w": peak, "active": active}

    def _normal_derate_percent(self) -> float:
        value = (self._curtailment_baseline_percent
                 if self._curtailment_baseline_percent is not None
                 else float(self.rc.curtailment_normal_percent))
        return max(0.0, min(100.0, value))

    def _set_derate_percent(self, percent: float) -> None:
        """Persistente E3DC-PV-Grenze schreiben (Protokollwert 0..1)."""
        from e3dc._rscpTags import RscpTag, RscpType
        value = max(0.0, min(100.0, float(percent))) / 100.0
        with self._lock:
            e = self._connect()
            e.sendRequest((RscpTag.EMS_REQ_SET_DERATE_PERCENT,
                           RscpType.Float32, value), keepAlive=True)

    def apply_pv_curtailment(self, row) -> dict:
        """Plan-Abregelung real setzen, zurücklesen und außerhalb des Slots lösen."""
        requested_w = max(0.0, float(row.get("pv_curtail_w", 0.0)))
        if not self.rc.curtailment_control_enabled:
            if requested_w > 5.0:
                return self._control_status(
                    False, "curtailment_unavailable", "pv_curtailment",
                    "PV-Abregelung geplant, aber E3DC-Abregelsteuerung ist deaktiviert.",
                    expected={"curtail_w": round(requested_w)})
            return self._control_status(
                None, "not_required", "pv_curtailment",
                "Keine aktive PV-Abregelung erforderlich.")
        if requested_w <= 5.0 and not self._curtailment_active:
            return self._control_status(
                None, "not_required", "pv_curtailment",
                "Keine aktive PV-Abregelung erforderlich.")
        try:
            before = self.read_curtailment()
            if self._curtailment_baseline_percent is None:
                # Nur einen plausiblen ungedrosselten Zustand als Baseline
                # merken; eine bereits extern gesetzte Grenze nicht überschreiben.
                configured = float(self.rc.curtailment_normal_percent)
                self._curtailment_baseline_percent = (
                    before["percent"] if not before["active"] else configured)
            normal = self._normal_derate_percent()
            if requested_w <= 5.0:
                target = normal
            else:
                peak_w = before.get("installed_peak_w") or 0.0
                if peak_w <= 0.0:
                    raise RuntimeError("installierte PV-Spitzenleistung nicht lesbar")
                available_w = max(0.0, float(row.get("pv_w", 0.0)) - requested_w)
                target = min(normal, 100.0 * available_w / peak_w)
            self._set_derate_percent(target)
            after = self.read_curtailment()
            tolerance = max(
                0.0, float(self.rc.curtailment_verify_tolerance_percent))
            if abs(after["percent"] - target) > tolerance:
                return self._control_status(
                    False, "curtailment_mismatch", "pv_curtailment",
                    f"PV-Grenze Soll {target:.1f} %, Ist {after['percent']:.1f} %.",
                    expected={"percent": round(target, 2)}, actual=after)
            self._curtailment_active = target < normal - tolerance
            return self._control_status(
                True, "curtailment_confirmed", "pv_curtailment",
                f"E3DC bestätigt PV-Grenze {after['percent']:.1f} %.",
                expected={"percent": round(target, 2)}, actual=after)
        except Exception as exc:
            self._curtailment_active = False
            return self._control_status(
                False, "curtailment_write_failed", "pv_curtailment",
                f"E3DC-PV-Abregelung fehlgeschlagen: {exc}",
                expected={"curtail_w": round(requested_w)})

    def _control_status(self, ok, state: str, mode: str, message: str,
                        expected=None, actual=None) -> dict:
        status = {
            "ok": ok, "state": state, "mode": mode, "message": message,
            "expected": expected or {}, "actual": actual or {},
            "checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        self._last_control_status = status
        return status

    def _verify_limits(self, enabled: bool, max_charge: float,
                       max_discharge: float, mode: str,
                       force: bool = False) -> dict:
        expected = {
            "power_limits_used": bool(enabled),
            "max_charge_w": round(float(max_charge)),
            "max_discharge_w": round(float(max_discharge)),
        }
        if not self.rc.verify_control and not force:
            return self._control_status(
                None, "unverified", mode, "Befehl gesendet; Rücklesung deaktiviert.",
                expected=expected)
        tolerance = max(0.0, float(self.rc.control_verify_tolerance_w))
        actual, problems, last_exc = None, [], None
        # Direkt nach EMS_REQ_SET_POWER_SETTINGS liefert manche E3DC-Firmware
        # für wenige 100 ms noch powerLimitsUsed vom vorherigen Zustand. Bis zu
        # vier kurze Rücklesungen vermeiden diesen falschen Ausfallalarm, ohne
        # echte Schreib-/Übernahmefehler zu verdecken.
        for attempt in range(4):
            try:
                candidate = self.read_control_limits()
                candidate_problems = []
                if candidate["power_limits_used"] != bool(enabled):
                    candidate_problems.append(
                        f"Limit-Aktivierung Soll {enabled}, Ist "
                        f"{candidate['power_limits_used']}")
                if enabled:
                    for key, label in (("max_charge_w", "Laden"),
                                       ("max_discharge_w", "Entladen")):
                        value = candidate.get(key)
                        if value is None or abs(value - expected[key]) > tolerance:
                            candidate_problems.append(
                                f"{label} Soll {expected[key]:.0f} W, Ist "
                                f"{'unbekannt' if value is None else f'{value:.0f} W'}")
                actual, problems, last_exc = candidate, candidate_problems, None
                if not problems:
                    if attempt:
                        log.info("RSCP-Rücklesung nach %d Wiederholung(en) bestätigt.",
                                 attempt)
                    break
            except Exception as exc:
                last_exc = exc
            if attempt < 3:
                time.sleep(0.25)
        if actual is None:
            return self._control_status(
                False, "readback_failed", mode,
                f"E3DC-Limits konnten nicht zurückgelesen werden: {last_exc}",
                expected=expected)
        if problems:
            return self._control_status(
                False, "mismatch", mode, "; ".join(problems),
                expected=expected, actual=actual)
        if enabled:
            message = (f"E3DC bestätigt Laden≤{actual['max_charge_w']:.0f} W "
                       f"und Entladen≤{actual['max_discharge_w']:.0f} W.")
        else:
            message = "E3DC bestätigt: SmartPower-Limits sind freigegeben."
        return self._control_status(
            True, "confirmed", mode, message, expected=expected, actual=actual)

    def last_control_status(self) -> Optional[dict]:
        """Letztes Ergebnis der Schreib-/Rücklesekontrolle."""
        return dict(self._last_control_status) if self._last_control_status else None

    def set_alarm_callback(self, callback) -> None:
        """Alarmziel für asynchrone Watchdog-Ausfälle (z.B. MQTT) setzen."""
        self._alarm_callback = callback

    def _watchdog_loop(self) -> None:
        """Sendet aktiven Modus alle ~5 s neu (E3DC-Watchdog 10 s). Bei auto still."""
        while not self._wd_stop.wait(WATCHDOG_RESEND_S):
            mode, value = self._wd_mode, self._wd_value
            if mode in (1, 2, 3, 4):
                try:
                    self._set_power(mode, value)
                    if self._watchdog_failed and self._alarm_callback:
                        self._alarm_callback(
                            "info", "E3DC-Steuer-Watchdog wiederhergestellt.")
                    self._watchdog_failed = False
                except Exception as exc:  # pragma: no cover
                    log.warning("RSCP-Watchdog-Sendung fehlgeschlagen (%s).", exc)
                    status = self._control_status(
                        False, "watchdog_failed", f"mode_{mode}",
                        f"E3DC-Steuer-Watchdog ausgefallen: {exc}",
                        expected={"mode": mode, "power_w": value})
                    self._watchdog_failed = True
                    now = time.time()
                    repeat_s = max(
                        60.0, float(self.rc.control_alarm_repeat_minutes) * 60.0)
                    if (self._alarm_callback
                            and now - self._last_watchdog_alarm >= repeat_s):
                        self._alarm_callback("error", status["message"])
                        self._last_watchdog_alarm = now

    def _ensure_watchdog(self) -> None:
        if self._wd_thread is None or not self._wd_thread.is_alive():
            self._wd_stop.clear()
            self._wd_thread = threading.Thread(target=self._watchdog_loop,
                                               name="rscp-watchdog", daemon=True)
            self._wd_thread.start()

    def manual_power(self, action: str, watts: float = 0.0,
                     seconds: float = 900.0) -> dict:
        """Manuelles Laden/Entladen vom Dashboard (expliziter Nutzer-Eingriff).

        action: "charge"/"grid_charge" (Mode 4, Netzladen), "pv_charge"
        (Mode 3, Laden), "discharge" (Mode 2), "idle" (Mode 1) oder
        "auto"/"stop" (Mode 0). Läuft über den 10-s-Watchdog und setzt sich nach
        `seconds` selbst auf auto zurück; stirbt der Prozess, fällt der E3DC nach
        10 s ohnehin auf auto (Fail-safe). GREIFT REAL IN DEN SPEICHER EIN – läuft
        bewusst auch ohne control_enabled (nur über das Dashboard-Opt-in erreichbar).
        Rückgabe: {mode, watts, seconds}."""
        with self._manual_lock:
            hb = self.cfg.house_battery
            mode = {"charge": 4, "grid_charge": 4, "pv_charge": 3,
                    "discharge": 2, "idle": 1,
                    "auto": 0, "stop": 0}.get(action)
            if mode is None:
                raise ValueError(
                    f"Unbekannte Aktion: {action!r} "
                    "(charge|grid_charge|pv_charge|discharge|idle|auto)")
            cap = hb.max_dc_charge_w if mode in (3, 4) else \
                (hb.max_discharge_w if mode == 2 else 0.0)
            val = int(max(0.0, min(float(watts or 0.0), float(cap))))
            if self._manual_timer is not None:
                self._manual_timer.cancel()
                self._manual_timer = None
            if mode == 0:
                self._manual_active = False
                self._manual_action = None
                self._manual_until = None
                self._wd_mode, self._wd_value = 0, 0
                try:
                    self._set_power(0, 0)
                except Exception as exc:  # pragma: no cover
                    log.warning("RSCP: Auto-Rücksetzen fehlgeschlagen (%s).", exc)
                log.info("RSCP: manueller Eingriff beendet -> auto.")
                return {"mode": 0, "watts": 0, "seconds": 0,
                        "active": False}
            self._set_limits(False)                   # etwaige Limits raus
            secs = max(0.0, float(seconds))
            self._manual_active = True
            self._manual_action = action
            self._manual_until = time.time() + secs if secs > 0 else None
            self._wd_mode, self._wd_value = mode, val
            self._ensure_watchdog()
            self._set_power(mode, val)
            if secs > 0:
                self._manual_timer = threading.Timer(secs, self._manual_revert)
                self._manual_timer.daemon = True
                self._manual_timer.start()
            log.info("RSCP: manuelles %s %d W für %.0f min (Mode %d).",
                     action, val, secs / 60.0, mode)
            return {"mode": mode, "watts": val, "seconds": secs,
                    "active": True, "until_epoch": self._manual_until}

    def manual_status(self) -> dict:
        """Thread-sicherer Zustand für Dashboard und Ablaufsteuerung."""
        with self._manual_lock:
            return {"active": self._manual_active,
                    "action": self._manual_action,
                    "mode": self._wd_mode if self._manual_active else 0,
                    "watts": self._wd_value if self._manual_active else 0,
                    "until_epoch": self._manual_until}

    def _manual_revert(self) -> None:
        with self._manual_lock:
            self._manual_timer = None
            self._manual_active = False
            self._manual_action = None
            self._manual_until = None
            self._wd_mode, self._wd_value = 0, 0
            try:
                self._set_power(0, 0)
                log.info("RSCP: manueller Eingriff abgelaufen -> auto.")
            except Exception as exc:  # pragma: no cover
                log.warning("RSCP: Auto-Rücksetzen nach Ablauf fehlgeschlagen (%s).", exc)

    def apply_control(self, row) -> dict:
        """Plan-Slot -> E3DC-Steuerung. Nur mit control_enabled=true.
        Aktives Netzladen/-entladen über Mode 3/4 (mit 10-s-Watchdog);
        reine Lade-/Entlade-BEGRENZUNG über persistente Limits (kein Watchdog).
        GREIFT REAL IN DEN SPEICHER EIN."""
        if not self.rc.control_enabled:
            return self._control_status(
                None, "disabled", "off", "E3DC-Steuerung ist deaktiviert.")
        with self._manual_lock:
            if self._manual_active:
                log.debug("RSCP: Optimierer-Sollwert ausgesetzt – Handbetrieb aktiv.")
                return self._control_status(
                    None, "manual", "manual",
                    "Optimierer-Steuerung durch Handbetrieb ausgesetzt.")
            hb = self.cfg.house_battery
            gc = float(row.get("batt_grid_charge_w", 0.0))
            gd = float(row.get("batt_grid_discharge_w", 0.0))
            try:
                if gc > 5.0:
                    # Netzladen: Mode 4 (grid_charge), Wert = Gesamt-Ladeleistung
                    # (PV zuerst, Netz für den Rest). Verifiziert @8 kW.
                    total = round(float(row.get("batt_dc_charge_w", 0.0)) + gc)
                    if self._set_limits(False) == -1:  # Limits aus, Mode regelt
                        raise RuntimeError("Freigabe der SmartPower-Limits abgelehnt")
                    self._wd_mode, self._wd_value = 4, total
                    self._ensure_watchdog()
                    self._set_power(4, total)
                    log.info("RSCP: Netzladen aktiv, Mode 4, %d W (Watchdog).",
                             total)
                    return self._verify_limits(
                        False, hb.max_dc_charge_w, hb.max_discharge_w,
                        "grid_charge")
                elif gd > 5.0 and self.cfg.optimization.allow_grid_discharge:
                    # Netz-Entladen: Mode 2 (discharge), Wert = Entladeleistung.
                    val = round(float(row.get("batt_discharge_w", 0.0)))
                    if self._set_limits(False) == -1:
                        raise RuntimeError("Freigabe der SmartPower-Limits abgelehnt")
                    self._wd_mode, self._wd_value = 2, val
                    self._ensure_watchdog()
                    self._set_power(2, val)
                    log.info("RSCP: Netz-Entladen aktiv, Mode 2, %d W (Watchdog).",
                             val)
                    return self._verify_limits(
                        False, hb.max_dc_charge_w, hb.max_discharge_w,
                        "grid_discharge")
                else:
                    # auto + persistente Lade-/Entlade-Limits gemäß Plan
                    if self._wd_mode != 0:
                        self._wd_mode, self._wd_value = 0, 0
                        self._set_power(0, 0)    # aktiv auf auto zurück
                    cl = float(row.get("batt_charge_limit_w", hb.max_dc_charge_w))
                    dl = float(row.get("batt_discharge_limit_w", hb.max_discharge_w))
                    limited = (cl < hb.max_dc_charge_w - 1
                               or dl < hb.max_discharge_w - 1)
                    if self._set_limits(limited, cl, dl) == -1:
                        raise RuntimeError("Setzen der SmartPower-Limits abgelehnt")
                    log.debug("RSCP: auto, Limit aktiv=%s "
                              "(Laden≤%.0f, Entladen≤%.0f).", limited, cl, dl)
                    status = self._verify_limits(limited, cl, dl, "limits")
                    (log.info if status.get("ok") else log.warning)(
                        "RSCP-Rücklesekontrolle: %s", status["message"])
                    return status
            except Exception as exc:
                log.warning("RSCP-Steuerung fehlgeschlagen (%s).", exc)
                return self._control_status(
                    False, "write_failed", "control",
                    f"E3DC-Steuerbefehl fehlgeschlagen: {exc}")
