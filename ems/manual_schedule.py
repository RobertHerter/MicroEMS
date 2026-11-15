"""Persistente, zeitgesteuerte E3/DC-Handeingriffe.

Geplante Netzladungen/Entladungen liegen in derselben lokalen SQLite wie die
E3/DC-Historie. Ein kleiner Dienstthread führt fällige Einträge aus und setzt
den Speicher danach auf Auto zurück. E3DCs 10-s-Watchdog und der zusätzliche
Timer in :class:`E3DCLink` bleiben die letzte Fail-safe-Schicht.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from typing import Optional

import pandas as pd

from .local_history import connect

log = logging.getLogger("ems.manual_schedule")

_ACTIVE = ("planned", "running")
_NO_SOC = "Kein aktueller E3/DC-SoC verfügbar"
# Liefert der Speicher zum Start keinen SoC, in diesem Abstand erneut fragen
# (read_live(force=True) ist ein echter RSCP-Abruf - nicht in jedem 1-s-Tick).
_SOC_RETRY_SECONDS = 10.0
# Laufender Eingriff: Abstand der SoC-Grenzprüfung und Zahl aufeinander-
# folgender Abrufe ohne SoC, nach der er beendet wird. Ein einzelner
# Lesefehler beendet ihn nicht (der Watchdog in E3DCLink hält ihn mit eigenem
# Zeitlimit); ohne SoC ist aber unbekannt, ob die Lade-/Entladegrenze schon
# erreicht ist. Mehr als 3 x 30 s = 90 s Blindflug sind dafür nicht vertretbar:
# danach zurück auf Auto - das ist die sichere Seite.
_SOC_CHECK_SECONDS = 30.0
_SOC_MAX_MISSES = 3
_SOC_LOST = "SoC wiederholt nicht lesbar - Eingriff vorsorglich beendet"


def _utc(value, timezone: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
    return ts.tz_convert("UTC")


class ManualScheduleStore:
    def __init__(self, path: str, timezone: str):
        self.path = path
        self.timezone = timezone
        self._lock = threading.Lock()
        self._init()

    def _con(self):
        """Verbindung (WAL + busy_timeout, siehe local_history.connect).
        Aufrufer schließen sie per ``closing`` - auch bei einer Ausnahme."""
        con = connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._lock:
            with closing(self._con()) as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS manual_battery_schedule ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " start_ts TEXT NOT NULL, end_ts TEXT NOT NULL,"
                    " action TEXT NOT NULL, watts REAL NOT NULL,"
                    " duration_minutes REAL NOT NULL, status TEXT NOT NULL,"
                    " created_ts TEXT NOT NULL, started_ts TEXT, finished_ts TEXT,"
                    " note TEXT DEFAULT '')")
                con.execute("CREATE INDEX IF NOT EXISTS idx_manual_schedule_start "
                            "ON manual_battery_schedule(start_ts, status)")
                con.commit()

    @staticmethod
    def _dict(row) -> Optional[dict]:
        return dict(row) if row is not None else None

    def add(self, action: str, start, watts: float, duration_minutes: float,
            max_watts: float, now=None) -> dict:
        action = str(action).lower()
        if action not in ("charge", "grid_charge", "pv_charge", "discharge", "idle"):
            raise ValueError("Unbekannter E3/DC-Modus")
        if start is None or str(start).strip() == "":
            raise ValueError("Startzeit fehlt")
        if start is None or str(start).strip() == "":
            raise ValueError("Startzeit fehlt")
        start = _utc(start, self.timezone)
        now = _utc(now or pd.Timestamp.now(tz=self.timezone), self.timezone)
        duration = float(duration_minutes)
        watts = float(watts)
        if start < now - pd.Timedelta(seconds=30):
            raise ValueError("Startzeit liegt in der Vergangenheit")
        if start > now + pd.Timedelta(days=30):
            raise ValueError("Startzeit darf höchstens 30 Tage vorausliegen")
        if not 1.0 <= duration <= 24 * 60:
            raise ValueError("Dauer muss zwischen 1 Minute und 24 Stunden liegen")
        if action == "idle":
            watts = 0.0
        elif not 1.0 <= watts <= float(max_watts):
            raise ValueError(f"Leistung muss zwischen 1 und {max_watts:.0f} W liegen")
        end = start + pd.Timedelta(minutes=duration)
        s, e = start.isoformat(), end.isoformat()
        created = now.isoformat()
        with self._lock:
            with closing(self._con()) as con:
                overlap = con.execute(
                    "SELECT id FROM manual_battery_schedule WHERE status IN (?,?) "
                    "AND start_ts < ? AND end_ts > ? LIMIT 1",
                    (*_ACTIVE, e, s)).fetchone()
                if overlap:
                    raise ValueError(
                        f"Zeitraum überschneidet sich mit Eintrag #{overlap['id']}")
                cur = con.execute(
                    "INSERT INTO manual_battery_schedule "
                    "(start_ts,end_ts,action,watts,duration_minutes,status,created_ts) "
                    "VALUES(?,?,?,?,?,'planned',?)",
                    (s, e, action, watts, duration, created))
                row = con.execute(
                    "SELECT * FROM manual_battery_schedule WHERE id=?",
                    (cur.lastrowid,)).fetchone()
                con.commit()
        return self._dict(row)

    def get(self, entry_id: int) -> Optional[dict]:
        with self._lock:
            with closing(self._con()) as con:
                row = con.execute(
                    "SELECT * FROM manual_battery_schedule WHERE id=?",
                    (int(entry_id),)).fetchone()
        return self._dict(row)

    def list(self, now=None) -> list[dict]:
        now = _utc(now or pd.Timestamp.now(tz=self.timezone), self.timezone)
        since = (now - pd.Timedelta(days=1)).isoformat()
        until = (now + pd.Timedelta(days=30)).isoformat()
        with self._lock:
            with closing(self._con()) as con:
                rows = con.execute(
                    "SELECT * FROM manual_battery_schedule "
                    "WHERE end_ts >= ? AND start_ts <= ? ORDER BY start_ts, id",
                    (since, until)).fetchall()
        return [dict(r) for r in rows]

    def active(self) -> list[dict]:
        with self._lock:
            with closing(self._con()) as con:
                rows = con.execute(
                    "SELECT * FROM manual_battery_schedule WHERE status IN (?,?) "
                    "ORDER BY start_ts, id", _ACTIVE).fetchall()
        return [dict(r) for r in rows]

    def set_status(self, entry_id: int, status: str, note: str = "") -> None:
        now = pd.Timestamp.now(tz="UTC").isoformat()
        extra = ""
        args: list = [status, str(note)]
        if status == "running":
            extra = ", started_ts=?"
            args.append(now)
        elif status in ("completed", "cancelled", "failed", "skipped"):
            extra = ", finished_ts=?"
            args.append(now)
        args.append(int(entry_id))
        with self._lock:
            with closing(self._con()) as con:
                con.execute(
                    f"UPDATE manual_battery_schedule SET status=?, note=?{extra} "
                    "WHERE id=?", args)
                con.commit()

    def delete(self, entry_id: int) -> bool:
        """Entfernt ausschließlich einen bereits beendeten Planeintrag.

        Die Statusbedingung liegt absichtlich im DELETE selbst, damit ein
        parallel anlaufender Scheduler-Tick nicht zwischen Prüfung und
        Löschen einen aktiven realen E3/DC-Eingriff hinterlassen kann.
        """
        with self._lock:
            with closing(self._con()) as con:
                cur = con.execute(
                    "DELETE FROM manual_battery_schedule "
                    "WHERE id=? AND status NOT IN (?,?)",
                    (int(entry_id), *_ACTIVE))
                con.commit()
                deleted = cur.rowcount > 0
        return deleted

class ManualScheduleRunner:
    """Führt gespeicherte Aktionen aus und hält SoC-Sicherheitsgrenzen ein."""

    def __init__(self, config, e3dc, poll_seconds: float = 1.0):
        self.config = config
        self.e3dc = e3dc
        self.store = ManualScheduleStore(
            config.e3dc_rscp.history_db_path, config.general.timezone)
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._active_id: Optional[int] = None
        self._last_soc_check = 0.0
        # Aufeinanderfolgende SoC-Abrufe ohne Wert beim laufenden Eingriff.
        self._soc_misses = 0
        # Eintrags-ID -> frühester Zeitpunkt (monotonic) des nächsten
        # SoC-Versuchs, solange der Start mangels SoC noch aussteht.
        self._soc_retry_at: dict = {}

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="manual-battery-schedule", daemon=True)
            self._thread.start()
            log.info("Manuelle Akku-Zeitplanung gestartet.")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def snapshot(self) -> dict:
        return {"entries": self.store.list(),
                "manual": self.e3dc.manual_status() if self.e3dc else {}}

    def add(self, payload: dict) -> dict:
        action = str(payload.get("action", "")).lower()
        hb = self.config.house_battery
        cap = (0.0 if action == "idle" else
               (hb.max_discharge_w if action == "discharge"
                else hb.max_dc_charge_w))
        return self.store.add(
            action, payload.get("start"), payload.get("watts", 0),
            payload.get("duration_minutes", 0), cap)

    def cancel(self, entry_id: int) -> dict:
        with self._lock:
            row = self.store.get(entry_id)
            if row is None:
                raise ValueError("Geplanter Vorgang nicht gefunden")
            if row["status"] not in _ACTIVE:
                raise ValueError("Vorgang ist nicht mehr aktiv")
            if row["status"] == "running" or self._active_id == int(entry_id):
                self.e3dc.manual_power("auto")
                self._active_id = None
            self.store.set_status(entry_id, "cancelled", "Vom Benutzer abgebrochen")
            return self.store.get(entry_id)

    def delete(self, entry_id: int) -> dict:
        row = self.store.get(entry_id)
        if row is None:
            raise ValueError("Planeintrag nicht gefunden")
        if row["status"] in _ACTIVE:
            raise ValueError("Aktiven Vorgang zuerst abbrechen")
        if not self.store.delete(entry_id):
            raise ValueError("Planeintrag konnte nicht gelöscht werden")
        return {"id": int(entry_id), "deleted": True}

    def _soc_allows(self, action: str) -> tuple[bool, str]:
        live = self.e3dc.read_live(force=True)
        soc = None if not live else live.get("soc_percent")
        if soc is None:
            return False, _NO_SOC
        soc = float(soc)
        hb = self.config.house_battery
        if action in ("charge", "grid_charge", "pv_charge") and \
                soc >= hb.max_soc_percent - 0.2:
            return False, f"Akku bereits bei {soc:.1f} %"
        if action == "discharge" and soc <= hb.min_soc_percent + 0.2:
            return False, f"Akku bereits an Mindest-SoC ({soc:.1f} %)"
        return True, ""

    def _finish(self, row: dict, status="completed", note="Plan beendet") -> None:
        if self._active_id == int(row["id"]):
            self.e3dc.manual_power("auto")
            self._active_id = None
            self._soc_misses = 0
        self._soc_retry_at.pop(int(row["id"]), None)
        self.store.set_status(row["id"], status, note)
        log.info("Manueller Akku-Plan #%s: %s (%s).", row["id"], status, note)

    def _tick(self) -> None:
        now = pd.Timestamp.now(tz="UTC")
        with self._lock:
            if (self.config.e3dc_rscp.enabled
                    and not self.config.e3dc_rscp.control_enabled):
                if self._active_id is not None:
                    self.store.set_status(
                        self._active_id, "cancelled",
                        "E3/DC-Steuerung wurde ausgeschaltet")
                    self._active_id = None
                return
            for row in self.store.active():
                start = pd.Timestamp(row["start_ts"])
                end = pd.Timestamp(row["end_ts"])
                if end <= now:
                    if row["status"] == "planned":
                        # Nie angelaufen (Dienst stand über das ganze Fenster
                        # oder es kam bis zuletzt kein SoC): das ist kein
                        # erledigter Vorgang.
                        note = ("Zeitfenster verstrichen - bis zuletzt kein "
                                "E3/DC-SoC verfügbar"
                                if int(row["id"]) in self._soc_retry_at else
                                "Zeitfenster verstrichen, ohne dass der "
                                "Vorgang lief (Dienst nicht aktiv)")
                        self._finish(row, "skipped", note)
                    else:
                        self._finish(row)
                    continue
                if start > now:
                    continue
                # Nach Dienstneustart einen noch laufenden persistenten Plan
                # mit der verbleibenden Dauer wieder aufnehmen.
                if row["status"] == "planned" or self._active_id != int(row["id"]):
                    rid = int(row["id"])
                    retry_at = self._soc_retry_at.get(rid)
                    if retry_at is not None and time.monotonic() < retry_at:
                        continue
                    ok, reason = self._soc_allows(row["action"])
                    if not ok and reason == _NO_SOC:
                        # Ein einzelner fehlender Messwert ist kein Grund, den
                        # Eintrag endgültig zu verwerfen: solange das Fenster
                        # läuft, erneut versuchen. Aufgegeben wird erst am
                        # Fensterende (oben, als "skipped").
                        if retry_at is None:
                            log.warning("Manueller Akku-Plan #%s: %s - neuer "
                                        "Versuch alle %.0f s bis Fensterende.",
                                        rid, reason, _SOC_RETRY_SECONDS)
                        self._soc_retry_at[rid] = (
                            time.monotonic() + _SOC_RETRY_SECONDS)
                        continue
                    self._soc_retry_at.pop(rid, None)
                    if not ok:
                        self.store.set_status(row["id"], "skipped", reason)
                        log.warning("Manueller Akku-Plan #%s übersprungen: %s",
                                    row["id"], reason)
                        continue
                    remaining = max(1.0, (end - now).total_seconds())
                    self.e3dc.manual_power(row["action"], row["watts"], remaining)
                    self.store.set_status(row["id"], "running")
                    self._active_id = int(row["id"])
                    self._last_soc_check = time.monotonic()
                    self._soc_misses = 0
                    log.info("Manueller Akku-Plan #%s gestartet: %s %.0f W.",
                             row["id"], row["action"], row["watts"])
                elif (time.monotonic() - self._last_soc_check
                      >= _SOC_CHECK_SECONDS):
                    self._last_soc_check = time.monotonic()
                    ok, reason = self._soc_allows(row["action"])
                    if ok:
                        self._soc_misses = 0
                    elif reason == _NO_SOC:
                        # Wie beim Start: ein einzelner fehlender Messwert
                        # beendet nichts. Anders als dort läuft der Eingriff
                        # hier aber schon - deshalb ist die Geduld begrenzt
                        # (siehe _SOC_MAX_MISSES) und das Ende kein stilles
                        # "completed", sondern ein sichtbarer Fehler.
                        self._soc_misses += 1
                        if self._soc_misses >= _SOC_MAX_MISSES:
                            log.warning("Manueller Akku-Plan #%s: %s.",
                                        row["id"], _SOC_LOST)
                            self._finish(row, "failed", _SOC_LOST)
                        else:
                            log.warning(
                                "Manueller Akku-Plan #%s: %s (%d/%d) - läuft "
                                "weiter, nächste Prüfung in %.0f s.",
                                row["id"], reason, self._soc_misses,
                                _SOC_MAX_MISSES, _SOC_CHECK_SECONDS)
                    else:
                        self._finish(row, "completed", reason)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # Scheduler darf den EMS-Dienst nie stoppen
                log.exception("Manuelle Akku-Zeitplanung fehlgeschlagen (%s).", exc)
            self._stop.wait(self.poll_seconds)
