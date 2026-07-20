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
from typing import Optional

import pandas as pd

log = logging.getLogger("ems.manual_schedule")

_ACTIVE = ("planned", "running")


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
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init(self) -> None:
        with self._lock:
            con = self._con()
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
            con.close()

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
            con = self._con()
            overlap = con.execute(
                "SELECT id FROM manual_battery_schedule WHERE status IN (?,?) "
                "AND start_ts < ? AND end_ts > ? LIMIT 1",
                (*_ACTIVE, e, s)).fetchone()
            if overlap:
                con.close()
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
            con.close()
        return self._dict(row)

    def get(self, entry_id: int) -> Optional[dict]:
        with self._lock:
            con = self._con()
            row = con.execute(
                "SELECT * FROM manual_battery_schedule WHERE id=?",
                (int(entry_id),)).fetchone()
            con.close()
        return self._dict(row)

    def list(self, now=None) -> list[dict]:
        now = _utc(now or pd.Timestamp.now(tz=self.timezone), self.timezone)
        since = (now - pd.Timedelta(days=1)).isoformat()
        until = (now + pd.Timedelta(days=30)).isoformat()
        with self._lock:
            con = self._con()
            rows = con.execute(
                "SELECT * FROM manual_battery_schedule "
                "WHERE end_ts >= ? AND start_ts <= ? ORDER BY start_ts, id",
                (since, until)).fetchall()
            con.close()
        return [dict(r) for r in rows]

    def active(self) -> list[dict]:
        with self._lock:
            con = self._con()
            rows = con.execute(
                "SELECT * FROM manual_battery_schedule WHERE status IN (?,?) "
                "ORDER BY start_ts, id", _ACTIVE).fetchall()
            con.close()
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
            con = self._con()
            con.execute(
                f"UPDATE manual_battery_schedule SET status=?, note=?{extra} "
                "WHERE id=?", args)
            con.commit()
            con.close()

    def delete(self, entry_id: int) -> bool:
        """Entfernt ausschließlich einen bereits beendeten Planeintrag.

        Die Statusbedingung liegt absichtlich im DELETE selbst, damit ein
        parallel anlaufender Scheduler-Tick nicht zwischen Prüfung und
        Löschen einen aktiven realen E3/DC-Eingriff hinterlassen kann.
        """
        with self._lock:
            con = self._con()
            cur = con.execute(
                "DELETE FROM manual_battery_schedule "
                "WHERE id=? AND status NOT IN (?,?)",
                (int(entry_id), *_ACTIVE))
            con.commit()
            deleted = cur.rowcount > 0
            con.close()
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
            return False, "Kein aktueller E3/DC-SoC verfügbar"
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
        self.store.set_status(row["id"], status, note)
        log.info("Manueller Akku-Plan #%s: %s (%s).", row["id"], status, note)

    def _tick(self) -> None:
        now = pd.Timestamp.now(tz="UTC")
        with self._lock:
            for row in self.store.active():
                start = pd.Timestamp(row["start_ts"])
                end = pd.Timestamp(row["end_ts"])
                if end <= now:
                    self._finish(row)
                    continue
                if start > now:
                    continue
                # Nach Dienstneustart einen noch laufenden persistenten Plan
                # mit der verbleibenden Dauer wieder aufnehmen.
                if row["status"] == "planned" or self._active_id != int(row["id"]):
                    ok, reason = self._soc_allows(row["action"])
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
                    log.info("Manueller Akku-Plan #%s gestartet: %s %.0f W.",
                             row["id"], row["action"], row["watts"])
                elif time.monotonic() - self._last_soc_check >= 30.0:
                    self._last_soc_check = time.monotonic()
                    ok, reason = self._soc_allows(row["action"])
                    if not ok:
                        self._finish(row, "completed", reason)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # Scheduler darf den EMS-Dienst nie stoppen
                log.exception("Manuelle Akku-Zeitplanung fehlgeschlagen (%s).", exc)
            self._stop.wait(self.poll_seconds)
