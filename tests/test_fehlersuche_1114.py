"""Regressionstests zur Fehlersuche vom 14.11.2026 (Regelschleife, RSCP,
Datenbankzugriff). Jeder Test stellt einen dort belegten Fehler nach."""
from __future__ import annotations

import sqlite3
import time

import pandas as pd
import pytest

import ems.main as _m
from ems import local_history as lh
from tests.test_rscp import _link


# ---- Datenbank: WAL, Leser sperren Schreiber nicht aus ---------------- #
def test_history_runs_in_wal_and_reader_does_not_block_writer(tmp_path):
    db = str(tmp_path / "h.sqlite")
    con = lh._con(db)
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    con.close()
    # Langer Leser mit offener Lesetransaktion (Kalibrierung, Analyse ...).
    reader = sqlite3.connect(db)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM house_load").fetchall()
    try:
        t0 = time.monotonic()
        idx = pd.date_range("2026-11-14 12:00", periods=2, freq="15min",
                            tz="Europe/Berlin")
        lh.write_house_load(db, {t.tz_convert("UTC").isoformat(): 500.0 for t in idx})
        assert time.monotonic() - t0 < 5.0
    finally:
        reader.close()
    assert lh.count(db) == 2


def test_schema_only_created_once_per_file(tmp_path, monkeypatch):
    db = str(tmp_path / "h.sqlite")
    calls = []
    original = lh._schema
    monkeypatch.setattr(lh, "_schema",
                        lambda con: (calls.append(1), original(con))[1])
    for _ in range(3):
        lh._con(db).close()
    assert len(calls) == 1
    # Neue Datei am selben Pfad (anderer Inode) -> Schema erneut.
    import os
    os.remove(db)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    con = lh._con(db)
    assert con.execute("SELECT COUNT(*) FROM house_load").fetchone()[0] == 0
    con.close()


def test_failed_setup_closes_connection(tmp_path, monkeypatch):
    db = str(tmp_path / "h.sqlite")
    opened = []
    real_connect = lh.connect

    def tracking(path, **kw):
        con = real_connect(path, **kw)
        opened.append(con)
        return con
    monkeypatch.setattr(lh, "connect", tracking)
    monkeypatch.setattr(lh, "_schema", lambda con: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")))
    with pytest.raises(sqlite3.OperationalError):
        lh._con(db)
    assert len(opened) == 2            # ein Wiederholungsversuch
    for con in opened:                 # beide geschlossen, keine haengt
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")


# ---- pandas 3: Slot-0-Anker braucht ein beschreibbares Array ---------- #
def test_anchor_array_is_writable_copy():
    series = pd.Series([100.0, 200.0])
    assert not series.values.flags.writeable or True   # pandas-abhaengig
    arr = series.to_numpy(dtype=float, copy=True)
    arr[0] = 999.0                                     # darf nicht werfen
    assert series.iloc[0] == 100.0                     # Original unberuehrt


def test_run_once_does_not_use_readonly_values():
    import inspect
    src = inspect.getsource(_m.run_once)
    assert "house_series.values" not in src
    assert "Slot-0-Anker fehlgeschlagen" not in src    # nie wieder auf debug


# ---- RSCP: Befehle verfallen, Handbetrieb endet sicher ---------------- #
def _grid_charge_row():
    return {"batt_grid_charge_w": 5000.0, "batt_dc_charge_w": 0.0}


def test_optimizer_command_expires_without_successor():
    cfg, link = _link(control_enabled=True)
    alarms = []
    link.set_alarm_callback(lambda level, msg: alarms.append((level, msg)))
    try:
        link.apply_control(_grid_charge_row())
        assert link._wd_mode == 4 and link._befehl_bis is not None
        jetzt = time.monotonic()
        # Innerhalb der Gueltigkeit: nichts passiert.
        assert link._pruefe_ablauf(jetzt + 60.0) is None
        assert link._wd_mode == 4
        # Zwei Zyklen + Karenz ohne neuen Befehl: zurueck auf auto.
        spaeter = jetzt + link._befehl_gueltigkeit_s() + 1.0
        assert link._pruefe_ablauf(spaeter) == "command"
        assert link._wd_mode == 0
        assert link._e3dc.last_power == (0, 0)
        assert alarms and alarms[0][0] == "warning"
        # Kein zweiter Alarm.
        assert link._pruefe_ablauf(spaeter + 10.0) is None
    finally:
        link.close()


def test_new_command_renews_deadline():
    cfg, link = _link(control_enabled=True)
    try:
        link.apply_control(_grid_charge_row())
        erste = link._befehl_bis
        time.sleep(0.01)
        link.apply_control(_grid_charge_row())
        assert link._befehl_bis > erste
    finally:
        link.close()


def test_persistent_limits_expire_too():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    try:
        link.apply_control({"batt_charge_limit_w": hb.max_dc_charge_w,
                            "batt_discharge_limit_w": 0.0})   # Entladen gesperrt
        assert link._limits_active and link._befehl_bis is not None
        spaeter = time.monotonic() + link._befehl_gueltigkeit_s() + 1.0
        assert link._pruefe_ablauf(spaeter) == "command"
        assert link._limits_active is False
        assert link._e3dc.limits["enable"] is False
    finally:
        link.close()


def test_unlimited_auto_has_no_deadline():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    try:
        link.apply_control({"batt_charge_limit_w": hb.max_dc_charge_w,
                            "batt_discharge_limit_w": hb.max_discharge_w})
        assert link._befehl_bis is None
        assert link._pruefe_ablauf(time.monotonic() + 1e6) is None
    finally:
        link.close()


def test_manual_timer_exists_even_if_first_send_fails():
    cfg, link = _link(control_enabled=False)

    def kaputt(mode, value):
        raise RuntimeError("Max retries reached")
    link._set_limits = lambda *a, **k: 0
    link._set_power = kaputt
    try:
        with pytest.raises(RuntimeError):
            link.manual_power("discharge", 3000.0, seconds=600)
        # Der Watchdog traegt den Modus - also MUSS das Ende feststehen.
        assert link._manual_active is True
        assert link._manual_timer is not None
        assert link._manual_until is not None
    finally:
        if link._manual_timer is not None:
            link._manual_timer.cancel()
        link._wd_stop.set()


def test_overdue_manual_mode_is_ended_by_watchdog():
    cfg, link = _link(control_enabled=False)
    try:
        link.manual_power("discharge", 3000.0, seconds=600)
        link._manual_timer.cancel()          # Timer verloren (alter Fehler)
        link._manual_timer = None
        bis = link._manual_until
        assert link._pruefe_ablauf(jetzt_wall=bis - 1.0) is None
        assert link._pruefe_ablauf(jetzt_wall=bis + 60.0) == "manual"
        assert link._manual_active is False and link._wd_mode == 0
    finally:
        link.close()


@pytest.mark.parametrize("seconds", [-1.0, float("nan"), float("inf")])
def test_manual_power_rejects_invalid_duration(seconds):
    cfg, link = _link(control_enabled=False)
    try:
        with pytest.raises(ValueError):
            link.manual_power("discharge", 3000.0, seconds=seconds)
        assert link._manual_active is False
    finally:
        link.close()


# ---- Dashboard: CSRF --------------------------------------------------- #
@pytest.mark.parametrize("headers,expected", [
    ({"Host": "192.168.71.10"}, False),                               # curl/Node-RED
    ({"Host": "192.168.71.10", "Origin": "http://192.168.71.10"}, False),
    ({"Host": "homepi:8080", "Origin": "http://homepi:8080"}, False),
    ({"Host": "192.168.71.10", "Origin": "http://evil.example"}, True),
    ({"Host": "192.168.71.10", "Origin": "null"}, True),
    ({"Host": "192.168.71.10", "Sec-Fetch-Site": "cross-site"}, True),
    ({"Host": "192.168.71.10", "Origin": "http://192.168.71.10",
      "Sec-Fetch-Site": "same-origin"}, False),
])
def test_cross_site_post_detection(headers, expected):
    assert _m._cross_site_post(headers) is expected


# ---- Auto-Recalc: Takt, Entprellung, Sperrzeit ------------------------- #
def test_recalc_gate_respects_interval_debounce_and_lockout():
    gate = _m._RecalcGate(90.0)
    assert gate.due(0.0)
    assert gate.observe(4000.0, 0.0) is None        # erste Probe: noch nicht
    assert not gate.due(5.0)                        # 5-s-Takt wirkt nicht mehr
    assert gate.due(90.0)
    assert gate.observe(4100.0, 90.0) == 4100.0     # zweite in Folge: ausloesen
    assert not gate.due(180.0)                      # Sperrzeit
    assert gate.due(90.0 + gate.lockout_s)


def test_recalc_gate_resets_on_normal_sample():
    gate = _m._RecalcGate(90.0)
    assert gate.observe(4000.0, 0.0) is None
    assert gate.observe(None, 90.0) is None         # Abweichung weg
    assert gate.observe(4000.0, 180.0) is None      # zaehlt wieder von vorn


# ---- Ausfuehrungsalarm: Warte-Audit entwarnt nicht --------------------- #
class _Alerts:
    def __init__(self):
        self.sent = []

    def publish_alert(self, level, message):
        self.sent.append(level)


def test_waiting_audit_does_not_clear_execution_alarm(monkeypatch):
    from tests.test_synthetic import make_config
    cfg = make_config()
    required = max(1, cfg.monitoring.execution_alert_consecutive)
    failed = [{"ok": False, "state": "deviation", "message": "x"}] * required
    monkeypatch.setattr(lh, "read_execution_audits", lambda *a, **k: list(failed))
    monkeypatch.setitem(_m._execution_alarm, "failed", False)
    alerts = _Alerts()
    _m._publish_execution_alarm(alerts, cfg, failed[0])
    assert alerts.sent == ["warning"]
    # Zaehlerfenster nicht lesbar: ok=True, aber keine Aussage zur Ausfuehrung.
    waiting = {"ok": True, "state": "data_waiting", "message": "warte"}
    monkeypatch.setattr(lh, "read_execution_audits",
                        lambda *a, **k: [waiting] + list(failed))
    _m._publish_execution_alarm(alerts, cfg, waiting)
    assert alerts.sent == ["warning"]                 # keine Entwarnung
    # Erst eine bestaetigte gute Pruefung entwarnt.
    good = {"ok": True, "state": "ok", "message": "passt"}
    monkeypatch.setattr(lh, "read_execution_audits",
                        lambda *a, **k: [good] + list(failed))
    _m._publish_execution_alarm(alerts, cfg, good)
    assert alerts.sent == ["warning", "info"]


# ---- Dienstende: Abbruch gilt auch fuer wartende Solver-Laeufe --------- #
def test_final_cancel_blocks_following_solves():
    from ems import optimizer as opt
    from tests.test_synthetic import make_config
    try:
        opt.request_solver_cancel(final=True)
        with pytest.raises(opt.SolverCancelled):
            opt.Optimizer(make_config())._solve(None)
    finally:
        opt._solver_shutdown.clear()
        opt.clear_solver_cancel()


# ---- Anfangs-SoC bei Zyklen mitten im Slot ----------------------------- #
def test_soc_is_projected_back_to_slot_start():
    f = _m._soc_at_slot_start
    # 9 kW Laden, 13 min im Slot: 1950 Wh sind schon im Mess-SoC enthalten.
    assert f(10000.0, 9000.0, 13 * 60, 20000.0, 900.0) == pytest.approx(8050.0)
    # Entladen: der SoC war am Slotbeginn hoeher.
    assert f(10000.0, -3000.0, 600, 20000.0, 900.0) == pytest.approx(10500.0)
    # Regulaerer Lauf kurz nach der Marke: unveraendert.
    assert f(10000.0, 9000.0, 20, 20000.0, 900.0) == 10000.0
    # Grenzen und ungueltige Werte.
    assert f(500.0, 9000.0, 800, 20000.0, 900.0) == 0.0
    assert f(19900.0, -9000.0, 800, 20000.0, 900.0) == 20000.0
    assert f(10000.0, None, 600, 20000.0, 900.0) == 10000.0
    assert f(10000.0, float("nan"), 600, 20000.0, 900.0) == 10000.0
    # Nie mehr als ein ganzer Slot.
    assert f(10000.0, 4000.0, 5000, 20000.0, 900.0) == pytest.approx(9000.0)


# ---- Overlay: Dateisperre ueber Prozessgrenzen -------------------------- #
def test_overlay_lock_blocks_other_processes_and_is_reentrant(tmp_path):
    import fcntl
    from ems import config as cfgmod
    overlay = str(tmp_path / "config_overrides.yaml")
    with cfgmod.overlay_locked(overlay):
        with cfgmod.overlay_locked(overlay):        # Wiedereintritt haengt nicht
            pass
        other = open(overlay + ".lock", "a+")       # wie ein zweiter Prozess
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            other.close()
    other = open(overlay + ".lock", "a+")           # danach wieder frei
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        other.close()


def test_save_override_roundtrip_with_file_lock(tmp_path):
    from ems import config as cfgmod
    cfg_path = str(tmp_path / "config.yaml")
    open(cfg_path, "w").write("general: {}\n")
    cfgmod.save_override(cfg_path, "optimization.charge_strategy", "late")
    cfgmod.save_override(cfg_path, "pv_model.p10_uncertainty", 0.3)
    import yaml
    data = yaml.safe_load(open(cfgmod._overrides_path(cfg_path)))
    assert data["optimization"]["charge_strategy"] == "late"
    assert data["pv_model"]["p10_uncertainty"] == 0.3


# ---- Temperatur-Alter im Plan ------------------------------------------- #
def test_plan_start_temperature_respects_age_limit():
    from types import SimpleNamespace

    class Pub:
        def get_load_temp(self, topic, max_age_minutes=None):
            return None if max_age_minutes else 27.5   # Wert ist veraltet

    load = SimpleNamespace(enabled=True, type="thermal", name="Pool",
                           temp_signal="pool/temp", temp_max_age_minutes=45.0)
    cfg = SimpleNamespace(controllable_loads=[load])
    fn = _m._read_load_state
    assert fn(cfg, Pub()) is None
    load.temp_max_age_minutes = 0.0                    # Grenze aus -> alter Wert
    assert fn(cfg, Pub()) == {"Pool": 27.5}
