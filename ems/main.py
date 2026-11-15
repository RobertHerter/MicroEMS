"""Orchestrierung des EMS-Laufs.

Ablauf je Zyklus:
  1. Verbrauchsprognose (72 h) aus Historie erzeugen und in InfluxDB schreiben.
  2. Eingangsdaten für den Optimierungshorizont (48 h) lesen
     (Preis & PV-Vorhersage für die Zukunft, aktueller Haus-/Auto-SoC).
  3. MILP-Optimierung -> Steuertabelle.
  4. Steuerbefehle per MQTT an Homey publizieren.
  5. Steuertabelle + prognostizierte SoCs/Zustände in InfluxDB schreiben.
  6. Dashboard erzeugen.

Aufruf:
  python -m ems.main --config config.yaml          # einmalig
  python -m ems.main --config config.yaml --loop    # Dauerbetrieb (Intervall aus config)
"""
from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import signal
import threading
import time as _time
from datetime import timedelta

import numpy as np
import pandas as pd

from .time_utils import floor_time

from .config import Config, load_config
from .dashboard import build_dashboard
from .forecast import (LoadForecaster, dampen_estimated,
                       intraday_factor_series, intraday_ratio, load_history,
                       stabilize_intraday_ratio)
from .local_history import read_actual_signal, write_actuals
from .homey_mqtt import HomeyMqttPublisher
from .influx import make_repository
from .optimizer import (Optimizer, OptimizerInputs, SolverCancelled,
                        request_solver_cancel, solver_is_running)
from .priceforecast import baseline_name
from .tariff import read_price_signal
from . import solcast
from .validate import summarize, validate_plan
# Ausgelagerte Bloecke: die Namen bleiben ueber ems.main erreichbar (Tests,
# Skripte); die Module schlagen ihrerseits spaet ueber ems.main nach.
from .execution_audit import (  # noqa: F401
    STRING_SAMPLE_S, _AUDIT_FULL_SOC_PCT, _AUDIT_SOC_MARGIN_PCT, _INFO_SPERRE,
    _LiveExecutionMonitor, _audit_execution)
from .dashboard_server import (  # noqa: F401
    _RateLimiter, _cross_site_post, _resolve_get_route, _resolve_post_route,
    start_dashboard_server, static_asset_allowed)
from .display_frame import _build_display_frame  # noqa: F401

if __name__ == "__main__":
    # Der Dienst startet als ``python -m ems.main``; dieses Modul heisst dann
    # ``__main__``. Die ausgelagerten Module (execution_audit, dashboard_server,
    # display_frame) schlagen Funktionen und Zustand spaet ueber ``ems.main``
    # nach - ohne diesen Eintrag wuerde dabei eine ZWEITE Kopie dieses Moduls
    # geladen, mit eigenem Laufzeitzustand und eigenen Sperren.
    import sys
    sys.modules["ems.main"] = sys.modules[__name__]

log = logging.getLogger("ems.main")

_intraday_state = {
    "load": {"issue": None, "ratio": 1.0, "applied": None, "seeded": False},
    "pv": {"issue": None, "ratio": 1.0, "applied": None, "seeded": False},
}
_intraday_raw = {"load": None, "pv": None}
_control_alarm = {"failed": False, "key": None, "last": 0.0}
_execution_alarm = {"failed": False}
_solver_alarm = {"failed": False}
# Prozessstart (monoton): kurz nach einem (Neu-)Start ist die RSCP-Steuerung noch
# nicht wieder gesetzt/eingependelt -> das Ausführungs-Audit würde fälschlich eine
# "Planabweichung Akku" melden. In einer Karenz danach wird es ausgesetzt.
_PROCESS_START = _time.monotonic()
_load_feedback_alarm = {"failed": set()}
_runtime_lock = threading.RLock()
_runtime_status = {
    "state": "starting", "phase": "Dienst startet", "progress": 0,
    "started_at": None, "finished_at": None, "plan_generated": None,
    "duration_seconds": None, "message": "Initialisierung",
    "pending_recalc": False, "trigger": "startup", "sequence": 0,
}
_cycle_watchdog_status = {
    "overdue": False, "message": None, "since": None,
}
_shadow_status = {
    "state": "idle", "started_at": None, "finished_at": None,
    "generated": None, "message": "Noch kein automatischer Vergleich",
    "result": None,
}
_service_restart_requested = threading.Event()
_shadow_curves = {"generated": None, "series": None}
# Snapshot der Eingaben des letzten Produktivlaufs für den What-if-Simulator
# (Dashboard): so kann mit geänderten Parametern neu gerechnet werden, ohne die
# Datenpipeline erneut zu ziehen. Nur lesend genutzt, nie in die Steuerung.
_last_solve_snapshot = {"inputs": None, "config": None}
# Monotone Zeit des letzten erfolgreichen Zyklus (Staleness-Watchdog, #7).
_last_cycle_ok = {"monotonic": None}


def _runtime_iso() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def _runtime_update(**changes) -> None:
    with _runtime_lock:
        _runtime_status.update(changes)


def _runtime_snapshot() -> dict:
    with _runtime_lock:
        out = dict(_runtime_status)
        out["shadow_comparison"] = dict(_shadow_status)
        out["cycle_watchdog"] = dict(_cycle_watchdog_status)
        return out


def _request_runtime_recalc() -> tuple[bool, dict]:
    """Genau einen Folgelauf vormerken; wiederholte Klicks werden gebuendelt."""
    with _runtime_lock:
        fresh = not bool(_runtime_status.get("pending_recalc"))
        _runtime_status["pending_recalc"] = True
        if _runtime_status.get("state") != "running":
            _runtime_status.update(
                state="queued", phase="Neuberechnung vorgemerkt", progress=0,
                message="Manuelle Neuberechnung wartet auf den EMS-Loop")
        return fresh, dict(_runtime_status)


def _runtime_begin() -> str:
    with _runtime_lock:
        trigger = "manual" if _runtime_status.get("pending_recalc") else "schedule"
        _runtime_status.update(
            state="running", phase="Datenquellen aktualisieren", progress=5,
            started_at=_runtime_iso(), finished_at=None, duration_seconds=None,
            message="EMS-Zyklus läuft", pending_recalc=False, trigger=trigger,
            sequence=int(_runtime_status.get("sequence", 0)) + 1)
        return trigger


def _runtime_finish(now, started_monotonic: float,
                    message="Plan erfolgreich aktualisiert") -> None:
    with _runtime_lock:
        pending = bool(_runtime_status.get("pending_recalc"))
        _runtime_status.update(
            state="queued" if pending else "ready",
            phase="Weitere Neuberechnung vorgemerkt" if pending else "Fertig",
            progress=0 if pending else 100, finished_at=_runtime_iso(),
            plan_generated=pd.Timestamp(now).isoformat(),
            duration_seconds=round(_time.monotonic() - started_monotonic, 2),
            message=("Plan aktualisiert; weiterer Lauf wartet" if pending else message))
        _cycle_watchdog_status.update(
            overdue=False, message=None, since=None)
    # Zeitstempel des letzten ERFOLGREICHEN Zyklus (für den Staleness-Watchdog).
    _last_cycle_ok["monotonic"] = _time.monotonic()


def _cycle_overdue(last_ok_monotonic, now_monotonic: float,
                   threshold_s: float):
    """Sekunden, die der letzte erfolgreiche Zyklus überfällig ist – oder None,
    wenn frisch bzw. noch nie ein Zyklus erfolgreich war (kein Startup-Alarm).
    Rein, damit die Staleness-Entscheidung ohne Threads testbar ist."""
    if last_ok_monotonic is None or threshold_s <= 0:
        return None
    overdue = now_monotonic - last_ok_monotonic - threshold_s
    return overdue if overdue > 0 else None


def _staleness_threshold_minutes(config):
    """Schwelle für den Staleness-Watchdog: konfiguriert, 0 = auto
    (2,5 × run_interval), negativ = aus (None)."""
    minutes = float(getattr(config.monitoring, "cycle_staleness_alert_minutes", 0.0))
    if minutes < 0:
        return None
    return minutes if minutes > 0 else 2.5 * config.general.run_interval_minutes


def _start_cycle_watchdog(config, publisher, stop_event):
    """Daemon-Thread, der alarmiert, wenn seit der Schwelle kein Zyklus mehr
    ERFOLGREICH durchlief (Dienst hängt, RSCP/Netz klemmt) – ergänzt den
    systemd-Watchdog (der nur Totalausfälle fängt). Gedrosselt + Entwarnung."""
    threshold_min = _staleness_threshold_minutes(config)
    if threshold_min is None or publisher is None:
        return None
    threshold_s = threshold_min * 60.0
    repeat_s = max(60.0, float(config.e3dc_rscp.control_alarm_repeat_minutes) * 60.0)
    check_s = max(20.0, min(60.0, threshold_s / 3.0))
    state = {"alarmed": False, "last": 0.0}

    def loop():
        while not stop_event.wait(check_s):
            overdue = _cycle_overdue(
                _last_cycle_ok["monotonic"], _time.monotonic(), threshold_s)
            now = _time.time()
            if overdue is not None:
                mins = (overdue + threshold_s) / 60.0
                with _runtime_lock:
                    _cycle_watchdog_status.update(
                        overdue=True,
                        message=(f"Seit {mins:.0f} min kein erfolgreicher "
                                 "Optimierungszyklus"),
                        since=_runtime_iso())
                if not state["alarmed"] or now - state["last"] >= repeat_s:
                    try:
                        publisher.publish_alert(
                            "error", f"EMS: seit {mins:.0f} min kein erfolgreicher "
                            "Optimierungszyklus (Dienst hängt?).")
                    except Exception as exc:  # pragma: no cover
                        log.debug("Watchdog-Alarm nicht publizierbar: %s", exc)
                    state.update(alarmed=True, last=now)
            elif state["alarmed"] and _last_cycle_ok["monotonic"] is not None:
                with _runtime_lock:
                    _cycle_watchdog_status.update(
                        overdue=False, message=None, since=None)
                try:
                    publisher.publish_alert("info", "EMS: Zyklen laufen wieder.")
                except Exception as exc:  # pragma: no cover
                    log.debug("Watchdog-Entwarnung nicht publizierbar: %s", exc)
                state["alarmed"] = False

    thread = threading.Thread(target=loop, name="ems-cycle-watchdog", daemon=True)
    thread.start()
    return thread


def _runtime_fail(message: str) -> None:
    _runtime_update(state="error", phase="Fehler", progress=100,
                    finished_at=_runtime_iso(), message=str(message))


def _record_dashboard_event(config, kind: str, message: str, *,
                            level="info", details=None) -> None:
    try:
        from .local_history import write_dashboard_event
        write_dashboard_event(config.e3dc_rscp.history_db_path, kind, message,
                              level=level, details=details)
    except Exception as exc:
        log.debug("Dashboard-Ereignis nicht speicherbar (%s).", exc)


# --- Alarme (Warnungen/Fehler/Infos) zusätzlich ins Ereignis-Log ------------ #
# publish_alert schreibt bisher nur nach MQTT. Über einen Sink am Publisher
# landet JEDER Alarm auch im Bedien-/Ereignisverlauf des Dashboards. Dedupe:
# viele Alarme (Ziel-SoC, Anschluss-Überlast, Planprüfung) feuern JEDEN Zyklus,
# solange die Ursache besteht - identische Meldungen (Zahlen maskiert) werden
# 30 min lang nur einmal geloggt, Zustandswechsel/Erholungen sofort.
_ALERT_EVENT_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_alert_event_last: dict = {}
_ALERT_EVENT_WINDOW_S = 1800.0


def _make_alert_event_sink(config):
    def sink(level: str, message: str) -> None:
        try:
            key = f"{level}|{_ALERT_EVENT_NUM.sub('#', str(message))}"
            now = _time.monotonic()
            if now - _alert_event_last.get(key, -1e18) < _ALERT_EVENT_WINDOW_S:
                return
            if len(_alert_event_last) > 300:
                _alert_event_last.clear()
            _alert_event_last[key] = now
            _record_dashboard_event(config, "alarm", str(message), level=str(level))
        except Exception as exc:   # pragma: no cover - reine Absicherung
            log.debug("Alarm-Ereignis nicht speicherbar (%s).", exc)
    return sink


# Schaltvorgänge: nur bei tatsächlicher Änderung gegenüber dem letzten Zyklus.
_last_switch_state: dict = {"ctl": None, "loads": {}}
# Neutraler Ausgangszustand (keine Begrenzung/kein Netzladen): so werden schon
# beim ERSTEN Zyklus nach dem Start aktive Begrenzungen als Ereignis geloggt.
_NEUTRAL_CTL = {"charge_lim": False, "charge_w": 0.0, "dis_lim": False,
                "dis_w": 0.0, "grid_charge_w": 0.0, "grid_dis": False}
# Grenzwert-Änderungen ab dieser Schwelle (W) als neues Ereignis - kleiner
# Rampen-Jitter innerhalb einer laufenden Begrenzung erzeugt sonst je Slot einen
# Eintrag.
_SWITCH_THR_W = 50.0
# Modusvergleich nur bei WECHSEL der Empfehlung loggen (sonst flutet er als
# Jede-Zyklus-Ereignis den Verlauf und verdeckt die echten Schalthandlungen).
_last_shadow_recommendation: dict = {"value": None}


def _control_state(row) -> dict:
    """Grobe (bool'sche) Steueraktion des aktuellen Slots aus der Plantabelle -
    Grundlage für Schalthandlungs-Ereignisse. Nur Ein/Aus der Begriffe zählt
    (nicht jeder Watt-Wert), damit der Verlauf nicht bei jeder Rampe volläuft."""
    def num(col):
        try:
            return float(row.get(col, 0.0) or 0.0)
        except Exception:
            return 0.0
    return {
        "charge_lim": num("charge_limited") > 0.5,
        "charge_w": num("batt_charge_limit_w"),
        "dis_lim": num("discharge_limited") > 0.5,
        "dis_w": num("batt_discharge_limit_w"),
        "grid_charge": num("batt_grid_charge_w") > 5.0,
        "grid_charge_w": num("batt_grid_charge_w"),
        "grid_dis": num("batt_grid_discharge_w") > 5.0,
    }


def _log_switching_events(config, table, load_cmds) -> None:
    """Tatsächliche Schalthandlungen des aktuellen Slots ins Ereignis-Log:
    Lade-/Entladebegrenzung an/aus, Netzladen/-einspeisung an/aus sowie
    Laststufen-Wechsel - jeweils nur beim Übergang gegenüber dem Vorzyklus."""
    try:
        if table is None or len(table) == 0:
            return
        cur = _control_state(table.iloc[0])
        prev = _last_switch_state.get("ctl") or _NEUTRAL_CTL
        _last_switch_state["ctl"] = cur
        msgs = []
        # Ladebegrenzung: neu gesetzt ODER Grenzwert deutlich geändert (z.B. beim
        # Peak-Shaping rampt der Wert -> jede spürbare Änderung ist ein Ereignis).
        if cur["charge_lim"]:
            if (not prev["charge_lim"]
                    or abs(cur["charge_w"] - prev["charge_w"]) >= _SWITCH_THR_W):
                msgs.append("Laden gesperrt (Limit 0 W)" if cur["charge_w"] < 50
                            else f"Laden begrenzt auf {cur['charge_w']:.0f} W")
        elif prev["charge_lim"]:
            msgs.append("Ladebegrenzung aufgehoben")
        # Entladebegrenzung analog
        if cur["dis_lim"]:
            if (not prev["dis_lim"]
                    or abs(cur["dis_w"] - prev["dis_w"]) >= _SWITCH_THR_W):
                msgs.append("Entladen gesperrt (Akku halten)" if cur["dis_w"] < 50
                            else f"Entladen begrenzt auf {cur['dis_w']:.0f} W")
        elif prev["dis_lim"]:
            msgs.append("Entladebegrenzung aufgehoben")
        # Netzladen (Wert)
        gc, pgc = cur["grid_charge_w"], prev["grid_charge_w"]
        if gc > 5.0:
            if pgc <= 5.0 or abs(gc - pgc) >= _SWITCH_THR_W:
                msgs.append(f"Netzladen {gc:.0f} W")
        elif pgc > 5.0:
            msgs.append("Netzladen beendet")
        # Netzeinspeisung aus Akku
        if cur["grid_dis"] and not prev["grid_dis"]:
            msgs.append("Netzeinspeisung aus Akku gestartet")
        elif not cur["grid_dis"] and prev["grid_dis"]:
            msgs.append("Netzeinspeisung aus Akku beendet")
        for msg in msgs:
            _record_dashboard_event(config, "switch", msg)
        old = _last_switch_state.get("loads", {})
        loads = {str(k): int(bool(v)) for k, v in (load_cmds or {}).items()}
        for label, on in loads.items():
            if label in old and on != old[label]:
                _record_dashboard_event(
                    config, "switch",
                    f"{label} {'eingeschaltet' if on else 'ausgeschaltet'}",
                    details={"load": label, "on": on})
        _last_switch_state["loads"] = {**old, **loads}
    except Exception as exc:   # pragma: no cover - reine Absicherung
        log.debug("Schaltvorgang-Ereignis nicht speicherbar (%s).", exc)


def _comparison_metrics(table: pd.DataFrame, total_cost_ct: float,
                        slot_minutes: int) -> dict:
    dt_kwh = float(slot_minutes) / 60_000.0

    def energy(col):
        values = table[col] if col in table else pd.Series(0.0, index=table.index)
        return round(float(pd.to_numeric(values, errors="coerce").fillna(0).sum())
                     * dt_kwh, 3)

    export = (table["grid_export_w"] if "grid_export_w" in table
              else pd.Series(0.0, index=table.index))
    modes = (table["mode"] if "mode" in table
             else pd.Series("auto", index=table.index)).astype(str)
    interventions = int((~modes.isin(["auto", "peak", "late"])).sum())
    return {
        "cost_eur": round(float(total_cost_ct) / 100.0, 2),
        "grid_import_kwh": energy("grid_import_w"),
        "grid_export_kwh": energy("grid_export_w"),
        "curtailment_kwh": energy("pv_curtail_w"),
        "peak_export_w": round(float(pd.to_numeric(
            export, errors="coerce").fillna(0).max()), 0),
        "restricted_slots": int(modes.isin(
            ["hold", "limit_discharge", "block_charge", "grid_charge"]).sum()),
        "intervention_slots": interventions,
        "end_soc_percent": (round(float(table["house_soc_percent"].iloc[-1]), 1)
                            if "house_soc_percent" in table and len(table) else None),
        "max_soc_percent": None,
        "max_soc_at": None,
    }


def _comparison_metrics_complete(table: pd.DataFrame, total_cost_ct: float,
                                 slot_minutes: int) -> dict:
    metrics = _comparison_metrics(table, total_cost_ct, slot_minutes)
    if "house_soc_percent" in table and len(table):
        soc = pd.to_numeric(table["house_soc_percent"], errors="coerce")
        if soc.notna().any():
            maximum = float(soc.max())
            metrics["max_soc_percent"] = round(maximum, 1)
            metrics["max_soc_at"] = pd.Timestamp(soc.idxmax()).isoformat()
    return metrics


def _comparison_series(table: pd.DataFrame) -> dict:
    """Kompakte Kurven fuer den gleichzeitigen Dashboard-Modusvergleich."""
    def values(*terms):
        total = sum((factor * pd.to_numeric(table.get(
            column, pd.Series(0.0, index=table.index)),
            errors="coerce").fillna(0.0) for column, factor in terms),
            start=pd.Series(0.0, index=table.index))
        return [round(float(value), 2) for value in total]

    return {
        "battery_w": values(("batt_dc_charge_w", 1),
                            ("batt_ac_charge_w", 1),
                            ("batt_discharge_w", -1)),
        "grid_w": values(("grid_import_w", 1), ("grid_export_w", -1)),
        "soc_percent": values(("house_soc_percent", 1)),
    }


def _late_target_soc(result, inputs: OptimizerInputs, timezone: str):
    """SoC am Ende der nutzbaren PV-Fenster vollstaendiger Horizont-Tage."""
    local = inputs.index.tz_convert(timezone)
    days = list(local.date)
    targets = []
    for day in sorted(set(days)):
        slots = [i for i, value in enumerate(days) if value == day]
        if not slots or max(local[i].hour for i in slots) < 15:
            continue
        surplus = [i for i in slots
                   if float(inputs.pv_w[i]) > float(inputs.house_load_w[i]) + 1.0]
        if surplus:
            slot = max(surplus)
            # Positionsindex nur nutzen, wenn die Ergebnistabelle wirklich so lang
            # ist (ein neutrales/Fallback-Ergebnis kann kürzer sein -> sonst
            # IndexError, der den Schattenvergleich still killt).
            if slot >= len(result.table):
                continue
            soc = float(pd.to_numeric(
                result.table["house_soc_percent"], errors="coerce").iloc[slot])
            if np.isfinite(soc):
                targets.append((slot, soc))
    if not targets:
        return None, None
    slot, soc = min(targets, key=lambda item: item[1])
    return round(soc, 1), pd.Timestamp(inputs.index[slot]).isoformat()


def _late_confidence(config, inputs: OptimizerInputs, late_result) -> dict:
    """Erreichbarkeit des Late-Ziels mit Erwartungswert und PV-P10 pruefen.

    Netzladen wird fuer diese reine Prognoseaussage ausgeschlossen. So kann ein
    guenstiger AC-Ladeslot eine zu schwache PV-Prognose nicht als sicher tarnen.
    """
    target = float(config.house_battery.max_soc_percent)
    cfg = copy.deepcopy(config)
    cfg.optimization.charge_strategy = "late"
    cfg.house_battery.max_ac_charge_w = 0.0
    expected = late_result
    ac = pd.to_numeric(late_result.table.get(
        "batt_ac_charge_w", pd.Series(0.0, index=late_result.table.index)),
        errors="coerce").fillna(0.0)
    if bool((ac > 1.0).any()):
        expected = Optimizer(cfg, stabilize_plan=False, store_warm=False).solve(inputs)
    expected_soc, expected_at = _late_target_soc(
        expected, inputs, config.general.timezone)

    p10 = inputs.pv10_w
    if p10 is None or len(p10) != len(inputs.index):
        return {"code": "unavailable", "label": "P10 nicht verfügbar",
                "target_soc_percent": target,
                "expected_soc_percent": expected_soc,
                "expected_target_at": expected_at,
                "p10_soc_percent": None, "p10_target_at": None}

    p10_values = np.minimum(np.asarray(inputs.pv_w, dtype=float),
                            np.nan_to_num(np.asarray(p10, dtype=float), nan=0.0))
    p10_inputs = copy.deepcopy(inputs)
    p10_inputs.pv_w = p10_values
    p10_inputs.pv10_w = p10_values.copy()
    conservative = Optimizer(
        cfg, stabilize_plan=False, store_warm=False).solve(p10_inputs)
    p10_soc, p10_at = _late_target_soc(
        conservative, p10_inputs, config.general.timezone)
    reaches_expected = expected_soc is not None and expected_soc >= target - 0.5
    reaches_p10 = p10_soc is not None and p10_soc >= target - 0.5
    if reaches_p10:
        code, label = "very_likely", "100 % sehr wahrscheinlich"
    elif reaches_expected:
        code, label = "expected_only", "nur mit Erwartungsprognose erreichbar"
    else:
        code, label = "p10_unreachable", "unter P10 nicht erreichbar"
    return {"code": code, "label": label, "target_soc_percent": target,
            "expected_soc_percent": expected_soc,
            "expected_target_at": expected_at, "p10_soc_percent": p10_soc,
            "p10_target_at": p10_at,
            "solver_seconds": round(float(conservative.solver_seconds), 2)}


def _selected_comparison_mode(config, inputs: OptimizerInputs, primary) -> str:
    """Im Auto-Betrieb die fuer den aktuellen Tag gewaehlte Strategie liefern."""
    configured = str(config.optimization.charge_strategy)
    if configured in ("asap", "peak", "late"):
        return configured
    table = getattr(primary, "table", pd.DataFrame())
    if table.empty or "export_line_w" not in table or not len(inputs.index):
        return "asap"
    local_index = pd.DatetimeIndex(table.index).tz_convert(
        config.general.timezone)
    current_day = pd.Timestamp(inputs.index[0]).tz_convert(
        config.general.timezone).date()
    today = pd.to_numeric(
        table.loc[local_index.date == current_day, "export_line_w"],
        errors="coerce")
    return "peak" if today.notna().any() else "asap"


def _start_shadow_comparison(config, inputs: OptimizerInputs,
                             primary, primary_violations) -> None:
    """Die drei realen Ladestrategien im Hintergrund vergleichen."""
    if not getattr(config.monitoring, "shadow_compare_enabled", True):
        return
    configured = str(config.optimization.charge_strategy)
    active = _selected_comparison_mode(config, inputs, primary)
    with _runtime_lock:
        if _shadow_status.get("state") in ("queued", "running"):
            log.info("Automatischer Modusvergleich läuft noch; dieser Zyklus wird gebündelt.")
            return
        _shadow_status.update(
            state="queued", started_at=None, finished_at=None,
            generated=pd.Timestamp(inputs.index[0]).isoformat(),
            message="Drei Strategien werden verglichen", result=None)

    def worker():
        try:
            with _runtime_lock:
                _shadow_status.update(state="running", started_at=_runtime_iso(),
                                      message="Schattenvergleich läuft")
            rows = {}
            curves = {}
            late_result = None
            for strategy in ("asap", "peak", "late"):
                # Nur ein explizit produktiv gewählter Modus ist exakt derselbe
                # Plan. Im Auto-Betrieb werden alle drei Strategien separat
                # gerechnet; Auto selbst ist keine vierte Strategie.
                if configured == strategy:
                    result, violations = primary, primary_violations
                else:
                    cfg = copy.deepcopy(config)
                    cfg.optimization.charge_strategy = strategy
                    result = Optimizer(
                        cfg, stabilize_plan=False, store_warm=False).solve(inputs)
                    violations = validate_plan(cfg, result, inputs)
                metrics = _comparison_metrics_complete(
                    result.table, result.total_cost_ct,
                    config.general.slot_minutes)
                metrics.update(
                    status=result.status,
                    solver_seconds=round(result.solver_seconds, 2),
                    errors=sum(v.severity == "error" for v in violations),
                    warnings=sum(v.severity == "warning" for v in violations),
                    active=strategy == active)
                rows[strategy] = metrics
                curves[strategy] = _comparison_series(result.table)
                if strategy == "late":
                    late_result = result
            valid = {name: value for name, value in rows.items()
                     if not value["errors"] and value["status"] == "Optimal"}
            best = min(valid, key=lambda name: (
                valid[name]["cost_eur"], valid[name]["grid_import_kwh"],
                valid[name]["peak_export_w"])) if valid else active
            saving = round(rows[active]["cost_eur"] - rows[best]["cost_eur"], 2)
            threshold = max(0.0, float(getattr(
                config.monitoring, "shadow_recommend_min_savings_eur", 0.05)))
            recommended = best if saving >= threshold else active
            payload = {
                "active": active, "configured_mode": configured,
                "recommended": recommended,
                "best_raw": best, "saving_eur": max(0.0, saving),
                "modes": rows,
                "late_confidence": _late_confidence(
                    config, inputs, late_result) if late_result is not None else None,
            }
            with _runtime_lock:
                _shadow_curves.update(
                    generated=pd.Timestamp(inputs.index[0]).isoformat(),
                    series={"timestamp": [stamp.isoformat()
                                          for stamp in inputs.index],
                            # PV-Prognose als gemeinsame Kontext-Kurve (für alle
                            # Modi gleich) -> macht den Modusvergleich lesbarer.
                            "pv_w": [round(float(v), 1) for v in inputs.pv_w],
                            "modes": curves})
                _shadow_status.update(
                    state="done", finished_at=_runtime_iso(), result=payload,
                    message=(f"Empfehlung: {recommended}"
                             + (f" (−{saving:.2f} €)" if recommended != active else
                                " (aktuellen Modus beibehalten)")))
            if recommended != _last_shadow_recommendation.get("value"):
                _last_shadow_recommendation["value"] = recommended
                _record_dashboard_event(
                    config, "shadow_comparison",
                    f"Modusvergleich: Empfehlung {recommended}"
                    + ("" if recommended == active
                       else f" statt {active} (−{max(0.0, saving):.2f} €)"),
                    details={"active": active, "configured_mode": configured,
                             "recommended": recommended,
                             "saving_eur": max(0.0, saving)})
        except SolverCancelled:
            # Dienstende/Neustart bricht den Solve ab - kein Fehler; der
            # naechste Prozess vergleicht beim ersten Zyklus neu.
            # Erst loggen, dann den Status setzen: wer auf den Status wartet,
            # findet die Meldung dann sicher vor.
            log.info("Automatischer Modusvergleich wegen Dienstende abgebrochen.")
            with _runtime_lock:
                _shadow_status.update(state="cancelled", finished_at=_runtime_iso(),
                                      message="Schattenvergleich wegen Dienstende abgebrochen")
        except Exception as exc:
            with _runtime_lock:
                _shadow_status.update(state="error", finished_at=_runtime_iso(),
                                      message=f"Schattenvergleich fehlgeschlagen: {exc}")
            log.exception("Automatischer Modusvergleich fehlgeschlagen")

    threading.Thread(target=worker, name="ems-shadow-comparison", daemon=True).start()


class _RecalcGate:
    """Entprellung des Auto-Recalc (config.recalc).

    - geprueft wird hoechstens alle ``check_seconds`` (vorher: im 5-s-Takt der
      Live-Stichprobe, die Einstellung war wirkungslos),
    - erst ZWEI aufeinanderfolgende Ueberschreitungen loesen aus (eine einzelne
      Probe faengt den Akku mitten im Nachregeln),
    - danach ruht die Pruefung fuer eine Sperrzeit, damit nicht Recalc an
      Recalc folgt, solange die Abweichung anhaelt."""

    HITS_REQUIRED = 2

    def __init__(self, check_seconds: float, lockout_seconds: float = None):
        self.check_s = max(10.0, float(check_seconds))
        self.lockout_s = (max(300.0, 3.0 * self.check_s)
                          if lockout_seconds is None else float(lockout_seconds))
        self._last_check = None
        self._hits = 0
        self._blocked_until = 0.0

    def due(self, now: float) -> bool:
        if now < self._blocked_until:
            return False
        return self._last_check is None or now - self._last_check >= self.check_s

    def observe(self, delta, now: float):
        """Ergebnis einer faelligen Pruefung melden. Rueckgabe: Abweichung,
        wenn jetzt neu gerechnet werden soll, sonst None."""
        self._last_check = now
        if delta is None:
            self._hits = 0
            return None
        self._hits += 1
        if self._hits < self.HITS_REQUIRED:
            return None
        self._hits = 0
        self._blocked_until = now + self.lockout_s
        return delta


def _live_recalc_needed(config, e3dc, live=None):
    """Weicht die gemessene Netzleistung im laufenden Slot stark vom Sollwert ab?
    Rückgabe: Abweichung (W) bei Überschreitung, sonst None. Netz ist hier das
    richtige Signal: es bündelt PV- UND Last-Prognosefehler (Netz = Last - PV +
    Akku) - genau die 'die Situation hat sich geändert'-Fälle, die eine sofortige
    Neuplanung rechtfertigen."""
    rc = getattr(config, "recalc", None)
    if not rc or not rc.enabled or e3dc is None:
        return None
    try:
        from .local_history import read_execution_plan_slot
        slot = floor_time(pd.Timestamp.now(tz=config.general.timezone),
                          f"{config.general.slot_minutes}min")
        planned = read_execution_plan_slot(config.e3dc_rscp.history_db_path, slot)
        if not planned or planned.get("grid_w") is None:
            return None
        live = live or e3dc.read_live(force=True)
        if not live or live.get("grid_w") is None:
            return None
        delta = float(live["grid_w"]) - float(planned["grid_w"])
        return delta if abs(delta) > rc.deviation_w else None
    except Exception as exc:  # darf den Wartezyklus nie stören
        log.debug("Live-Recalc-Prüfung fehlgeschlagen (%s).", exc)
        return None


def _publish_execution_alarm(publisher, config, audit) -> None:
    if publisher is None or not audit:
        return
    from .local_history import read_execution_audits
    required = max(1, config.monitoring.execution_alert_consecutive)
    recent = read_execution_audits(
        config.e3dc_rscp.history_db_path, max(20, required * 5))
    # Vorläufige Live-Prüfungen besitzen einen eigenen Alarm mit Hysterese.
    # Hier dürfen ausschließlich die später bestätigten Zählerprüfungen zählen.
    confirmed = [item for item in recent
                 if not str(item.get("state", "")).startswith("live_")
                 and item.get("state") not in ("startup", "data_waiting")]
    consecutive = (len(confirmed) >= required
                   and all(not item["ok"] for item in confirmed[:required]))
    if consecutive and not _execution_alarm["failed"]:
        publisher.publish_alert(
            "warning", "EMS-Ausführung weicht wiederholt vom Plan ab: "
            + audit["message"])
        _execution_alarm["failed"] = True
    elif (_execution_alarm["failed"] and confirmed and confirmed[0]["ok"]
          and audit.get("ok")
          and audit.get("state") not in ("startup", "data_waiting")
          and not str(audit.get("state", "")).startswith("live_")):
        # Warte-/Start-Audits tragen ok=True, sagen aber nichts ueber die
        # Ausfuehrung - sie duerfen den Alarm nicht entwarnen (Flattern).
        publisher.publish_alert("info", "EMS-Ausführung folgt wieder dem Sollplan.")
        _execution_alarm["failed"] = False


def _publish_solver_alarm(publisher, status) -> None:
    if publisher is None or not status:
        return
    if status.get("slow") and not _solver_alarm["failed"]:
        typical = status.get("median_seconds")
        suffix = (f" statt typisch {typical:.1f} s" if typical is not None else "")
        publisher.publish_alert(
            "warning", f"Solver ungewöhnlich langsam: "
            f"{status['seconds']:.1f} s{suffix}.")
        _solver_alarm["failed"] = True
    elif not status.get("slow") and _solver_alarm["failed"]:
        publisher.publish_alert("info", "Solver-Laufzeit wieder normal.")
        _solver_alarm["failed"] = False


def _publish_load_feedback_alarm(publisher, statuses) -> None:
    if publisher is None:
        return
    if (hasattr(publisher, "feedback_alarm_ready") and
            not publisher.feedback_alarm_ready()):
        return
    bad = {item["label"] for item in (statuses or [])
           if item.get("required") and not item.get("fresh")}
    old = set(_load_feedback_alarm["failed"])
    for label in sorted(bad - old):
        publisher.publish_alert(
            "warning", f"Last-Rückmeldung fehlt oder ist veraltet: {label}.")
    for label in sorted(old - bad):
        publisher.publish_alert("info", f"Last-Rückmeldung wieder aktuell: {label}.")
    _load_feedback_alarm["failed"] = bad


def _publish_control_alarm(publisher, config, status) -> None:
    """Steuerfehler sofort, danach gedrosselt; Erholung genau einmal melden."""
    if publisher is None or not status:
        return
    failed = status.get("ok") is False
    now = _time.time()
    if failed:
        key = f"{status.get('state')}:{status.get('message')}"
        repeat_s = max(
            60.0, config.e3dc_rscp.control_alarm_repeat_minutes * 60.0)
        if (not _control_alarm["failed"] or _control_alarm["key"] != key
                or now - _control_alarm["last"] >= repeat_s):
            publisher.publish_alert(
                "error", f"E3DC-Steuer-Ausfall: {status.get('message')}")
            _control_alarm.update(failed=True, key=key, last=now)
    elif status.get("ok") is True and _control_alarm["failed"]:
        publisher.publish_alert(
            "info", "E3DC-Steuerung wieder bestätigt und funktionsfähig.")
        _control_alarm.update(failed=False, key=None, last=now)


def _store_control_status(config, status) -> None:
    if not status:
        return
    try:
        from .local_history import write_control_verification
        write_control_verification(config.e3dc_rscp.history_db_path, status)
    except Exception as exc:  # Diagnose darf die Steuerung nicht beeinträchtigen
        log.warning("E3DC-Steuerstatus nicht speicherbar (%s).", exc)


def _apply_curtailment_and_control(e3dc, cmd) -> dict:
    """PV-Abregelung + Akku-Steuerung eines Slots anwenden und zu EINEM
    Steuerstatus zusammenführen. Scheitert die Abregelung, wird der Akku-Befehl
    trotzdem ausgeführt, der nicht umgesetzte PV-Anteil aber als Ausfall gemeldet
    (Akku-Status unter 'battery_control' eingebettet); sonst hängt die
    Abregel-Quittung als 'curtailment' am Akku-Status. (Zyklus + Startup-Reapply
    teilen sich diese Logik.)"""
    curtailment = e3dc.apply_pv_curtailment(cmd)
    control = e3dc.apply_control(cmd)
    if curtailment.get("ok") is False:
        failed = dict(curtailment)
        failed["battery_control"] = control
        return failed
    control["curtailment"] = curtailment
    return control


def _run_whatif(overrides: dict, snapshot=None) -> dict:
    """What-if-Simulation: die Eingaben des letzten Produktivlaufs mit geänderten
    Parametern neu rechnen, OHNE zu persistieren/steuern. overrides:
    mode (asap|peak|late|auto), price_factor (0.1..5.0, skaliert Bezugs- und
    Spotpreis). Rückgabe: kompakte Plan-Kennzahlen. Wirft ValueError, wenn noch
    kein Produktivlauf vorliegt."""
    import dataclasses as _dc
    snap = snapshot if snapshot is not None else _last_solve_snapshot
    inp, base_cfg = snap.get("inputs"), snap.get("config")
    if inp is None or base_cfg is None:
        raise ValueError("Noch kein Produktivlauf – Simulation erst nach dem "
                         "ersten Zyklus möglich.")
    cfg = copy.deepcopy(base_cfg)
    mode = str(overrides.get("mode", "")).lower()
    if mode in ("asap", "peak", "late", "auto"):
        cfg.optimization.charge_strategy = mode
    factor = min(5.0, max(0.1, float(overrides.get("price_factor", 1.0) or 1.0)))
    new_inp = _dc.replace(
        inp, price_ct_kwh=np.asarray(inp.price_ct_kwh, dtype=float) * factor,
        spot_price_ct_kwh=(np.asarray(inp.spot_price_ct_kwh, dtype=float) * factor
                           if inp.spot_price_ct_kwh is not None else None))
    result = Optimizer(cfg, store_warm=False).solve(new_inp)
    tbl = result.table
    dt = base_cfg.general.dt_hours
    return {
        "mode": cfg.optimization.charge_strategy,
        "price_factor": round(factor, 3),
        "infeasible": bool(result.infeasible),
        "status": result.status,
        "total_cost_eur": round(float(result.total_cost_ct) / 100.0, 2),
        "grid_import_kwh": round(float(tbl["grid_import_w"].sum()) * dt / 1000.0, 1),
        "grid_export_kwh": round(float(tbl["grid_export_w"].sum()) * dt / 1000.0, 1),
        "end_soc_percent": (round(float(tbl["house_soc_percent"].iloc[-1]), 1)
                            if "house_soc_percent" in tbl and len(tbl) else None),
        "auto_peak_basis": result.auto_peak_basis,
    }


def _maybe_snapshot_forecast_accuracy(config) -> None:
    """Einmal täglich die 7-Tage-Prognosegüte als Trend-Punkt sichern (für das
    Dashboard-Trend-Panel). Günstig gated: nur rechnen, wenn für heute noch kein
    Eintrag existiert – der teure compare_sources läuft so max. 1×/Tag."""
    try:
        from .local_history import (latest_forecast_accuracy_day,
                                     write_forecast_accuracy)
        from .observability import forecast_accuracy
        db = config.e3dc_rscp.history_db_path
        today = pd.Timestamp.now(tz=config.general.timezone).strftime("%Y-%m-%d")
        if latest_forecast_accuracy_day(db) == today:
            return
        write_forecast_accuracy(db, today, forecast_accuracy(config, days=7))
    except Exception as exc:  # darf den Zyklus nie stören
        log.debug("Prognosegüte-Snapshot fehlgeschlagen: %s", exc)


def _dispatch_control(action, payload, *, config, publisher, e3dc, config_path,
                      schedule_runner, find_load, load_params):
    """Dashboard-Steuer-Aktion ausführen (Laufzeit) und Lasten-/Modus-Änderungen
    ins Overlay (config_overrides.yaml) persistieren.

    Modulweit und dependency-injected, damit der HTTP-Handler eine dünne Hülle
    bleibt und der Dispatcher direkt testbar ist. Wirft KeyError bei unbekannter
    Aktion und ValueError bei ungültiger Eingabe (vom Handler zu 404/400)."""
    from .config import save_override
    from .loads import _slug as _lslug
    from .homey_mqtt import _slug as _hslug
    if action == "recalc":
        fresh, status = _request_runtime_recalc()
        if publisher is not None:
            publisher.recalc_event.set()
        return {"queued": fresh, "runtime": status}
    if action == "e3dc_control":
        if e3dc is None:
            raise ValueError("Keine RSCP-Verbindung – E3/DC-Steuerung nicht schaltbar")
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled muss true oder false sein")
        result = e3dc.set_control_enabled(enabled)
        # set_control_enabled setzt bereits self.rc.control_enabled, und self.rc
        # IST config.e3dc_rscp (dasselbe Objekt, rscp.py __init__). Die explizite
        # Zuweisung ist daher nur ein Sicherheitsnetz (Absicht = enabled). Die
        # BESTÄTIGTE Gerätelage kommt separat über control_status (Alarm/Kachel).
        config.e3dc_rscp.control_enabled = enabled
        save_override(config_path, "e3dc_rscp.control_enabled", enabled)
        status = result.get("control_status")
        _store_control_status(config, status)
        _publish_control_alarm(publisher, config, status)
        _request_runtime_recalc()
        if publisher is not None:
            publisher.recalc_event.set()
        return result
    if action == "load":
        name = str(payload.get("name", ""))
        ld = find_load(name)
        slug = _lslug(name)          # Overlay-/Spalten-Konvention (config_overrides)
        changed = {}
        if "enabled" in payload:
            en = bool(payload["enabled"])
            ld.enabled = en
            # apply_load_overrides überschreibt ld.enabled JEDEN Zyklus aus
            # publisher.load_overrides[homey-slug] – daher MUSS hier der
            # homey-slug (kleingeschrieben) genutzt werden, sonst greift die
            # Änderung nicht (der direkte ld.enabled-Set würde überschrieben).
            if publisher is not None:
                publisher.load_overrides[_hslug(name)] = en
            save_override(config_path,
                          f"controllable_loads_overrides.{slug}.enabled", en)
            changed["enabled"] = en
        allowed = load_params.get(ld.type, {})
        for key, val in (payload.get("params") or {}).items():
            if key not in allowed:
                raise ValueError(f"Parameter {key!r} für {ld.type} nicht erlaubt")
            cast = allowed[key](val)
            setattr(ld, key, cast)
            save_override(config_path,
                          f"controllable_loads_overrides.{slug}.{key}", cast)
            changed[key] = cast
        if publisher is not None:
            _request_runtime_recalc()
            publisher.recalc_event.set()
        return changed
    if action == "mode":
        strat = str(payload.get("strategy", "")).lower()
        if strat not in ("asap", "peak", "late", "auto"):
            raise ValueError("strategy muss asap|peak|late|auto sein")
        config.optimization.charge_strategy = strat
        save_override(config_path, "optimization.charge_strategy", strat)
        if publisher is not None:
            _request_runtime_recalc()
            publisher.recalc_event.set()
        return {"charge_strategy": strat}
    if action == "battery":
        if e3dc is None:
            raise ValueError("Keine RSCP-Verbindung – manuelles Laden nicht möglich")
        act = str(payload.get("action", "")).lower()
        if not config.e3dc_rscp.control_enabled and act != "auto":
            raise ValueError("E3/DC-Steuerung ist ausgeschaltet")
        watts = float(payload.get("watts", 0) or 0)
        minutes = float(payload.get("minutes", 15) or 15)
        if not (np.isfinite(watts) and watts >= 0.0):
            raise ValueError("watts muss eine Zahl >= 0 sein")
        # Ohne gueltige Dauer entstand frueher ein UNBEFRISTETER Eingriff.
        if not (np.isfinite(minutes) and 1.0 <= minutes <= 24 * 60):
            raise ValueError("minutes muss zwischen 1 und 1440 liegen")
        return e3dc.manual_power(act, watts, minutes * 60.0)
    if action == "battery_schedule":
        if schedule_runner is None:
            raise ValueError("Akku-Zeitplanung ist nicht verfügbar")
        op = str(payload.get("op", "add")).lower()
        if op == "add":
            return schedule_runner.add(payload)
        if op == "cancel":
            return schedule_runner.cancel(int(payload.get("id", 0)))
        if op == "delete":
            return schedule_runner.delete(int(payload.get("id", 0)))
        raise ValueError("Unbekannte Planungsaktion")
    raise KeyError(action)


def _status_api_payload(path: str, config, query=None):
    """Payload + HTTP-Code der reinen Status-JSON-Endpoints (status/mode-
    comparison/events). Modulweit und ohne Live-/Datei-IO, damit das Routing der
    Handler-GET direkt testbar ist. None, wenn der Pfad kein Status-Endpoint ist."""
    if path == "/api/status.json":
        status = _runtime_snapshot()
        status["solver_running"] = solver_is_running()
        return status, 200
    if path == "/api/mode-comparison.json":
        with _runtime_lock:
            curves = {"generated": _shadow_curves.get("generated"),
                      "series": _shadow_curves.get("series")}
        if curves["series"] is None:
            return {"status": "unavailable"}, 503
        return curves, 200
    if path == "/api/events.json":
        from .local_history import read_dashboard_events
        return {"events": read_dashboard_events(
            config.e3dc_rscp.history_db_path, config.general.timezone, 50)}, 200
    if path == "/api/savings-history.json":
        from .observability import savings_drivers, savings_over_time
        payload = savings_over_time(config.e3dc_rscp.history_db_path)
        payload["drivers"] = savings_drivers(config, days=30)
        return payload, 200
    if path == "/api/forecast-accuracy.json":
        from .local_history import read_forecast_accuracy
        from .observability import forecast_accuracy
        return {"7d": forecast_accuracy(config, days=7),
                "30d": forecast_accuracy(config, days=30),
                "trend": read_forecast_accuracy(
                    config.e3dc_rscp.history_db_path, days=30)}, 200
    if path == "/api/forecast-analysis.json":
        from .observability import forecast_analysis
        day = ((query or {}).get("day") or [None])[0]
        return forecast_analysis(config, days=30, target_day=day), 200
    if path == "/api/battery-health.json":
        from .observability import battery_health
        return battery_health(config, days=30), 200
    if path == "/api/load-profiles.json":
        from .load_learning import APPLY_MIN_RUNS, learn_profile
        from .load_learning import _feedback_series as _fb
        tz = config.general.timezone
        now = pd.Timestamp.now(tz=tz)
        rows = []
        for ld in (getattr(config, "controllable_loads", []) or []):
            if ld.type != "deferrable":
                continue
            item = {"name": ld.name, "learned": None,
                    "configured_slots": len(ld.power_profile_w or []),
                    "in_use": bool(ld.power_profile_w)}
            try:
                power = _fb(config, ld, now - pd.Timedelta(days=30), now)
                profile = learn_profile(power, config.general.dt_hours) \
                    if power is not None and not power.empty else None
            except Exception as exc:            # pragma: no cover
                log.debug("Lastprofil %s nicht lesbar (%s).", ld.name, exc)
                profile = None
            if profile is None:
                item["reason"] = ("keine Rückmeldung (power_topic?)"
                                  if not ld.power_topic else "noch kein Lauf")
            else:
                item["learned"] = {
                    "power_w": profile.power_w,
                    "runtime_minutes": profile.runtime_minutes,
                    "n_runs": profile.n_runs,
                    "energy_kwh": profile.energy_kwh,
                    "peak_w": profile.peak_w,
                    "usable": profile.usable}
                if not profile.usable:
                    item["reason"] = f"erst ab {APPLY_MIN_RUNS} Läufen"
            rows.append(item)
        return {"loads": rows, "min_runs": APPLY_MIN_RUNS}, 200
    if path == "/api/plan-value.json":
        # Timing-Güte ist reines Nachrechnen; der Regret kostet je Tag zwei
        # Solverläufe (~2-3 s), deshalb nur wenige Tage und 6 h Prozess-Cache.
        # Drei Tage, weil der jüngste (gestern) noch keinen abgeschlossenen
        # Folgetag als Solver-Horizont hat und deshalb entfällt.
        from .planvalue import plan_value_summary
        return plan_value_summary(config, timing_days=7, regret_days=3), 200
    return None


CONTROL_COLS = [
    # E3DC-Steuerbefehle (Limits nur bei Abweichung vom Eigenverbrauch):
    "batt_charge_limit_w", "batt_discharge_limit_w",
    "batt_grid_charge_w", "batt_grid_discharge_w",
    "charge_limited", "discharge_limited",
    # rohe Optimierer-Leistungen (Referenz/Analyse):
    "batt_dc_charge_w", "batt_ac_charge_w", "batt_discharge_w",
    "car_charge_w", "grid_import_w", "grid_export_w", "pv_w", "pv_curtail_w",
]


def _house_history_refresh_window(now, last, slot_minutes: int,
                                  settle_minutes: int,
                                  overlap_hours: int):
    """Sicheres RSCP-Historienfenster: gereift, überlappend, max. drei Tage."""
    freq = pd.Timedelta(minutes=slot_minutes)
    now = pd.Timestamp(now)
    safe_end = floor_time(now - pd.Timedelta(minutes=settle_minutes), freq)
    if last is None:
        start = safe_end - timedelta(days=1)
    else:
        start = min(pd.Timestamp(last) + freq,
                    safe_end - pd.Timedelta(hours=overlap_hours))
    return max(start, safe_end - timedelta(days=3)), safe_end


def _complete_operational_series(series, index, fallback,
                                 interpolate_limit: int = 0,
                                 edge_limit: int = 0):
    """Zeitreihe sicher aufs Betriebsraster bringen, ohne lange Lücken zu tarnen.

    Kurze interne Lücken dürfen optional interpoliert werden; Randwerte werden
    nur begrenzt gehalten. Alles danach Fehlende erhält einen expliziten,
    konservativen Fallback. Rückgabe zusätzlich: Zahl der Fallback-Slots.
    """
    s = (pd.Series(dtype="float64") if series is None else pd.Series(series))
    out = s.reindex(index).astype(float)
    if interpolate_limit > 0 and out.notna().any():
        out = out.interpolate(method="time", limit=interpolate_limit,
                              limit_area="inside")
    if edge_limit > 0:
        out = out.ffill(limit=edge_limit).bfill(limit=edge_limit)
    missing = out.isna()
    if missing.any():
        if isinstance(fallback, pd.Series):
            out = out.fillna(fallback.reindex(index))
        else:
            out = out.fillna(float(fallback))
    return out, int(missing.sum())


def _signed_plan_total(frame: pd.DataFrame, positive: tuple[str, ...],
                       negative: tuple[str, ...]) -> pd.Series:
    """Plan-Komponenten vorzeichenrichtig summieren, ohne Lücken zu 0 zu machen."""
    parts = []
    for column in positive + negative:
        values = (pd.to_numeric(frame[column], errors="coerce")
                  if column in frame.columns else
                  pd.Series(float("nan"), index=frame.index))
        parts.append(-values if column in negative else values)
    return pd.concat(parts, axis=1).sum(axis=1, min_count=1)


def _settle_actual_slots(config, now) -> int:
    """Leistungs-Istwerte in ``actuals`` auf die 5-s-Mittel nachziehen.

    Gegenstueck zu ``_overlay_live_slot_actuals``: das Dashboard legt die
    Slotmittel nur fuer die Anzeige darueber, hier wandern sie dauerhaft in die
    Tabelle. Davon leben alle, die ``read_actual`` direkt lesen - Lauf-Archiv,
    Akku-Kalibrierung, Drift-Monitor, Fehler-Heatmaps, Planwert.

    Der LAUFENDE Slot ist ausdruecklich dabei. Sein Mittel ist zwar erst ein
    Teilmittel, aber es schaetzt das Slotmittel besser als ein einzelner
    Momentanwert vom Slotanfang - auf einer Flanke liegt der Einzelpunkt
    systematisch daneben, das Teilmittel nur unvollstaendig. Zu frueh im Slot
    passiert nichts: ``read_live_slot_averages`` gibt einen Slot erst ab
    ``min_coverage_seconds`` heraus, und mit jedem Zyklus reift der Wert nach,
    bis der Slot vorbei ist.

    Der Blick zurueck geht ueber eine Stunde, damit ein Neustart oder ein
    ausgefallener Zyklus die Luecke beim naechsten Mal von selbst schliesst.
    """
    from .local_history import read_live_slot_averages, settle_actual_slots

    slot = pd.Timedelta(minutes=config.general.slot_minutes)
    current = floor_time(now, f"{config.general.slot_minutes}min")
    averages = read_live_slot_averages(
        config.e3dc_rscp.history_db_path, current - pd.Timedelta(hours=1),
        current + slot, config.general.timezone, config.general.slot_minutes,
        config.forecast.live_nowcast_min_coverage_seconds,
        config.forecast.live_nowcast_max_gap_seconds)
    if averages is None or averages.empty:
        return 0
    return settle_actual_slots(config.e3dc_rscp.history_db_path, averages)


def _overlay_live_slot_actuals(frame: pd.DataFrame,
                               live_slots: pd.DataFrame,
                               past_mask) -> pd.DataFrame:
    """Leistungs-Istwerte durch echte Mittelwerte ihres Slots ersetzen.

    ``actuals`` enthält einen Momentanwert am Beginn eines Rechenlaufs. Direkt
    an einer Slotgrenze beschreibt dieser Messpunkt häufig noch die Wirkung des
    vorherigen Steuer-Slots. Die 5-s-Livehistorie wird dagegen innerhalb jedes
    Slots zeitgewichtet integriert und hat damit die richtige Zeitbedeutung.
    Zustandswerte wie SoC oder Temperatur werden absichtlich nicht verändert.
    """
    if live_slots is None or live_slots.empty:
        return frame
    mapping = {
        "pv_w": "actual_pv_w",
        "house_w": "actual_load_w",
        "grid_w": "actual_grid_w",
        "battery_w": "actual_battery_w",
    }
    mask = pd.Series(past_mask, index=frame.index, dtype=bool)
    for source, target in mapping.items():
        if source not in live_slots.columns:
            continue
        averaged = pd.to_numeric(
            live_slots[source], errors="coerce").reindex(frame.index)
        averaged = averaged.where(mask)
        previous = (pd.to_numeric(frame[target], errors="coerce")
                    if target in frame.columns else
                    pd.Series(float("nan"), index=frame.index))
        frame[target] = averaged.combine_first(previous).where(mask)
    return frame


PREDICTION_COLS = [
    "house_soc_wh", "house_soc_percent", "car_soc_wh", "car_soc_percent",
    "slot_cost_ct", "price_ct_kwh", "feedin_ct_kwh", "pv_w", "house_load_w",
]


def _now_slot(config: Config) -> pd.Timestamp:
    freq = f"{config.general.slot_minutes}min"
    return floor_time(pd.Timestamp.now(tz=config.general.timezone), freq)


def _anchor_controlled_w(config, load_feedback_status, stage_power) -> float:
    """Leistung (W), die der Slot-0-Anker aus der gemessenen Hauslast nimmt.

    Nur Lasten, deren Schaltkanal das EMS besitzt (``ems_schaltet_last``) -
    dasselbe Kriterium wie in Training und Bewertung der Grundlast. Alles
    andere (z. B. Waschmaschinen ohne control_topic) GEHOERT zur Grundlast.
    Ohne den Filter zog der Anker die seit 13.11.2026 eingefrorene Rueckmeldung
    von "Waschmaschine 1" (294 W, dauerhaft "an") ab: der laufende Slot
    rechnete mit rund 290 W zu wenig Hauslast."""
    from .local_history import ems_schaltet_last
    geschaltet = {ld.name for ld in getattr(config, "controllable_loads", [])
                  if ems_schaltet_last(ld)}
    total = 0.0
    for item in load_feedback_status or []:
        if item.get("name") not in geschaltet:
            continue
        if not item.get("fresh") or not item.get("on"):
            continue
        measured = item.get("power_w")
        total += (max(0.0, float(measured)) if measured is not None
                  else stage_power.get(item.get("label"), 0.0))
    return total


SLOT_SOC_MIN_ELAPSED_S = 120.0


def _soc_at_slot_start(soc_wh: float, battery_w, elapsed_s: float,
                       capacity_wh: float, slot_s: float) -> float:
    """Mess-SoC (Wh) auf den Beginn des laufenden Slots zurueckrechnen.

    battery_w > 0 = Laden. Naeherung mit der aktuellen Akkuleistung ueber die
    verstrichene Slotzeit; unter ``SLOT_SOC_MIN_ELAPSED_S`` (regulaerer Lauf
    kurz nach der Marke) und ohne gueltige Leistung bleibt der Wert
    unveraendert. Ergebnis auf 0..Kapazitaet begrenzt."""
    try:
        power = float(battery_w)
        elapsed = float(elapsed_s)
    except (TypeError, ValueError):
        return soc_wh
    if not (np.isfinite(power) and np.isfinite(elapsed)):
        return soc_wh
    if elapsed < SLOT_SOC_MIN_ELAPSED_S:
        return soc_wh
    elapsed = min(elapsed, float(slot_s))
    start = soc_wh - power * elapsed / 3600.0
    return float(min(max(start, 0.0), float(capacity_wh)))


def _control_slot(config: Config, plan_start) -> pd.Timestamp:
    """Der Slot, der beim ANWENDEN des Befehls gilt - Wanduhr, nicht Zyklusbeginn.

    Ein Solve, der ueber die Slotgrenze laeuft, wuerde sonst den ABGELAUFENEN
    Slot kommandieren und den laufenden ueberspringen. Beobachtet am 17.08.2026:
    Zyklusbeginn 14:27:58, Solver 351,9 s, Veroeffentlichung 14:33:51 mit dem
    Sollwert fuer 14:15 - der 14:30-Slot mit 9,7 kW geplantem Netzladen bekam
    nie einen Befehl, und der Watchdog hielt bis 14:45 den alten Stand.
    """
    jetzt = _now_slot(config)
    return jetzt if jetzt > pd.Timestamp(plan_start) else pd.Timestamp(plan_start)


def _optimization_index(config: Config, now) -> pd.DatetimeIndex:
    """Konfigurierten Horizont, optional bis zur nächsten Mitternacht."""
    start = pd.Timestamp(now)
    freq = f"{config.general.slot_minutes}min"
    if not config.general.optimization_horizon_round_to_midnight:
        return pd.date_range(
            start, periods=config.general.n_opt_slots, freq=freq,
            tz=config.general.timezone)

    raw_end = start + pd.Timedelta(
        hours=config.general.optimization_horizon_hours)
    rounded_end = raw_end.normalize()
    # Immer auf die Mitternacht NACH dem regulären Ende. Damit endet jeder Lauf
    # eines Tages am selben Zeitpunkt - auch der um 00:00. Vorher wurde ein
    # bereits auf Mitternacht fallendes Ende nicht aufgerundet; dann sah der
    # 00:00-Lauf mit 48 h einen ganzen Tag WENIGER als der Lauf 15 Minuten
    # später (71,75 h) - genau der Tag, für den er den Akku einteilen soll.
    if rounded_end <= raw_end:
        rounded_end += pd.DateOffset(days=1)
    return pd.date_range(start, rounded_end, freq=freq,
                         tz=config.general.timezone, inclusive="left")


def run_once(config: Config, publisher: HomeyMqttPublisher | None = None,
             e3dc=None) -> None:
    """Ein Rechenzyklus. `publisher`/`e3dc`: persistente Verbindungen im Loop-
    Betrieb (MQTT Last Will bzw. RSCP-Watchdog-Thread); ohne werden sie pro Lauf
    erzeugt und wieder geschlossen."""
    global _last_efficiency_check, _last_load_bias, _last_monitor_diagnostics
    runtime_started = _time.monotonic()
    runtime_trigger = _runtime_begin()
    repo = None
    control_status = None
    execution_status = None
    solver_status = None
    pv_selection = None
    plan_published = False
    one_shot = publisher is None
    # Optionale direkte E3DC-Anbindung (RSCP); im Loop von main() übergeben,
    # sonst pro Lauf erzeugen (own_e3dc -> am Ende schließen).
    own_e3dc = e3dc is None
    if own_e3dc and config.e3dc_rscp.enabled:
        try:
            from .rscp import E3DCLink
            e3dc = E3DCLink(config)
            if publisher is not None:   # Einzellauf ohne MQTT
                e3dc.set_alarm_callback(publisher.publish_alert)
        except Exception as exc:
            log.warning("RSCP-Anbindung nicht verfügbar (%s).", exc)
    try:
        # Repository-Init in den try ziehen: schlägt es fehl, muss der
        # Runtime-Status auf "error" fallen (via except -> _runtime_fail) und darf
        # NICHT auf "running" hängen bleiben (Dashboard zeigte sonst ewig "läuft").
        repo = make_repository(config)
        try:
            _reload_calibration_overrides(config)
        except Exception as exc:
            # Der Dienst hat eine gueltige Config im Speicher. Eine gerade halb
            # editierte config.yaml oder ein unpassender Overlay-Wert darf
            # nicht jeden Zyklus zu Fall bringen - weiter mit dem alten Stand,
            # der naechste Zyklus versucht es erneut.
            log.warning("Kalibrier-/Overlay-Werte nicht neu geladen (%s) - "
                        "weiter mit dem bisherigen Stand.", exc)
        now = _now_slot(config)
        freq = f"{config.general.slot_minutes}min"
        # Per MQTT gesetzte Overrides (ems/cmd/car_departure_time,
        # car_target_soc, min_soc, max_soc) für diesen Lauf anwenden.
        if publisher is not None:
            publisher.apply_vehicle_overrides(config.vehicle)
            publisher.apply_battery_overrides(config.house_battery)
            publisher.apply_load_overrides(config.controllable_loads)
        # Rückmeldungen vor der Lastprognose archivieren: damit können die
        # steuerbaren Stufen bereits aus Grundlast und Live-Nowcast entfernt
        # werden. Dieselben Werte gehen später unverändert in den Optimierer.
        load_feedback, load_feedback_status = _read_load_feedback(
            config, publisher, now)
        load_run_state = _read_load_run_state(config, now)
        grid_charge_run_wh = _read_grid_charge_run_wh(config, now)
        # Konfigurierten Horizont modellieren; optional kontrolliert bis zur
        # nächsten Mitternacht erweitern. Ohne Option bleiben es konstant 48 h.
        opt_index = _optimization_index(config, now)
        opt_end = opt_index[-1] + pd.Timedelta(freq)

        # --- 0) Lokale Hauslast-Historie aus dem E3DC nachführen --------- #
        # Vor der Prognose die seit dem letzten Stand fehlenden 15-min-Fenster
        # holen (gekappt auf 3 Tage, damit run_once nie den ganzen Backfill zieht
        # - die Tiefe macht rscp_import.py im Hintergrund).
        if e3dc and config.e3dc_rscp.history_source:
            try:
                from .local_history import last_timestamp, write_house_load, count
                last = last_timestamp(config.e3dc_rscp.history_db_path)
                frm, safe_end = _house_history_refresh_window(
                    now, last, config.general.slot_minutes,
                    config.e3dc_rscp.history_settle_minutes,
                    config.e3dc_rscp.history_overlap_hours)
                data = (e3dc.read_house_load_15min(frm, safe_end)
                        if frm < safe_end else {})
                n = write_house_load(config.e3dc_rscp.history_db_path, data)
                total = count(config.e3dc_rscp.history_db_path)
                log.info("Hauslast-Historie aktualisiert: %d Fenster geschrieben "
                         "(bis %s, gesamt %d).", n, safe_end, total)
                if total == 0:
                    log.warning("Lokale Hauslast-Historie ist leer – Backfill "
                                "starten: python rscp_import.py --config config.yaml")
            except Exception as exc:
                log.warning("Hauslast-Nachführung fehlgeschlagen (%s).", exc)

        # --- 0b) Day-Ahead-Spotpreis (Energy-Charts) nachführen ---------- #
        _refresh_spot(config)

        # --- 0c) PV-Vorhersage nachführen: Solcast ODER freies pvlib-Modell -- #
        try:
            solcast.refresh(config)
        except Exception as exc:  # pragma: no cover
            log.warning("Solcast-Nachführung fehlgeschlagen (%s).", exc)
        try:
            from . import pvforecast
            pvforecast.refresh(config)
        except Exception as exc:  # pragma: no cover
            log.warning("PV-Modell-Nachführung fehlgeschlagen (%s).", exc)
        try:
            from .pv_eval import select_source
            pv_selection = select_source(config, now)
            config._pv_selected_source = pv_selection["selected"]
            log.info("PV-Quellenwahl: %s (%s).", pv_selection["selected"],
                     pv_selection["reason"])
        except Exception as exc:
            config._pv_selected_source = (
                "solcast" if config.solcast.enabled else "pvlib")
            log.warning("Automatische PV-Quellenwahl fehlgeschlagen (%s) – "
                        "nutze %s.", exc, config._pv_selected_source)

        # --- 1) Verbrauchsprognose (72 h) -------------------------------- #
        _runtime_update(phase="Prognosen erstellen", progress=30,
                        message="Last-, PV- und Preisprognosen werden aufbereitet")
        log.info("Lade Verbrauchs-Historie und erstelle Prognose ...")
        history_raw = load_history(repo, config, now)
        forecast_end = now + timedelta(hours=config.general.forecast_horizon_hours)
        temp = _read_temp(repo, config,
                          now - timedelta(days=config.forecast.lookback_days), forecast_end)
        # Solar-Einstrahlung für den Pool-Wärmeeintrag (dasselbe Fenster, derselbe
        # Open-Meteo-Cache-Refresh wie temp – kein zusätzlicher HTTP-Call).
        solar = _read_solar(config,
                            now - timedelta(days=config.forecast.lookback_days), forecast_end)
        forecaster = LoadForecaster(config)
        hist_pv = solcast.read_pv_signal(config, repo, "pv_forecast", 
                                         now - timedelta(days=config.forecast.lookback_days), now)
        fut_pv = solcast.read_pv_signal(config, repo, "pv_forecast", 
                                        now, forecast_end,
                                        require_complete=config.solcast.enabled)
        from . import load_models
        history, load_evaluation_actual, load_disaggregation = (
            load_models.disaggregate(
                config, forecaster, history_raw, now))
        load_source_missing = history_raw.dropna().empty
        load_candidates = {}
        load_base_fc = None
        load_model_note = None
        if load_source_missing:
            load_fc = pd.Series(
                config.forecast.fallback_load_w,
                index=pd.date_range(now, periods=config.general.n_forecast_slots,
                                    freq=freq, tz=config.general.timezone))
            load_base_fc = load_fc.copy()
            log.warning("Keine verwertbare Verbrauchshistorie – verwende "
                        "konservativ %.0f W.", config.forecast.fallback_load_w)
        else:
            load_candidates = load_models.candidate_forecasts(
                forecaster, history, now, config.general.n_forecast_slots,
                hist_temp=temp, fut_temp=temp, hist_pv=hist_pv, fut_pv=fut_pv)
            load_weights, temp_residual_model, _load_analysis = (
                load_models.analyze(config, now, load_evaluation_actual))
            load_base_fc = load_models.combine(
                load_candidates, load_weights, now)
            load_fc, temp_residual_delta = (
                load_models.apply_temperature_residual(
                    load_base_fc, temp, temp_residual_model,
                    config.forecast.temperature_residual_max_adjustment_percent))
            load_model_note = load_models.status_summary()
            if temp_residual_delta.abs().max() > 0.0:
                log.info(
                    "Temperatur-Residual Last: mittlere Korrektur %+.0f W, "
                    "Maximum %+.0f W.",
                    temp_residual_delta.mean(),
                    temp_residual_delta.iloc[
                        int(np.argmax(np.abs(temp_residual_delta.values)))])
            log.info("Lastmodelle: %s.", load_model_note)

        # Intraday-Korrektur: Ist/Prognose-Verhältnis der letzten Stunden auf
        # die Zukunft anwenden (abklingend) - fängt Tagesabweichungen, die das
        # Ähnliche-Tage-Modell nicht sehen kann (Besuch, Wetter).
        load_ratio, pv_ratio = _intraday_ratios(
            repo, config, forecaster, history, temp, now, hist_pv=hist_pv,
            load_actual=load_evaluation_actual)
        load_hours = _intraday_hour_ratios(config, now)
        if load_ratio is not None or load_hours:
            load_fc = load_fc * intraday_factor_series(
                load_ratio, load_fc.index, now,
                config.forecast.intraday_load_decay_hours,
                per_hour=load_hours, timezone=config.general.timezone)
            if load_hours:
                _stunde = int(pd.Timestamp(now).tz_convert(
                    config.general.timezone).hour)
                log.info("Intraday-Korrektur Last: x%.2f (roh x%.2f; klingt "
                         "über %.1f h ab) gemischt mit dem regimegleichen "
                         "Fenster x%.2f für %02d Uhr (%d Stunden aus %.0f "
                         "Tagen).",
                         load_ratio if load_ratio is not None else 1.0,
                         _intraday_raw["load"] or 1.0,
                         config.forecast.intraday_load_decay_hours,
                         load_hours.get(_stunde, 1.0), _stunde,
                         len(load_hours),
                         config.forecast.intraday_load_sameslot_days)
            else:
                log.info("Intraday-Korrektur Last: x%.2f (roh x%.2f; klingt "
                         "über %.1f h ab).", load_ratio,
                         _intraday_raw["load"],
                         config.forecast.intraday_load_decay_hours)

        load_p10, load_p90 = forecaster.uncertainty_band(
            history, load_fc, hist_temp=temp, fut_temp=temp)
        load_band_note = forecaster.uncertainty_summary()
        forecast_issue = pd.Timestamp.now(tz="UTC")

        repo.write_frame(
            "load_forecast",
            pd.DataFrame({"house_load_w": load_fc.values,
                          "house_load_p10_w": load_p10.values,
                          "house_load_p90_w": load_p90.values},
                         index=load_fc.index),
        )
        log.info("Verbrauchsprognose (%d Slots) in InfluxDB geschrieben.", len(load_fc))

        # Optimierungshorizont: exakt oder optional bis Mitternacht erweitert.
        house_series, n_load_fallback = _complete_operational_series(
            load_fc, opt_index, config.forecast.fallback_load_w,
            interpolate_limit=2, edge_limit=1)
        if n_load_fallback:
            log.warning("Lastprognose: %d fehlende Slots durch %.0f W ersetzt.",
                        n_load_fallback, config.forecast.fallback_load_w)
        # Beschreibbare KOPIE: seit pandas 3 (Copy-on-Write) ist .values
        # schreibgeschuetzt - der Slot-0-Anker scheiterte daran stumm.
        house_load = house_series.to_numpy(dtype=float, copy=True)
        load_p10_opt = load_p10.reindex(opt_index)
        load_p90_opt = load_p90.reindex(opt_index)

        # --- 2) Eingangsdaten lesen ------------------------------------- #
        cal_profile = None
        if config.calibration.enabled:
            from .calibration import load_profile
            cal_profile = load_profile(config.calibration.pv_profile)
        calibration_source = "solcast" if config.solcast.enabled else "pvlib"
        # Seit es Profile je Quellgruppe gibt, bekommt auch eine Ausweichquelle
        # ihre eigene Korrektur. Fremde Faktoren bleiben ausgeschlossen.
        from .calibration import pv_profile_for_source
        cal_profile_active = pv_profile_for_source(
            cal_profile, solcast.selected_source(config), calibration_source)

        pv_missing_slots = {}
        pv_without_nowcast = {}

        def _pv_series(signal: str, required: bool = True) -> pd.Series:
            """PV-Signal aufbereiten; Nowcast ausschließlich im Nahbereich."""
            s = solcast.read_pv_signal(
                config, repo, signal, now, opt_end,
                require_complete=config.solcast.enabled).reindex(opt_index)
            missing = int(s.isna().sum())
            pv_missing_slots[signal] = missing
            if required and missing:
                # Keine Energie erfinden: fehlende PV konservativ als 0 W.
                s = s.fillna(0.0)
                log.warning("%s: %d fehlende Slots konservativ als 0 W.",
                            signal, missing)
            if cal_profile_active:
                from .calibration import apply_pv_correction
                s = apply_pv_correction(
                    s, cal_profile_active, config.general.timezone)
            # Dieser unveränderte Stand ist der automatische Challenger. Er
            # wird zusammen mit dem produktiven Nahbereichs-Nowcast archiviert
            # und später Rolling-Origin gegen die PV-Istwerte bewertet.
            pv_without_nowcast[signal] = s.copy()
            if pv_ratio is not None:
                s = s * intraday_factor_series(
                    pv_ratio, s.index, now,
                    config.forecast.intraday_pv_decay_hours,
                    max_slots=config.forecast.intraday_pv_operational_slots,
                    slot_minutes=config.general.slot_minutes)
            return s

        pv = _pv_series("pv_forecast")
        if cal_profile_active:
            log.info("PV-Kalibrierprofil für %s angewandt (%s).",
                     calibration_source, config.calibration.pv_profile)
        elif cal_profile:
            log.info("PV-Kalibrierprofil gehört zu %s und wird auf %s nicht "
                     "angewandt.", calibration_source,
                     solcast.selected_source(config))
        if pv_ratio is not None:
            log.info("Intraday-Korrektur PV: x%.2f (roh x%.2f; nur nächste "
                     "%d Slots, danach unveränderte %s-Prognose).",
                     pv_ratio, _intraday_raw["pv"],
                     config.forecast.intraday_pv_operational_slots,
                     solcast.selected_source(config))
        # Pessimistische PV (Solcast p10, optional): dimensioniert die
        # Einspeise-Linie an Peak-Tagen wolken-robust.
        pv10 = (_pv_series("pv_forecast_p10", required=False)
                if solcast.available(config, repo, "pv_forecast_p10") else None)
        # Oberes Band wird nicht für die Optimierung benötigt, aber zusammen
        # mit p10 archiviert, damit das Dashboard die damals bekannte
        # Prognoseunsicherheit auch rückblickend unverändert zeigen kann.
        pv90 = (_pv_series("pv_forecast_p90", required=False)
                if solcast.available(config, repo, "pv_forecast_p90") else None)
        price, price_estimated = _price_series(
            repo, config, opt_index, now, return_estimated=True)
        from .tariff import read_spot_signal
        spot_price = read_spot_signal(
            config, repo, now, opt_end).reindex(opt_index)

        missing_feed = 0
        if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
            feedin = repo.read_slots(
                "feed_in_tariff", now, opt_end, fill=False).reindex(opt_index)
            missing_feed = int(feedin.isna().sum())
            feedin = feedin.fillna(config.feed_in.fixed_ct_kwh)
            if missing_feed:
                log.warning("Einspeisetarif: %d fehlende Slots durch %.2f ct/kWh "
                            "ersetzt.", missing_feed,
                            config.feed_in.fixed_ct_kwh)
        else:
            feedin = pd.Series(config.feed_in.fixed_ct_kwh, index=opt_index)
        # Solarspitzengesetz: in Negativpreis-Stunden gibt es (für betroffene
        # Anlagen) keine Einspeisevergütung -> Export dort mit 0 ct bewerten.
        if config.feed_in.zero_at_negative_price:
            if spot_price.notna().any():
                feedin = feedin.where(~(spot_price < 0.0), 0.0)
            else:
                log.warning("Negativpreisregel ausgesetzt: kein unveränderter "
                            "Börsenpreis verfügbar.")

        # Plausibilitäts-Grenzen auf die externen Eingaben (Preis/PV) UND die
        # Hauslast, bevor irgendetwas davon in Archiv/Optimierer fließt: ein
        # einzelner API-Ausreißer (Solcast-Sprung, Preis-Spike, negativer Wert)
        # darf keinen ganzen Steuerzyklus verzerren.
        if getattr(config, "sanity", None) and config.sanity.enabled:
            from .sanity import sanitize_inputs
            price, pv, pv10, house_series = sanitize_inputs(
                config, price=price, pv=pv, pv10=pv10, load=house_series)
            house_load = house_series.to_numpy(dtype=float, copy=True)

        # Live-Werte optional direkt vom E3DC (RSCP) statt aus der InfluxDB.
        # force=True: im Loop-Betrieb frisch pollen (kein Zyklus-übergreifender Cache).
        live = e3dc.read_live(force=True) if (e3dc and config.e3dc_rscp.read_live) else None
        live_src = "RSCP"
        if live is None:               # ohne RSCP: extern per Ingest-API eingespeiste Werte
            from .ingest import get_live
            live = get_live()
            live_src = "Ingest-API"
        if live:
            live_soc = live.get("soc_percent")
            log.info("%s live: SoC %.1f%% (%s), PV %.0f W, Last %.0f W.",
                     live_src, float(live_soc) if live_soc is not None else -1.0,
                     live.get("soc_source") or "extern",
                     live.get("pv_w") or 0, live.get("house_load_w") or 0)
            # Ist-Werte lokal protokollieren (Ersatz für die InfluxDB-Ist-Signale).
            if config.e3dc_rscp.history_source:
                try:
                    write_actuals(config.e3dc_rscp.history_db_path, now, live)
                    if config.forecast.live_nowcast_enabled:
                        from .local_history import (prune_live_samples,
                                                    write_live_sample)
                        write_live_sample(
                            config.e3dc_rscp.history_db_path, now, live)
                        _settle_actual_slots(config, now)
                        prune_live_samples(
                            config.e3dc_rscp.history_db_path,
                            now - pd.Timedelta(
                                days=max(
                                    1, config.forecast.live_nowcast_retention_days)))
                except Exception as exc:
                    log.warning("Ist-Wert-Protokollierung fehlgeschlagen (%s).", exc)
            try:
                execution_status = _audit_execution(config, now, live, e3dc=e3dc)
            except Exception as exc:
                log.warning("Soll/Ist-Ausführungsprüfung fehlgeschlagen (%s).", exc)

        # Slot-0-Anker: für den unmittelbar laufenden Slot schlägt die
        # Live-Messung (RSCP direkt, sonst Mittel der letzten Slot-Länge aus
        # InfluxDB) die Prognose - der Sollwert basiert auf dem echten Zustand.
        try:
            slot_td = pd.Timedelta(freq)
            if (live and live.get("house_load_w") is not None
                    and np.isfinite(float(live["house_load_w"]))):
                live_house_w = float(live["house_load_w"])
                if config.forecast.disaggregate_controllable_loads:
                    controlled_w = 0.0
                    stage_power = {
                        f"{ld.name}/{stage.name}": float(stage.power_w)
                        for ld in config.controllable_loads
                        if ld.enabled and ld.type == "thermal"
                        for stage in ld.stages
                    }
                    stage_power.update({
                        ld.name: float(ld.power_w)
                        for ld in config.controllable_loads
                        if ld.enabled and ld.type == "deferrable"
                    })
                    controlled_w = _anchor_controlled_w(
                        config, load_feedback_status, stage_power)
                    live_house_w -= controlled_w
                house_load[0] = max(0.0, live_house_w)
            else:
                m = repo.read_slots("house_consumption", now - slot_td, now,
                                    fill=False).mean()
                if np.isfinite(m):
                    house_load[0] = max(0.0, float(m))
        except Exception as exc:
            # Kein debug: ein stumm gescheiterter Anker blieb monatelang
            # unbemerkt (pandas-3-Umstellung).
            log.warning("Slot-0-Anker (Last) fehlgeschlagen (%s).", exc)
        # PV getrennt: ein Fehler beim Last-Anker darf den PV-Anker nicht
        # mitreißen (und umgekehrt).
        try:
            slot_td = pd.Timedelta(freq)
            if (live and live.get("pv_w") is not None
                    and np.isfinite(float(live["pv_w"]))):
                pv.iloc[0] = max(0.0, float(live["pv_w"]))
            elif repo.signal_available("pv_generation"):
                m = repo.read_slots("pv_generation", now - slot_td, now,
                                    fill=False).mean()
                if np.isfinite(m):
                    pv.iloc[0] = max(0.0, float(m))
            log.debug("Slot-0-Anker: Last %.0f W, PV %.0f W.",
                      house_load[0], pv.iloc[0])
        except Exception as exc:
            log.warning("Slot-0-Anker (PV) fehlgeschlagen (%s).", exc)

        # Anfangs-SoC Haus: bevorzugt RSCP-Live, sonst InfluxDB.
        lookback = now - timedelta(hours=6)
        soc_pct = live.get("soc_percent") if live else None
        if soc_pct is not None and not np.isfinite(float(soc_pct)):
            soc_pct = None
        if soc_pct is None:
            soc_pct = repo.read_scalar_latest("battery_soc", lookback, now)
        if soc_pct is None:
            log.warning("Kein Haus-SoC gefunden – nehme min_soc an.")
            soc_pct = config.house_battery.min_soc_percent
        init_house_soc = soc_pct / 100.0 * config.house_battery.capacity_wh
        # Der Plan rechnet Slot 0 als GANZEN Slot ab Slotbeginn; der SoC ist
        # aber "jetzt" gemessen. Laeuft der Zyklus mitten im Slot (manueller
        # Recalc, Dashboard-Aktion, langsamer Zyklus), wird der Mess-SoC auf
        # den Slotbeginn zurueckgerechnet - sonst zaehlt die schon geflossene
        # Akkuenergie doppelt (bei 9 kW Netzladen um :13 rund 2 kWh).
        if live and live.get("soc_percent") is not None:
            elapsed_s = (pd.Timestamp.now(tz=config.general.timezone)
                         - now).total_seconds()
            corrected = _soc_at_slot_start(
                init_house_soc, live.get("battery_w"), elapsed_s,
                config.house_battery.capacity_wh,
                config.general.slot_minutes * 60.0)
            if abs(corrected - init_house_soc) >= 50.0:
                log.info("Anfangs-SoC auf den Slotbeginn zurückgerechnet: "
                         "%.0f -> %.0f Wh (%.0f s im Slot, Akku %.0f W).",
                         init_house_soc, corrected, elapsed_s,
                         float(live.get("battery_w") or 0.0))
            init_house_soc = corrected

        # Anfangs-SoC Auto (optional). Anwesenheit: bevorzugt über den Wallbox-
        # Status (Signal vehicle_connected, 0/1) - ein Tage alter SoC-Wert in
        # der DB heißt sonst nicht, dass das Auto wirklich angesteckt ist.
        init_car_soc = None
        car_present = False
        if config.vehicle.enabled and repo.signal_available("vehicle_soc"):
            car_pct = repo.read_scalar_latest("vehicle_soc", lookback, now)
            connected = None
            connected_signal = repo.signal_available("vehicle_connected")
            if connected_signal:
                connected = repo.read_scalar_latest("vehicle_connected", lookback, now)
            if connected_signal and connected is None:
                log.warning("Wallbox-Anwesenheit ohne aktuellen Datenwert – Auto "
                            "wird sicherheitshalber nicht mitoptimiert.")
            elif connected is not None and connected < 0.5:
                log.info("Auto nicht angesteckt (vehicle_connected=%.0f) – "
                         "wird nicht mitoptimiert.", connected)
            elif car_pct is not None:
                init_car_soc = car_pct / 100.0 * config.vehicle.capacity_wh
                car_present = True
                log.info("Auto erkannt, SoC %.1f%%%s.", car_pct,
                         " (Wallbox: angesteckt)" if connected is not None else "")

        # --- 3) Optimierung --------------------------------------------- #
        _runtime_update(phase="Plan optimieren", progress=58,
                        message=f"Solver berechnet {len(opt_index)} Zeitschritte")
        log.info("Starte MILP-Optimierung (%d Slots) ...", len(opt_index))
        load_state = _read_load_state(config, publisher)
        if temp is not None:
            recent_temp = temp[(temp.index >= now - pd.Timedelta(hours=24))
                               & (temp.index < now)].dropna()
            temp_fallback = float(recent_temp.median()) if len(recent_temp) else 20.0
            ambient, n_temp_fallback = _complete_operational_series(
                temp, opt_index, temp_fallback, interpolate_limit=8, edge_limit=4)
            if n_temp_fallback:
                log.warning("Außentemperatur: %d fehlende Slots durch %.1f °C "
                            "ersetzt.", n_temp_fallback, temp_fallback)
        else:
            ambient = None
        if solar is not None:
            solar_safe, n_solar_fallback = _complete_operational_series(
                solar, opt_index, 0.0, interpolate_limit=8, edge_limit=4)
            if n_solar_fallback:
                log.warning("Solarstrahlung: %d fehlende Slots als 0 W/m².",
                            n_solar_fallback)
        else:
            solar_safe = None

        # Qualität der TATSÄCHLICH an den Optimierer übergebenen Prognosen.
        # Die Einstufung bezieht sich auf den aktuellen Horizont und trennt
        # echte Quelldaten klar von Slot-Fallbacks bzw. Modellergänzungen.
        total_slots = len(opt_index)
        try:
            from .local_history import (latest_pv_forecast_issue,
                                        latest_weather_forecast_issue)
            pv_issue = latest_pv_forecast_issue(
                config.e3dc_rscp.history_db_path,
                solcast.selected_source_ids(config))
            weather_issue = latest_weather_forecast_issue(
                config.e3dc_rscp.history_db_path)
        except Exception:
            pv_issue = weather_issue = None
        pv_name = solcast.selected_source(config)
        pv_note = f"Quelle: {'Solcast' if pv_name == 'solcast' else 'pvlib'}"
        pv_note += (
            f" · PV-Nowcast nur {config.forecast.intraday_pv_operational_slots} "
            "operative Slots; danach Quelle unverändert")
        if pv_selection and pv_selection.get("reason"):
            pv_note += f" ({pv_selection['reason']})"
        try:
            from .pvforecast import status_summary as _pv_ensemble_summary
            if config.pv_model.shadow or config.pv_model.enabled:
                pv_note += f" · {_pv_ensemble_summary()}"
        except Exception as exc:
            log.debug("Ensemble-Status fuer die Prognosenotiz nicht verfuegbar: %s", exc)
        forecast_quality = [
            _forecast_quality_entry(
                "Hauslast", total_slots,
                total_slots if load_source_missing else n_load_fallback,
                "konservativer Lastwert", forecast_issue,
                load_band_note
                + (f" · {load_model_note}" if load_model_note else "")),
            _forecast_quality_entry(
                "PV", total_slots, pv_missing_slots.get("pv_forecast", total_slots),
                "0 W", pv_issue, pv_note),
        ]
        if pv10 is None:
            forecast_quality.append(_forecast_quality_entry(
                "PV-p10", total_slots, total_slots, "PV-Erwartungswert",
                pv_issue, pv_note))
        else:
            forecast_quality.append(_forecast_quality_entry(
                "PV-p10", total_slots,
                pv_missing_slots.get("pv_forecast_p10", total_slots),
                "konservative PV-Ableitung", pv_issue, pv_note))
        if config.calibration.enabled:
            profile_name = os.path.basename(config.calibration.pv_profile)
            profile_issue = None
            try:
                profile_issue = pd.Timestamp(
                    os.path.getmtime(config.calibration.pv_profile),
                    unit="s", tz="UTC")
            except OSError as exc:
                log.debug("Alter des Kalibrierprofils nicht bestimmbar: %s", exc)
            if cal_profile is None:
                cal_level, cal_state = "replaced", "Profil fehlt oder ist unlesbar"
                cal_detail = (
                    f"{profile_name} · PV-Korrektur wird derzeit nicht angewandt")
            elif cal_profile_active is None:
                cal_level, cal_state = "partial", "für aktive Quelle ausgesetzt"
                vorhanden = ", ".join(
                    (cal_profile.get("pv_sources") or {})) or calibration_source
                cal_detail = (
                    f"{profile_name} hat kein Profil für "
                    f"{solcast.selected_source(config)} (vorhanden: {vorhanden})")
            else:
                cal_level, cal_state = "current", "aktiv"
                cal_detail = (f"{profile_name} · angewandt auf "
                              f"{solcast.selected_source(config)}")
            forecast_quality.append({
                "name": "PV-Kalibrierung", "state": cal_state,
                "level": cal_level, "detail": cal_detail,
                "issued_at": (profile_issue.isoformat()
                              if profile_issue is not None else None),
            })
        # Strompreis: der Day-Ahead wird erst mittags für morgen veröffentlicht,
        # der ferne Horizont ist also IMMER geschätzt -> die reine Ersatzquote
        # wäre dauerhaft gelb. Ampel daher am nächsten Tag ausrichten: grün,
        # sobald morgen real vorliegt; gelb, solange morgen (noch) geschätzt ist;
        # rot nur ohne jeden realen Preis.
        _est = price_estimated.to_numpy(dtype=bool)
        _next_day_end = now.normalize() + pd.DateOffset(days=2)
        _next_day = opt_index < _next_day_end
        if int(price_estimated.sum()) >= total_slots or total_slots == 0:
            _p_level, _p_state = "replaced", "keine realen Preise"
        elif bool(_est[_next_day].any()):
            _p_level, _p_state = "partial", "morgen noch geschätzt"
        else:
            _p_level, _p_state = "current", "morgen real vorhanden"
        forecast_quality.append(_forecast_quality_entry(
            "Strompreis", total_slots, int(price_estimated.sum()),
            _last_price_fill or baseline_name(config), forecast_issue,
            level_override=_p_level, state_override=_p_state))
        if config.weather.enabled:
            forecast_quality.extend([
                _forecast_quality_entry(
                    "Außentemperatur", total_slots,
                    total_slots if temp is None else n_temp_fallback,
                    "Temperatur-Ersatzwert", weather_issue),
                _forecast_quality_entry(
                    "Solarstrahlung", total_slots,
                    total_slots if solar is None else n_solar_fallback,
                    "0 W/m²", weather_issue),
            ])
        if config.feed_in.mode == "db":
            forecast_quality.append(_forecast_quality_entry(
                "Einspeisetarif", total_slots,
                total_slots if not repo.signal_available("feed_in_tariff")
                else missing_feed,
                "fester Einspeisetarif", forecast_issue))

        # Den tatsächlich verwendeten, vollständig aufbereiteten Prognosesatz
        # unveränderlich archivieren. Damit kann ein späterer Backtest genau
        # den Informationsstand dieses Laufs wiederverwenden (inklusive
        # Kalibrierung, Intraday-Korrektur und markierter Preisschätzungen).
        try:
            from .local_history import write_optimizer_forecast_archive
            archive_series = {
                # Inklusive Slot-0-Anker: archiviert wird, was der
                # Optimierer wirklich bekam.
                "house_load_w": pd.Series(
                    np.asarray(house_load, dtype=float),
                    index=house_series.index, name=house_series.name),
                "house_load_p10_w": load_p10_opt,
                "house_load_p90_w": load_p90_opt,
                "pv_w": pv,
                "pv_without_nowcast_w": pv_without_nowcast.get("pv_forecast"),
                "price_ct_kwh": price,
                "spot_price_ct_kwh": spot_price,
                "feedin_ct_kwh": feedin,
            }
            if load_base_fc is not None:
                archive_series["house_load_base_w"] = (
                    load_base_fc.reindex(opt_index))
            for model_name, model_series in load_candidates.items():
                archive_series[
                    f"house_load_candidate_{model_name}_w"] = (
                        model_series.reindex(opt_index))
            if pv10 is not None:
                archive_series["pv10_w"] = pv10
                archive_series["pv10_without_nowcast_w"] = (
                    pv_without_nowcast.get("pv_forecast_p10"))
            if pv90 is not None:
                archive_series["pv90_w"] = pv90
                archive_series["pv90_without_nowcast_w"] = (
                    pv_without_nowcast.get("pv_forecast_p90"))
            if ambient is not None:
                archive_series["ambient_temp_c"] = ambient
            if solar_safe is not None:
                archive_series["solar_w_m2"] = solar_safe
            n_archived = write_optimizer_forecast_archive(
                config.e3dc_rscp.history_db_path,
                pd.Timestamp.now(tz="UTC"), archive_series,
                estimated={"price_ct_kwh": price_estimated})
            log.info("Optimierer-Prognosesnapshot archiviert: %d Werte.",
                     n_archived)
        except Exception as exc:  # Archivierung darf die Steuerung nie stoppen
            log.warning("Optimierer-Prognosesnapshot nicht speicherbar (%s).", exc)

        inputs = OptimizerInputs(
            index=opt_index,
            house_load_w=np.asarray(house_load, dtype=float),
            pv_w=pv.values.astype(float),
            price_ct_kwh=price.values.astype(float),
            feedin_ct_kwh=feedin.values.astype(float),
            initial_house_soc_wh=init_house_soc,
            initial_car_soc_wh=init_car_soc,
            car_present=car_present,
            pv10_w=(pv10.values.astype(float) if pv10 is not None else None),
            ambient_temp_c=(ambient.values.astype(float)
                            if ambient is not None else None),
            solar_w_m2=(solar_safe.values.astype(float)
                        if solar_safe is not None else None),
            load_state=load_state,
            load_feedback=load_feedback,
            load_run_state=load_run_state,
            grid_charge_run_wh=grid_charge_run_wh,
            spot_price_ct_kwh=spot_price.values.astype(float),
        )
        # Ist-Temperatur thermischer Lasten für den Dashboard-Verlauf mitschreiben –
        # bewusst UNABHÄNGIG von enabled: die Pool-Temperatur ist auch dann
        # interessant, wenn das EMS die Last gerade nicht steuert.
        if publisher is not None:
            from .local_history import write_load_temp
            for ld in getattr(config, "controllable_loads", []):
                if ld.type != "thermal" or not ld.temp_signal:
                    continue
                _tc = publisher.get_load_temp(ld.temp_signal)
                if _tc is None:
                    continue
                try:
                    write_load_temp(config.e3dc_rscp.history_db_path, now,
                                    ld.name, float(_tc))
                except Exception as exc:  # pragma: no cover
                    log.debug("Ist-Temp-Historie (%s) fehlgeschlagen: %s", ld.name, exc)
        result = Optimizer(config, stabilize_plan=True).solve(inputs)
        _last_solve_snapshot["inputs"] = inputs      # für den What-if-Simulator
        _last_solve_snapshot["config"] = config
        from .executability import annotate_executability
        annotate_executability(config, result.table)
        log.info("Optimierung: %s, erwartete Netto-Kosten %.2f € (Horizont).",
                 result.status, result.total_cost_ct / 100.0)
        # Solver-Telemetrie vor dem Speichern gegen die bisherige Historie
        # bewerten, damit der aktuelle Lauf nicht seine eigene Basis verzerrt.
        try:
            from .local_history import read_solver_runs, write_solver_run
            old_runs = read_solver_runs(
                config.e3dc_rscp.history_db_path,
                config.monitoring.solver_runtime_baseline_runs)
            old_seconds = [float(r["seconds"]) for r in old_runs
                           if r.get("seconds") is not None]
            median_s = float(np.median(old_seconds)) if old_seconds else None
            absolute_slow = (result.solver_seconds >=
                             config.monitoring.solver_runtime_alert_seconds)
            relative_slow = (median_s is not None and len(old_seconds) >= 4 and
                             result.solver_seconds >= median_s *
                             config.monitoring.solver_runtime_factor)
            solver_status = {
                "seconds": result.solver_seconds,
                "polish_seconds": result.solver_polish_seconds,
                "median_seconds": median_s,
                "slow": bool(absolute_slow or relative_slow),
                "slots": result.solver_slots, "variables": result.solver_variables,
                "binaries": result.solver_binaries,
                "constraints": result.solver_constraints,
                "warm_start": result.solver_warm_start,
                "mip_gap": result.solver_mip_gap, "status": result.status,
            }
            write_solver_run(config.e3dc_rscp.history_db_path, now, result)
        except Exception as exc:
            log.warning("Solver-Telemetrie nicht speicherbar (%s).", exc)

        # --- 3b) Planprüfung (Invarianten) ------------------------------ #
        _runtime_update(phase="Plan prüfen", progress=76,
                        message="Grenzen und wirtschaftliche Entscheidungen werden geprüft")
        # Fängt Modellfehler ("das darf nie passieren") automatisch ab, statt
        # sich auf den Blick ins Dashboard zu verlassen. Errors = echter Bug,
        # Warnungen = verdächtig (siehe ems/validate.py).
        violations = validate_plan(config, result, inputs)
        plan_errors = [v for v in violations if v.severity == "error"]
        plan_warnings = [v for v in violations if v.severity == "warning"]
        for v in violations:
            (log.error if v.severity == "error" else log.warning)("Planprüfung: %s", v)
        log.info("Planprüfung: %s.", summarize(violations))
        # solver.time_limit ist an peak-Sommertagen erwartbar (lose LP-Relaxation,
        # Optimalität unbeweisbar -> das Zeitlimit greift jeden Zyklus, der Plan ist
        # aber near-optimal). Nicht als Homey-Alarm eskalieren (sonst 15-min-Spam);
        # in der Planprüfung/Log bleibt er sichtbar. Echte Solver-Probleme melden
        # sich als solver.infeasible (error).
        alertable_warnings = [v for v in plan_warnings if v.rule != "solver.time_limit"]

        # --- 3c) SoC-Drift (Modell gegen Realität) ---------------------- #
        drift_mae = None
        efficiency_drift = _last_monitor_diagnostics.get("efficiency")
        ac_charge_drift = _last_monitor_diagnostics.get("ac_charge")
        execution_bias = _last_monitor_diagnostics.get("execution_bias")
        load_bias = _last_load_bias
        diagnostics_refreshed = False
        if config.monitoring.drift_enabled:
            try:
                from .drift import DriftMonitor
                monitor = DriftMonitor(config)
                drift_mae = monitor.check(repo, now)
                # Bilanzprüfung zusätzlich zur SoC-Kurve: die findet einen
                # systematischen Modellfehler nicht (jeder Zyklus startet neu
                # beim gemessenen SoC). Höchstens stündlich - reine SQLite-Reads,
                # aber je Zyklus wäre es unnötig.
                if _time.time() - _last_efficiency_check > 3600:
                    efficiency_drift = monitor.check_energy_model(now)
                    ac_charge_drift = monitor.check_ac_charge_model(now)
                    execution_bias = monitor.check_execution_bias(now)
                    load_bias = monitor.check_load_bias(now)
                    _last_monitor_diagnostics = {
                        "efficiency": efficiency_drift,
                        "ac_charge": ac_charge_drift,
                        "execution_bias": execution_bias,
                    }
                    _last_load_bias = load_bias
                    _last_efficiency_check = _time.time()
                    diagnostics_refreshed = True
            except Exception as exc:
                log.warning("Drift-Check fehlgeschlagen (%s).", exc)
        else:
            efficiency_drift = execution_bias = load_bias = None
            ac_charge_drift = None
        monitoring_status = {}
        if drift_mae is not None:
            monitoring_status["soc_drift"] = {
                "mae_pp": round(float(drift_mae), 2),
                "threshold_pp": float(config.monitoring.drift_alert_percent),
                "window_hours": float(config.monitoring.drift_window_hours),
                "alert": bool(
                    drift_mae > config.monitoring.drift_alert_percent),
            }
        if efficiency_drift:
            monitoring_status["efficiency"] = efficiency_drift
        if ac_charge_drift:
            monitoring_status["ac_charge"] = ac_charge_drift
        if execution_bias:
            monitoring_status["execution_bias"] = execution_bias

        # --- 3d) Debug-Schnappschuss (für den Mail-Report-Button) ------- #
        try:
            from .debugdump import save_snapshot
            save_snapshot(config, now, inputs, result, violations, drift_mae)
        except Exception as exc:
            log.warning("Debug-Schnappschuss fehlgeschlagen (%s).", exc)

        # --- 4) MQTT (best effort – darf den Lauf nicht abbrechen) ------ #
        _runtime_update(phase="Steuerung übertragen", progress=84,
                        message="Sollwerte werden an MQTT und E3DC übertragen")
        try:
            if one_shot:
                publisher = HomeyMqttPublisher(config)
                publisher.event_sink = _make_alert_event_sink(config)
            # Ein konsolidierter Alarm aus der Planprüfung (deckt infeasible und
            # Solver-Zeitlimit mit ab); Errors haben Vorrang vor Warnungen.
            if plan_errors:
                publisher.publish_alert(
                    "error", "Planprüfung: " + "; ".join(
                        f"{v.rule} ({v.count})" for v in plan_errors[:5]))
            elif alertable_warnings:
                publisher.publish_alert(
                    "warning", "Planprüfung: " + "; ".join(
                        f"{v.rule} ({v.count})" for v in alertable_warnings[:5]))
            if result.car_target_shortfall_wh > 100.0:
                publisher.publish_alert(
                    "warning",
                    f"Auto erreicht Ziel-SoC nicht: es fehlen "
                    f"{result.car_target_shortfall_wh / 1000.0:.1f} kWh zur Abfahrt.")
            if result.grid_overload_wh > 100.0:
                publisher.publish_alert(
                    "warning",
                    f"Hausanschluss-Grenze überschritten: "
                    f"{result.grid_overload_wh / 1000.0:.1f} kWh über dem Limit "
                    f"geplant – eine Lastspitze war nicht anders deckbar.")
            if result.infeasible and getattr(result, "infeasible_reason", None):
                publisher.publish_alert(
                    "error",
                    f"Optimierung unlösbar – wahrscheinliche Ursache: "
                    f"{result.infeasible_reason}")
            if drift_mae is not None and drift_mae > config.monitoring.drift_alert_percent:
                publisher.publish_alert(
                    "warning", f"SoC-Drift {drift_mae:.1f} pp über Schwelle "
                               f"({config.monitoring.drift_alert_percent:.0f} pp) – "
                               f"Modell weicht von der Realität ab.")
            if diagnostics_refreshed and load_bias and load_bias.get("alert"):
                night = load_bias.get("night_median_w")
                scope = (f"Nacht-Bias {night:+.0f} W; "
                         if night is not None
                         and load_bias.get("alert_scope") in ("Nacht", "Tag und Nacht")
                         else "")
                publisher.publish_alert(
                    "warning",
                    f"Lastprognose: {scope}{load_bias['median_w']:+.0f} W im Median "
                    f"daneben ({load_bias['kwh_per_day']:+.2f} kWh/Tag) – "
                    f"{load_bias.get('diagnostic', 'Lastmodell prüfen')}.")
            if (diagnostics_refreshed and execution_bias
                    and execution_bias.get("alert")):
                publisher.publish_alert(
                    "warning",
                    f"Ausführungs-Versatz {execution_bias['median_w']:+.0f} W "
                    f"im Median über {execution_bias['n']} Slots "
                    f"({execution_bias['kwh_per_day']:+.2f} kWh/Tag) – die "
                    f"Anlage weicht vom ausführbaren Befehlsrahmen ab.")
            if (diagnostics_refreshed and efficiency_drift
                    and efficiency_drift.get("alert")):
                publisher.publish_alert(
                    "warning",
                    f"Entladewirkungsgrad {efficiency_drift['measured']:.2f} "
                    f"gemessen gegen {efficiency_drift['model']:.2f} im Modell "
                    f"({efficiency_drift['deviation_percent']:+.0f} %) – der "
                    f"geplante SoC-Verlauf läuft systematisch weg.")
            _publish_solver_alarm(publisher, solver_status)
            _publish_execution_alarm(publisher, config, execution_status)
            _publish_load_feedback_alarm(publisher, load_feedback_status)
            steuer_ts = _control_slot(config, now)
            if steuer_ts != now:
                log.warning(
                    "Zyklus lief ueber die Slotgrenze: Plan beginnt %s, "
                    "kommandiert wird der laufende Slot %s. Der Solve dauerte "
                    "zu lange fuer den 15-min-Takt.",
                    now.strftime("%H:%M"), steuer_ts.strftime("%H:%M"))
            load_cmds = publisher.publish(
                result.table, steuer_ts, result.load_mqtt_map)
            plan_published = True
            _log_switching_events(config, result.table, load_cmds)  # Schaltvorgänge
            # Publizierte Heiz-Freigabe je thermischer Last loggen (0 = sicher
            # aus): Grundlage der Thermomodell-Kalibrierung
            # (python -m ems.pool_calibration). NUR bei aktiver Steuerung UND
            # in der Saison loggen: außerhalb (Winter) ist der Temperatur-
            # sensor gar nicht im Pool - solche Slots dürfen NICHT als
            # "sicher aus" in den Fit einfließen. Nicht geloggt = unbekannt
            # = vom Fit ignoriert (read_load_cmd füllt Lücken bewusst nicht).
            try:
                from .local_history import write_load_cmd
                from .loads import in_season
                slot_ts = floor_time(now, "15min")
                for ld in getattr(config, "controllable_loads", []):
                    if ld.type != "thermal" or not ld.enabled \
                            or not in_season(ld, slot_ts):
                        continue
                    permit = int(any(
                        v == 1 for lbl, v in (load_cmds or {}).items()
                        if lbl == ld.name or lbl.startswith(f"{ld.name}/")))
                    write_load_cmd(config.e3dc_rscp.history_db_path, slot_ts,
                                   ld.name, permit)
                    from .local_history import write_load_stage_cmd
                    for st in ld.stages:
                        label = f"{ld.name}/{st.name}"
                        cmd = (load_cmds or {}).get(label)
                        if cmd in (0, 1, False, True):
                            write_load_stage_cmd(
                                config.e3dc_rscp.history_db_path, slot_ts,
                                ld.name, st.name, int(bool(cmd)))
            except Exception as exc:
                log.debug("load_cmd-Log fehlgeschlagen (%s).", exc)
        except Exception as exc:
            log.warning("MQTT-Ausgabe fehlgeschlagen (%s) – InfluxDB-Writeback "
                        "und Dashboard werden trotzdem erstellt.", exc)

        # --- 4b) Steuerung optional direkt per RSCP an den E3DC ---------- #
        if config.e3dc_rscp.control_enabled:
            if e3dc is None:
                control_status = {
                    "ok": False, "state": "unavailable", "mode": "control",
                    "message": "Keine RSCP-Verbindung zum E3DC verfügbar.",
                    "expected": {}, "actual": {},
                    "checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
                }
            elif result.infeasible:
                control_status = {
                    "ok": False, "state": "plan_infeasible", "mode": "control",
                    "message": "Steuerung wegen unzulässigem Optimierungsplan ausgesetzt.",
                    "expected": {}, "actual": {},
                    "checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
                }
            else:
                # Einen zwischen den Zyklen erkannten Watchdog-Ausfall zuerst
                # melden, bevor der neue erfolgreiche Befehl ihn überschreibt.
                previous = e3dc.last_control_status()
                if previous and previous.get("state") == "watchdog_failed":
                    _store_control_status(config, previous)
                    _publish_control_alarm(publisher, config, previous)
                try:
                    steuer_ts = _control_slot(config, now)
                    pos = result.table.index.get_indexer(
                        [steuer_ts], method="ffill")[0]
                    row = result.table.iloc[max(pos, 0)]
                    control_status = _apply_curtailment_and_control(e3dc, row)
                    # Aktuellen Befehl lokal sichern -> beim nächsten Dienststart
                    # sofort re-applybar (schließt die Steuer-Lücke bis zum 1. Solve).
                    try:
                        from .local_history import write_last_control
                        write_last_control(
                            config.e3dc_rscp.history_db_path, steuer_ts,
                            {c: row.get(c) for c in CONTROL_COLS if c in row.index})
                    except Exception as exc:   # pragma: no cover
                        log.debug("last_control-Sicherung fehlgeschlagen (%s).", exc)
                except Exception as exc:
                    log.warning("RSCP-Steuerung fehlgeschlagen (%s).", exc)
                    control_status = {
                        "ok": False, "state": "write_failed", "mode": "control",
                        "message": f"E3DC-Steuerbefehl fehlgeschlagen: {exc}",
                        "expected": {}, "actual": {},
                        "checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
                    }
            _store_control_status(config, control_status)
            _publish_control_alarm(publisher, config, control_status)
            if control_status and control_status.get("ok") is True:
                plan_published = True
        if one_shot and publisher is not None:
            try:
                publisher.close()
            except Exception as exc:
                log.debug("MQTT nach Einzellauf nicht sauber geschlossen: %s", exc)

        if plan_published:
            try:
                from .local_history import write_execution_plan
                write_execution_plan(
                    config.e3dc_rscp.history_db_path, now, result.table,
                    initial_soc_percent=float(soc_pct),
                    static_export_limit_w=config.inverter.max_export_w,
                    dashboard_series={name: values for name, values in {
                        "house_load_p10_w": load_p10_opt,
                        "house_load_p90_w": load_p90_opt,
                        "pv10_w": pv10,
                        "pv90_w": pv90,
                        "price_estimated": price_estimated,
                    }.items() if values is not None})
            except Exception as exc:
                log.warning("Sollfahrplan-Audit nicht speicherbar (%s).", exc)

        # --- 5) InfluxDB-Writeback -------------------------------------- #
        ctrl = result.table[[c for c in CONTROL_COLS if c in result.table.columns]]
        repo.write_frame("control_table", ctrl)
        pred = result.table[[c for c in PREDICTION_COLS if c in result.table.columns]]
        repo.write_frame("predicted_state", pred)
        log.info("Steuertabelle + Prognosezustände in InfluxDB geschrieben.")

        # --- 5b) Ersparnis-Tracking (Ist vs. Ohne-EMS-Baseline) ---------- #
        savings_eur = None
        if config.savings.enabled:
            try:
                from .savings import SavingsTracker
                savings_eur = SavingsTracker(config).update(repo, now)
            except Exception as exc:
                log.warning("Ersparnis-Tracking fehlgeschlagen (%s).", exc)

        # --- 6) Dashboard ----------------------------------------------- #
        _runtime_update(phase="Dashboard aktualisieren", progress=94,
                        message="Plan und Diagnoseansicht werden geschrieben")
        # Anzeige ab heute 00:00: Ist-Werte (bis jetzt) und Prognose (ganzer
        # Bereich) vergleichbar; Steuerung/Prognose-SoC für die Zukunft.
        if config.dashboard.enabled:
            display = _build_display_frame(repo, config, now, history_raw, result,
                                           intraday=(load_ratio, pv_ratio),
                                           hist_pv=hist_pv, fut_pv=fut_pv,
                                           e3dc=e3dc)
            # Ist-Temperatur-Verlauf thermischer Lasten für das Temperatur-Panel.
            load_temp_actual = {}
            try:
                from .local_history import read_load_temp
                from .loads import _slug as _load_slug
                tz = config.general.timezone
                for ld in getattr(config, "controllable_loads", []):
                    if ld.type == "thermal":
                        s = read_load_temp(config.e3dc_rscp.history_db_path, ld.name,
                                           display.index[0], display.index[-1], tz)
                        if not s.empty:
                            load_temp_actual[ld.name] = s
                            slug = _load_slug(ld.name)
                            actual_col = f"actual_load_{slug}_temp_c"
                            planned_col = f"load_{slug}_temp_c"
                            display[actual_col] = s.reindex(display.index)
                            if planned_col in display.columns:
                                display[f"load_{slug}_temp_deviation_c"] = (
                                    pd.to_numeric(
                                        display[actual_col], errors="coerce")
                                    - pd.to_numeric(
                                        display[planned_col], errors="coerce")
                                ).round(1)
            except Exception as exc:  # pragma: no cover
                log.debug("Ist-Temp-Verlauf fürs Dashboard nicht verfügbar: %s", exc)
            # Außentemperatur auf den Anzeigezeitraum zuschneiden (temp deckt den
            # kompletten Lookback von forecast.lookback_days ab, z.B. 730 Tage -
            # das Dashboard soll wie alle anderen Kurven nur "heute ab 00:00" bis
            # Horizontende zeigen).
            ambient_temp_display = (
                temp[(temp.index >= display.index[0]) & (temp.index <= display.index[-1])]
                if temp is not None else None)
            # Vergleichs-Overlay Solcast vs. pvlib-Modell (nur bei shadow/enabled
            # konfiguriertem pv_model; beeinflusst den Optimierer nicht).
            pv_compare = None
            try:
                from . import pvforecast
                if config.pv_model.arrays:
                    pvc = pvforecast.read_compare(config, display.index[0],
                                                  display.index[-1])
                    pv_compare = pvc if not pvc.empty else None
            except Exception as exc:  # pragma: no cover
                log.debug("PV-Vergleichsreihe nicht verfügbar: %s", exc)
            try:
                from .local_history import read_latest_thermal_calibration
                thermal_calibration = read_latest_thermal_calibration(
                    config.e3dc_rscp.history_db_path,
                    config.general.timezone)
            except Exception:
                thermal_calibration = []
            build_dashboard(config, display, result.total_cost_ct,
                            export_line_w=result.export_line_w,
                            savings_eur=savings_eur,
                            violations=violations,
                            load_temp_actual=load_temp_actual,
                            ambient_temp_c=ambient_temp_display,
                            source_status=_source_status(config, now),
                            pv_compare=pv_compare,
                            control_status=control_status,
                            forecast_quality=forecast_quality,
                            solver_status=solver_status,
                            execution_status=execution_status,
                            load_feedback_status=load_feedback_status,
                            thermal_calibration=thermal_calibration,
                            auto_peak_basis=getattr(result, "auto_peak_basis", None),
                            load_bias=load_bias,
                            monitoring_status=monitoring_status,
                            plan_status={
                                "status": result.status,
                                "infeasible": result.infeasible,
                                "infeasible_reason": result.infeasible_reason,
                                "car_target_shortfall_wh":
                                    result.car_target_shortfall_wh,
                                "grid_overload_wh": result.grid_overload_wh,
                            })
            if getattr(config.dashboard, "api_enabled", False):
                api_file = os.path.join(os.path.dirname(config.dashboard.output_path) or ".", "api_data.json")
                try:
                    # Atomar wie das Dashboard: ein Abruf waehrend des
                    # Schreibens saehe sonst eine halbe Datei.
                    api_tmp = f"{api_file}.{os.getpid()}.tmp"
                    display.reset_index().rename(columns={"index": "timestamp"}).to_json(
                        api_tmp, orient="records", date_format="iso"
                    )
                    os.replace(api_tmp, api_file)
                except Exception as exc:
                    log.warning("Fehler beim Schreiben von %s: %s", api_file, exc)
        # Das teure ML-Schattenmodell bewusst erst nach erfolgreicher
        # Optimierung starten. Es läuft während der Wartezeit im Hintergrund
        # und kann dadurch weder den aktuellen Sollplan noch die Solverzeit
        # verzögern.
        try:
            load_models.start_pending_ml_training()
        except Exception as exc:
            log.debug("ML-Lastschattenstart fehlgeschlagen (%s).", exc)
        _start_shadow_comparison(config, inputs, result, violations)
        _maybe_snapshot_forecast_accuracy(config)   # 1×/Tag Trend-Punkt sichern
        _runtime_finish(now, runtime_started)
        # Jede abgeschlossene Neuberechnung ins Ereignis-Log (nicht nur manuelle),
        # mit dem aktuell gefahrenen Steuermodus als Kurzinfo.
        _cur_mode = (str(result.table.iloc[0].get("mode", "auto"))
                     if getattr(result, "table", None) is not None
                     and len(result.table) else "auto")
        _record_dashboard_event(
            config, "recalc",
            ("Plan manuell neu berechnet" if runtime_trigger == "manual"
             else "Plan neu berechnet") + f" · Modus {_cur_mode}",
            details={"trigger": runtime_trigger, "mode": _cur_mode,
                     "duration_seconds": _runtime_snapshot().get("duration_seconds"),
                     "solver_seconds": getattr(result, "solver_seconds", None),
                     "violations": len(violations or [])})
    except Exception as exc:
        _runtime_fail(f"EMS-Zyklus fehlgeschlagen: {exc}")
        _record_dashboard_event(config, "error", f"EMS-Zyklus fehlgeschlagen: {exc}",
                                level="error")
        raise
    finally:
        if repo is not None:
            repo.close()
        if own_e3dc and e3dc is not None:
            e3dc.close()


_intraday_hours_cache: dict = {}


def _intraday_hour_ratios(config, now) -> dict:
    """Restverhaeltnis je Tagesstunde aus den Vortagen (regimegleiches Fenster).

    Einmal je Zyklus lesen: der Wert wird an mehreren Stellen gebraucht (Haupt-
    prognose und die archivierten/angezeigten Reihen), und die Abfrage geht
    ueber die schon vorhandenen Intraday-Fenster.
    """
    tage = float(getattr(config.forecast, "intraday_load_sameslot_days", 0.0))
    if tage <= 0:
        return {}
    schluessel = (str(pd.Timestamp(now)), tage)
    if schluessel in _intraday_hours_cache:
        return _intraday_hours_cache[schluessel]
    try:
        from .local_history import read_intraday_hour_ratios
        werte = read_intraday_hour_ratios(
            config.e3dc_rscp.history_db_path, "load", now, tage,
            config.general.timezone,
            max_factor=config.forecast.intraday_load_max_factor)
    except Exception as exc:            # pragma: no cover
        log.debug("Regimegleiches Fenster nicht lesbar (%s).", exc)
        werte = {}
    _intraday_hours_cache.clear()       # nur der aktuelle Zyklus
    _intraday_hours_cache[schluessel] = werte
    return werte


def _intraday_ratios(repo, config, forecaster, history, temp, now, hist_pv=None,
                     load_actual=None):
    """Ist/Prognose-Verhältnisse der letzten Stunden (Last, PV) für die
    Intraday-Korrektur. None = keine Korrektur (zu wenig Daten, PV-Nacht, aus)."""
    fc = config.forecast
    if not fc.intraday_enabled:
        return None, None
    load_start = now - timedelta(hours=fc.intraday_load_window_hours)
    pv_start = now - timedelta(hours=fc.intraday_pv_window_hours)
    load_ratio = pv_ratio = None

    def stabilize(signal, ratio, deadband, max_step):
        state = _intraday_state[signal]
        issue = pd.Timestamp(now)
        if state["issue"] == issue:
            return state["applied"]
        # Kaltstart (erster Lauf nach Prozessstart): direkt vom Rohwert seeden,
        # OHNE max_step-Rampe von 1.0. Sonst wäre die Verbrauchskurve nach jedem
        # Neustart unter-korrigiert (z.B. x1.10 statt x1.17) und würde erst über
        # mehrere Zyklen hochrampen -> sichtbarer Sprung bei jedem Restart. Der
        # Rohwert stammt aus den letzten Stunden Ist-Daten, ist also sofort
        # belastbar; max_step glättet nur die Übergänge ZWISCHEN Live-Zyklen.
        step = 0.0 if not state["seeded"] else max_step
        applied = stabilize_intraday_ratio(
            ratio, state["ratio"], deadband=deadband, max_step=step)
        state["issue"] = issue
        state["applied"] = applied
        if applied is not None:
            state["ratio"] = applied
            state["seeded"] = True
        return applied

    def archive(signal, start, details, applied):
        try:
            from .local_history import write_intraday_diagnostic
            write_intraday_diagnostic(
                config.e3dc_rscp.history_db_path, now, signal, start,
                details, applied)
        except Exception as exc:  # Diagnose darf den EMS-Lauf nie stoppen
            log.warning("Intraday-Diagnose %s nicht speicherbar (%s).",
                        signal, exc)

    try:
        act = (pd.Series(load_actual).loc[load_start:now].dropna()
               if load_actual is not None else pd.Series(dtype="float64"))
        if not config.forecast.live_nowcast_enabled:
            act = read_actual_signal(
                config, repo, "house_consumption", load_start, now)
        else:
            try:
                from .local_history import (read_controllable_load_power,
                                            read_live_slot_averages)
                live_slots = read_live_slot_averages(
                    config.e3dc_rscp.history_db_path, load_start, now,
                    config.general.timezone, config.general.slot_minutes,
                    config.forecast.live_nowcast_min_coverage_seconds,
                    config.forecast.live_nowcast_max_gap_seconds)
                live_house = (live_slots["house_w"].dropna()
                              if "house_w" in live_slots else
                              pd.Series(dtype="float64"))
                if (config.forecast.disaggregate_controllable_loads
                        and not live_house.empty):
                    measured, complete, labels = read_controllable_load_power(
                        config.e3dc_rscp.history_db_path,
                        config.controllable_loads, load_start, now,
                        config.general.timezone, config.general.slot_minutes)
                    if labels:
                        aligned = measured.reindex(live_house.index)
                        valid = complete.reindex(
                            live_house.index).fillna(False) & aligned.notna()
                        live_house = (live_house - aligned).clip(
                            lower=0.0).where(valid).dropna()
                if not live_house.empty:
                    act = pd.concat([act, live_house])
                    act = act[~act.index.duplicated(keep="last")].sort_index()
            except Exception as exc:
                log.debug("Live-Nowcast Last nicht lesbar (%s).", exc)
        if act.empty:
            act = read_actual_signal(
                config, repo, "house_consumption", load_start, now)
        # Prognose für das Fenster aus der Historie DAVOR (sonst fließen die
        # Ist-Werte des Fensters in ihre eigene Prognose ein).
        hist_before = history[history.index < load_start]
        intraday_slots = max(1, int(np.ceil(
            (pd.Timestamp(now) - pd.Timestamp(load_start)).total_seconds()
            / (config.general.slot_minutes * 60.0))))
        pred = forecaster.forecast(hist_before, load_start, intraday_slots,
                                   hist_temp=temp, fut_temp=temp,
                                   hist_pv=hist_pv, fut_pv=hist_pv)
        clipped, details = intraday_ratio(
            act, pred.reindex(act.index), min_mean=50.0,
            max_factor=fc.intraday_load_max_factor, robust=True,
            return_details=True)
        _intraday_raw["load"] = details.get("raw_ratio")
        load_ratio = stabilize(
            "load", clipped, fc.intraday_load_deadband,
            fc.intraday_load_max_step)
        archive("load", load_start, details, load_ratio)
    except Exception as exc:
        log.warning("Intraday-Korrektur Last fehlgeschlagen (%s).", exc, exc_info=True)
    try:
        if config.e3dc_rscp.history_source or repo.signal_available("pv_generation"):
            act_pv = read_actual_signal(config, repo, "pv_generation", pv_start, now)
            pred_pv = solcast.read_pv_signal(config, repo, "pv_forecast", pv_start, now)
            if config.calibration.enabled:
                from .calibration import (apply_pv_correction, load_profile,
                                          pv_profile_for_source)
                prof = pv_profile_for_source(
                    load_profile(config.calibration.pv_profile),
                    solcast.selected_source(config),
                    "solcast" if config.solcast.enabled else "pvlib")
                if prof:
                    pred_pv = apply_pv_correction(pred_pv, prof,
                                                  config.general.timezone)
            # Nur stabile Tagesleistung: Dämmerungsrampen und kurze Wolken
            # dürfen die Mittagsprognose nicht über Stunden verzerren.
            clipped, details = intraday_ratio(
                act_pv, pred_pv.reindex(act_pv.index), min_mean=200.0,
                max_factor=fc.intraday_pv_max_factor, robust=True,
                min_slot_value=fc.intraday_pv_min_power_w,
                return_details=True)
            _intraday_raw["pv"] = details.get("raw_ratio")
            pv_ratio = stabilize(
                "pv", clipped, fc.intraday_pv_deadband,
                fc.intraday_pv_max_step)
            archive("pv", pv_start, details, pv_ratio)
    except Exception as exc:
        log.warning("Intraday-Korrektur PV fehlgeschlagen (%s).", exc)
    return load_ratio, pv_ratio


_last_weather_fetch = 0.0
_last_grid_weather_fetch = 0.0
_last_efficiency_check = 0.0
_last_load_bias = None
_last_monitor_diagnostics = {"efficiency": None, "execution_bias": None}
_last_price_fill = None      # womit _price_series zuletzt Preise gefüllt hat
_price_model = None          # zuletzt FERTIG trainiertes Preismodell
_price_model_day = None      # Tag, für den zuletzt ein Training begonnen hat
_price_model_thread = None   # laufendes Hintergrund-Training
_price_model_lock = threading.Lock()
# Nur der Dauerbetrieb lernt im Hintergrund (main() setzt das). Ein Einzellauf
# wartet wie bisher - er wäre sonst vor dem Ende des Trainings vorbei.
_price_model_async = False
# OpenMP-Threads des Hintergrund-Trainings. Gemessen am 23.10.2026 auf dem Pi:
# 1, 2 und 4 Threads gleich schnell (25 bis 40 s, Streuung größer als jeder
# Unterschied) - einer nimmt dem laufenden Zyklus aber am wenigsten weg.
_PRICE_MODEL_THREADS = 1


def _refresh_weather_cache(config) -> None:
    """Holt Temperatur + Solar-Einstrahlung von Open-Meteo (EIN HTTP-Call für
    BEIDE Signale, kein Mehraufwand) und cacht sie lokal. Höchstens alle ~5 min
    pro Zyklus (von _read_temp UND _read_solar aufgerufen – der zweite Aufruf
    greift innerhalb desselben Zyklus einfach den frischen Cache)."""
    if not config.weather.enabled:
        return
    global _last_weather_fetch
    if _time.time() - _last_weather_fetch <= 300:
        return
    w, db = config.weather, config.e3dc_rscp.history_db_path
    try:
        from .weather import fetch_forecast
        from .local_history import (write_radiation, write_temperature,
                                    write_weather_forecast_archive)
        temp_map, rad_map = fetch_forecast(
            config.general.latitude, config.general.longitude,
                                           w.past_days, w.forecast_days)
        issue_time = pd.Timestamp.now(tz="UTC")
        n_t = write_temperature(db, temp_map)
        n_r = write_radiation(db, rad_map)
        n_a = write_weather_forecast_archive(
            db, issue_time, temp_map, rad_map)
        _last_weather_fetch = _time.time()
        log.info("Open-Meteo: %d Temperatur- + %d Strahlungs-Stundenwerte "
                 "aktualisiert; %d Zukunftszeitpunkte archiviert.",
                 n_t, n_r, n_a)
    except Exception as exc:
        log.warning("Open-Meteo-Abruf fehlgeschlagen (%s) – nutze Cache.", exc)


def _refresh_grid_weather(config) -> None:
    """Deutschlandweite Wetter-Indizes (Treiber des Börsenpreises) auffrischen.

    EIN Open-Meteo-Call für alle Stützpunkte, höchstens alle ~30 min - die
    Indizes sind Stundenwerte, häufigeres Abrufen bringt nichts. Fehler sind
    unkritisch: ohne frische Werte bleibt es bei der bisherigen Schätzung.
    """
    global _last_grid_weather_fetch
    if not getattr(config.forecast, "price_model_enabled", True):
        return
    if _time.time() - _last_grid_weather_fetch <= 1800:
        return
    try:
        from .gridweather import as_mapping, fetch_forecast
        from .local_history import write_grid_weather
        n = write_grid_weather(
            config.e3dc_rscp.history_db_path,
            as_mapping(fetch_forecast()))
        _last_grid_weather_fetch = _time.time()
        log.info("Open-Meteo: %d deutschlandweite Wetter-Indizes für die "
                 "Preisprognose aktualisiert.", n)
    except Exception as exc:
        log.warning("Deutschlandweite Wetter-Indizes nicht abrufbar (%s) – "
                    "Preisschätzung bleibt bei der %s.", exc,
                    baseline_name(config))


def _price_model_for(config):
    """Gelerntes Preismodell, höchstens einmal je Kalendertag neu trainiert.

    Im Dauerbetrieb lernt es IM HINTERGRUND, der Zyklus wartet nie darauf: bis
    das neue Modell fertig ist, gilt das vorige - nach einem Neustart die
    bisherige Schätzung. Vom 10. bis 23.10.2026 lief das tägliche Training um
    Mitternacht in den Genetik-Lauf von EOS (alle 15 min, auch um 00:01) und
    dauerte dann 2,5 bis 19 Minuten statt rund 30 s; so lange kam kein
    Steuerbefehl. Am 23.10. traf es ebenso den ersten Zyklus nach einem Neustart.
    Weniger Threads halfen nicht (neben EOS brauchte auch einer über 100 s).

    None, wenn kein einsatzbereites Modell vorliegt; der Aufrufer bleibt dann
    beim bisherigen Schätzer.
    """
    global _price_model_day, _price_model_thread
    if not getattr(config.forecast, "price_model_enabled", True):
        return None
    today = pd.Timestamp.now(tz=config.general.timezone).date()
    with _price_model_lock:
        faellig = (_price_model_day != today
                   and (_price_model_thread is None
                        or not _price_model_thread.is_alive()))
        if faellig:
            _price_model_day = today
            if _price_model_async:
                _price_model_thread = threading.Thread(
                    target=_train_price_model, args=(config,),
                    name="ems-price-model", daemon=True)
                _price_model_thread.start()
    if faellig and not _price_model_async:
        _train_price_model(config)
    with _price_model_lock:
        model = _price_model
    return model if model is not None and model.model is not None else None


def _train_price_model(config) -> None:
    """Preismodell lernen und erst FERTIG einsetzen. Scheitert das Training,
    bleibt das vorige Modell in Gebrauch."""
    global _price_model
    started = _time.monotonic()
    try:
        from threadpoolctl import threadpool_limits

        from .local_history import read_grid_weather, read_spot
        from .priceforecast import PriceModel
        db = config.e3dc_rscp.history_db_path
        tz = config.general.timezone
        now = pd.Timestamp.now(tz=tz)
        start = now.normalize() - pd.Timedelta(days=540)
        spot = read_spot(db, start, now, tz, config.general.slot_minutes)
        weather = read_grid_weather(db, start, now, tz)
        model = PriceModel(config)
        # Gilt nur für DIESEN Thread (OpenMP-Einstellung je Thread).
        with threadpool_limits(limits=_PRICE_MODEL_THREADS, user_api="openmp"):
            model.fit(spot, weather,
                      holdout_days=int(config.forecast.price_model_holdout_days),
                      min_train_days=int(config.forecast.price_model_min_train_days))
        with _price_model_lock:
            _price_model = model
        log.info("Preismodell: %s (Training %.0f s%s)", model.reason,
                 _time.monotonic() - started,
                 ", im Hintergrund" if _price_model_async else "")
    except Exception as exc:
        with _price_model_lock:
            bisher = _price_model is not None and _price_model.model is not None
        log.warning("Preismodell nicht trainierbar (%s) – %s.", exc,
                    "das bisherige bleibt in Gebrauch" if bisher
                    else f"{baseline_name(config)} bleibt aktiv")


def _read_temp(repo, config, start, end):
    """Temperatur fürs Ähnlichkeits-Gewicht. Quelle: Open-Meteo (lokaler
    SQLite-Cache) wenn weather.enabled, sonst InfluxDB. None wenn nicht da."""
    if config.weather.enabled:
        _refresh_weather_cache(config)
        db = config.e3dc_rscp.history_db_path
        freq = f"{config.general.slot_minutes}min"
        try:
            from .local_history import read_temperature
            s = read_temperature(db, start, end, config.general.timezone, freq)
            return s if not s.empty else None
        except Exception as exc:  # pragma: no cover
            log.warning("Lokale Temperatur nicht lesbar (%s).", exc)
            return None
    if not repo.signal_available("temperature"):
        return None
    try:
        return repo.read_slots("temperature", start, end, fill=False)
    except Exception as exc:  # pragma: no cover
        log.warning("Temperatur konnte nicht gelesen werden (%s).", exc)
        return None


def _read_solar(config, start, end):
    """Solar-Einstrahlung (W/m² Globalstrahlung) für den solaren Wärmeeintrag
    thermischer Lasten (Pool). Nur Open-Meteo – kein InfluxDB-Äquivalent
    vorhanden. None wenn weather.enabled=false oder nichts gecacht ist."""
    if not config.weather.enabled:
        return None
    _refresh_weather_cache(config)
    try:
        from .local_history import read_radiation
        freq = f"{config.general.slot_minutes}min"
        s = read_radiation(config.e3dc_rscp.history_db_path, start, end,
                           config.general.timezone, freq)
        return s if not s.empty else None
    except Exception as exc:  # pragma: no cover
        log.warning("Solar-Einstrahlung nicht lesbar (%s).", exc)
        return None


_last_spot_fetch = 0.0


def _refresh_spot(config):
    """Day-Ahead-Spotpreis von Energy-Charts, bei Ausfall SMARD, lokal cachen (throttled,
    höchstens einmal je ~5 min). Holt jüngste Historie + Folgetag (Day-Ahead),
    damit der Optimierungshorizont echte Preise hat. Nur bei dynamischem Tarif."""
    if not (config.tariff.enabled and config.tariff.type == "dynamic"):
        return
    global _last_spot_fetch
    if _time.time() - _last_spot_fetch <= 300:
        return
    # Auch Ausfälle drosseln, damit mehrere Aufrufer nicht erneut warten.
    _last_spot_fetch = _time.time()
    try:
        from .energycharts import fetch_spot
        from .local_history import write_spot
        today = pd.Timestamp.now(tz=config.general.timezone).normalize()
        start = (today - pd.DateOffset(days=3)).date().isoformat()
        end = (today + pd.DateOffset(days=2)).date().isoformat()
        source = "Energy-Charts"
        try:
            prices = fetch_spot(config.tariff.bidding_zone, start, end)
            if not prices:
                raise ValueError("keine Spot-Preiswerte geliefert")
        except Exception as exc:
            if config.tariff.bidding_zone != "DE-LU":
                raise
            log.warning("Energy-Charts-Abruf fehlgeschlagen (%s) – versuche SMARD.", exc)
            from .smard import fetch_spot as fetch_smard
            source = "SMARD"
            prices = fetch_smard(config.tariff.bidding_zone, start, end)
            if not prices:
                raise ValueError("SMARD: keine vollständigen Spot-Preistage geliefert")
        n = write_spot(config.e3dc_rscp.history_db_path, prices)
        log.info("%s: %d Spot-Preiswerte aktualisiert.", source, n)
    except Exception as exc:
        log.warning("Spotpreis-Aktualisierung fehlgeschlagen (%s) – nutze Cache.", exc)


def _forecast_quality_entry(name: str, total_slots: int, replaced_slots: int,
                            replacement: str, issued_at=None,
                            note: str | None = None,
                            level_override: str | None = None,
                            state_override: str | None = None) -> dict:
    """Einheitliche Qualitätsstufe für eine operative Prognosereihe.

    level_override/state_override erlauben eine abweichende Ampel-Logik (z.B.
    Strompreis: der ferne Horizont ist immer geschätzt -> die reine
    Ersatzquote wäre dauerhaft gelb; stattdessen am nächsten Tag ausrichten)."""
    total = max(0, int(total_slots))
    replaced = min(total, max(0, int(replaced_slots)))
    available = total - replaced
    if total == 0 or replaced >= total:
        state, level = "vollständig ersetzt", "replaced"
        detail = f"alle {total} Slots durch {replacement}"
    elif replaced > 0:
        state, level = "teilweise ergänzt", "partial"
        detail = f"{replaced} von {total} Slots durch {replacement}"
    else:
        state, level = "aktuell", "current"
        detail = f"{available} von {total} Slots aus der Prognosequelle"
    if level_override:
        level = level_override
        if state_override:
            state = state_override
    if note:
        detail += f" · {note}"
    issue = None
    if issued_at is not None:
        try:
            issue = pd.Timestamp(issued_at).isoformat()
        except Exception:
            issue = str(issued_at)
    return {"name": name, "state": state, "level": level,
            "detail": detail, "available_slots": available,
            "replaced_slots": replaced, "total_slots": total,
            "issued_at": issue}


def _source_status(config, now):
    """Frische der externen Datenquellen fürs Dashboard (Ampel je Quelle).
    Fällt eine API aus, läuft das EMS still auf Cache/Schätzung weiter -
    genau das soll sichtbar werden, statt unbemerkt tagelang auf alten
    Daten zu optimieren."""
    out = []
    now = floor_time(now, "15min")
    tz = config.general.timezone
    # Spotpreis (Energy-Charts): bis wann liegen ECHTE Preise vor? Erwartung:
    # vormittags bis Ende heute; nach der Day-Ahead-Auktion (~14 Uhr) sollte
    # der Folgetag da sein. Danach ergänzt die Preisschätzung.
    try:
        if config.tariff.enabled and config.tariff.type == "dynamic":
            from .local_history import read_spot
            s = read_spot(config.e3dc_rscp.history_db_path,
                          now - pd.Timedelta(hours=2),
                          now + pd.Timedelta(hours=48), tz)
            sd = s.dropna()
            last = sd.index[-1] if len(sd) else None
            expected = (now.normalize()
                        + pd.DateOffset(days=2 if now.hour >= 15 else 1)
                        - pd.Timedelta(minutes=15))
            if last is None or last < now:
                lvl, det = "err", ("keine Daten" if last is None else
                                   f"veraltet ({last.strftime('%d.%m. %H:%M')})")
            elif last < expected - pd.Timedelta(hours=1):
                lvl, det = "warn", f"echt bis {last.strftime('%d.%m. %H:%M')}"
            else:
                lvl, det = "ok", f"echt bis {last.strftime('%d.%m. %H:%M')}"
            out.append({"name": "Spotpreis", "level": lvl, "detail": det})
    except Exception as exc:  # pragma: no cover
        log.debug("Quellen-Status Preis fehlgeschlagen: %s", exc)
    # Wetter (Open-Meteo): Alter des letzten erfolgreichen Abrufs (Prozess).
    try:
        if config.weather.enabled:
            if _last_weather_fetch <= 0:
                out.append({"name": "Wetter", "level": "err",
                            "detail": "noch kein Abruf"})
            else:
                age_min = (_time.time() - _last_weather_fetch) / 60.0
                lvl = "ok" if age_min < 120 else ("warn" if age_min < 720 else "err")
                out.append({"name": "Wetter", "level": lvl,
                            "detail": f"vor {age_min:.0f} min"})
    except Exception as exc:  # pragma: no cover
        log.debug("Quellen-Status Wetter fehlgeschlagen: %s", exc)
    # Solcast: letzter erfolgreicher Abruf laut solcast_log (überlebt Neustarts).
    try:
        if config.solcast.enabled:
            import sqlite3
            con = sqlite3.connect(config.e3dc_rscp.history_db_path)
            row = con.execute("SELECT MAX(ts) FROM solcast_log").fetchone()
            con.close()
            if not row or not row[0]:
                out.append({"name": "Solcast", "level": "err",
                            "detail": "noch kein Abruf"})
            else:
                last = pd.Timestamp(row[0]).tz_convert(tz)
                age_h = (now - last).total_seconds() / 3600.0
                lvl = "ok" if age_h < 5 else ("warn" if age_h < 12 else "err")
                out.append({"name": "Solcast", "level": lvl,
                            "detail": f"vor {age_h:.1f} h"})
    except Exception as exc:  # pragma: no cover
        log.debug("Quellen-Status Solcast fehlgeschlagen: %s", exc)
    # E3DC live (RSCP/Ingest): letzter protokollierter Live-Snapshot (actuals,
    # wird jeden Zyklus geschrieben) - veraltet = Live-Auslesen klemmt.
    # Hauslast-Historie: letztes 15-min-Fenster aus der E3DC-DB (Basis der
    # Verbrauchsprognose); hängt normal 15-30 min hinter "jetzt".
    try:
        if config.e3dc_rscp.history_source:
            import sqlite3
            con = sqlite3.connect(config.e3dc_rscp.history_db_path)
            row_a = con.execute("SELECT MAX(ts) FROM actuals").fetchone()
            row_h = con.execute("SELECT MAX(ts) FROM house_load").fetchone()
            con.close()
            for label, row, lim_ok, lim_warn in (
                    ("E3DC live", row_a, 0.5, 2.0),
                    ("Hauslast-Historie", row_h, 2.0, 12.0)):
                if not row or not row[0]:
                    out.append({"name": label, "level": "err",
                                "detail": "keine Daten"})
                    continue
                last = pd.Timestamp(row[0]).tz_convert(tz)
                age_h = (now - last).total_seconds() / 3600.0
                lvl = "ok" if age_h < lim_ok else ("warn" if age_h < lim_warn
                                                   else "err")
                det = (f"vor {age_h * 60:.0f} min" if age_h < 2.0
                       else f"vor {age_h:.1f} h")
                out.append({"name": label, "level": lvl, "detail": det})
    except Exception as exc:  # pragma: no cover
        log.debug("Quellen-Status E3DC fehlgeschlagen: %s", exc)
    return out


def _read_load_state(config, publisher):
    """Ist-Temperatur thermischer steuerbarer Lasten (für T[0] im Optimierer),
    aus dem MQTT-Cache des Publishers (temp_signal = MQTT-Topic). None, wenn noch
    kein Wert empfangen -> der Optimierer startet dann von target_c."""
    if publisher is None:
        return None
    st = {}
    for ld in getattr(config, "controllable_loads", []):
        if ld.enabled and ld.type == "thermal" and ld.temp_signal:
            # Mit Altersgrenze (wie die Heiz-Freigabe): ein verstummter Sensor
            # darf den Plan nicht dauerhaft auf seinem letzten Wert starten
            # lassen. Ohne frischen Wert startet der Optimierer von target_c.
            t = publisher.get_load_temp(
                ld.temp_signal,
                max_age_minutes=getattr(ld, "temp_max_age_minutes", None))
            if t is not None and np.isfinite(float(t)):
                st[ld.name] = float(t)
            elif publisher.get_load_temp(ld.temp_signal) is not None:
                log.warning(
                    "Temperatur von %s ist aelter als %s min - der Plan "
                    "startet von der Solltemperatur.", ld.name,
                    getattr(ld, "temp_max_age_minutes", "?"))
    return st or None


def _reload_calibration_overrides(config) -> None:
    """Extern kalibrierte Modellwerte ohne Neustart übernehmen."""
    path = getattr(config, "_source_path", None)
    if not path:
        return
    from .config import _overrides_path, load_config
    try:
        mtime = os.path.getmtime(_overrides_path(path))
    except OSError:
        mtime = 0.0
    if mtime <= getattr(config, "_overrides_mtime", 0.0):
        return
    fresh = load_config(path)
    changes = []
    for field in ("p10_uncertainty", "p90_uncertainty"):
        old, new = getattr(config.pv_model, field), getattr(fresh.pv_model, field)
        if old != new:
            setattr(config.pv_model, field, new)
            changes.append(f"pv_model.{field}={new}")
    old_eta = config.house_battery.discharge_efficiency
    new_eta = fresh.house_battery.discharge_efficiency
    if old_eta != new_eta:
        config.house_battery.discharge_efficiency = new_eta
        changes.append(f"house_battery.discharge_efficiency={new_eta}")
    by_name = {ld.name: ld for ld in fresh.controllable_loads}
    for current in config.controllable_loads:
        newer = by_name.get(current.name)
        if newer is None or current.type != newer.type:
            continue
        if current.type == "deferrable":
            for field in ("power_profile_w", "runtime_minutes"):
                old, new = getattr(current, field), getattr(newer, field)
                if old != new:
                    if field == "power_profile_w" and new is not None:
                        new = list(new)
                    setattr(current, field, new)
                    changes.append(f"{current.name}.{field}={new}")
            continue
        if current.type != "thermal":
            continue
        # comfort_penalty_ct_per_k_slot gehoert dazu: der Wert ist zum Drehen
        # gedacht (er entscheidet, ob Komfort oder Strompreis gewinnt), und ohne
        # Hot-Reload braeuchte jede Probe einen Dienstneustart.
        for field in ("loss_w_per_k", "solar_absorption",
                      "comfort_penalty_ct_per_k_slot"):
            old, new = getattr(current, field), getattr(newer, field)
            if old != new:
                setattr(current, field, new)
                changes.append(f"{current.name}.{field}={new}")
        stages = {stage.name: stage for stage in newer.stages}
        for stage in current.stages:
            if stage.name in stages and stage.heat_w != stages[stage.name].heat_w:
                stage.heat_w = stages[stage.name].heat_w
                changes.append(f"{current.name}/{stage.name}.heat_w={stage.heat_w}")
    config._overrides_mtime = mtime
    if changes:
        log.info("Kalibrierungswerte automatisch übernommen: %s",
                 ", ".join(changes))


def _read_load_feedback(config, publisher, now):
    """Echte Zustände aller rückgekoppelten Lasten lesen und archivieren."""
    state, statuses = {}, []
    if publisher is None:
        return None, statuses
    from .local_history import write_load_feedback
    for ld in getattr(config, "controllable_loads", []):
        feedback_lanes = (
            [(stage.name, f"{ld.name}/{stage.name}",
              stage.feedback_topic, stage.power_topic)
             for stage in ld.stages]
            if ld.type == "thermal" else
            [("__load__", ld.name, ld.feedback_topic, ld.power_topic)])
        for stage_name, label, state_topic, power_topic in feedback_lanes:
            configured = bool(state_topic or power_topic)
            if not configured and not ld.feedback_required:
                continue
            feedback = publisher.get_load_feedback(
                label, ld.feedback_max_age_minutes,
                getattr(ld, "feedback_hold_while_connected", False)
            ) if configured else None
            item = {"label": label, "name": ld.name, "stage": stage_name,
                    "configured": configured,
                    "required": bool(ld.feedback_required),
                    "fresh": bool(feedback and feedback.get("fresh")),
                    "held": bool(feedback and feedback.get("held")),
                    "on": feedback.get("on") if feedback else None,
                    "power_w": feedback.get("power_w") if feedback else None,
                    "age_seconds": (feedback.get("age_seconds")
                                    if feedback else None)}
            statuses.append(item)
            if feedback is not None:
                try:
                    write_load_feedback(config.e3dc_rscp.history_db_path, now,
                                        ld.name, stage_name, feedback)
                except Exception as exc:
                    log.debug("Last-Rückmeldung nicht speicherbar (%s).", exc)
            if item["fresh"] and item["on"] is not None:
                state[label] = bool(item["on"])
    return state or None, statuses


def _read_load_run_state(config, now):
    """Publizierte Laufdauer thermischer Stufen aus SQLite wiederherstellen."""
    from .local_history import read_load_stage_run_state

    out = {}
    for ld in getattr(config, "controllable_loads", []):
        if ld.type != "thermal" or not ld.enabled:
            continue
        for stage in ld.stages:
            state = read_load_stage_run_state(
                config.e3dc_rscp.history_db_path, ld.name, stage.name, now,
                config.general.slot_minutes)
            if state is not None:
                out[f"{ld.name}/{stage.name}"] = state
    return out or None


def _read_grid_charge_run_wh(config, now) -> float:
    """Netzseitige Energie des Netzladeblocks, der bis zum aktuellen Slot lief."""
    from .local_history import read_grid_charge_run_wh

    wh = read_grid_charge_run_wh(config.e3dc_rscp.history_db_path, now,
                                 config.general.slot_minutes)
    if wh > 0.0:
        log.info("Netzladeblock bis Slotbeginn: %.2f kWh aus dem Netz "
                 "befohlen (zählt auf die Mindestmenge).", wh / 1000.0)
    return wh


def _price_series(repo, config, index, now, return_estimated=False):
    """Strompreis über `index`: Ist-Werte wo vorhanden, sonst eine Schätzung für
    noch fehlende (Folgetag-)Preise – statt einer flachen ffill-Linie. Welches
    Verfahren gefüllt hat, steht danach in ``_last_price_fill``.
    Preise dürfen negativ sein (clip_min=None).
    return_estimated=True: zusätzlich Bool-Maske, welche Slots geschätzt sind.
    """
    slot = pd.Timedelta(f"{config.general.slot_minutes}min")
    raw = read_price_signal(config, repo, index[0], index[-1] + slot).reindex(index)
    estimated = raw.isna()   # Slots ohne echten Börsenpreis -> Schätzung
    hist = pd.Series(dtype="float64")
    global _last_price_fill
    verfahren = []
    if estimated.any():
        # 1) Deutschlandweites Residuallast-Modell: der Day-Ahead folgt der
        #    Merit-Order über Last minus Wind/Solar. Nur wenn es sich gegen die
        #    bisherige Schätzung bewiesen hat (siehe ems/priceforecast.py).
        try:
            _refresh_grid_weather(config)
            model = _price_model_for(config)
            if model is not None:
                from .local_history import read_grid_weather
                from .tariff import read_spot_signal
                weather = read_grid_weather(
                    config.e3dc_rscp.history_db_path, index[0],
                    index[-1] + slot, config.general.timezone)
                # Bis zum Horizontende lesen: der heutige Tag ist laengst
                # vollstaendig veroeffentlicht und ist - wie im Training - der
                # Anker fuer morgen. predict nimmt den Anker nie aus dem
                # Prognosetag selbst.
                spot_hist = read_spot_signal(
                    config, repo, now - timedelta(days=3),
                    index[-1] + slot).dropna()
                guess = model.predict(index[estimated], weather, spot_hist)
                if guess is not None:
                    # Das Modell schätzt den BÖRSENPREIS - für den Optimierer
                    # muss das Tarifmodell darüber (Aufschläge, Netz, MwSt).
                    from .tariff import apply_tariff
                    raw = raw.fillna(apply_tariff(guess, config).reindex(index))
                    verfahren.append("Residuallast-Modell")
                    log.info("Fehlende Folgetag-Preise per Residuallast-Modell "
                             "ergänzt (%d Slots).", int(estimated.sum()))
        except Exception as exc:  # pragma: no cover
            log.warning("Residuallast-Preismodell nicht anwendbar (%s).", exc)
        # 2) Rückfall: LoadForecaster auf der eigenen Preishistorie - nach
        #    forecast.method die Ähnliche-Tage-Mittelung oder die ML-Prognose.
        try:
            hist = read_price_signal(config, repo, now - timedelta(days=90), now).dropna()
            if raw.isna().any() and not hist.empty:
                fc = LoadForecaster(config).forecast(
                    hist, index[0], len(index), clip_min=None, apply_correction=False)
                raw = raw.fillna(fc.reindex(index))
                verfahren.append(baseline_name(config))
                log.info("Fehlende Folgetag-Preise per %s ergänzt.",
                         baseline_name(config))
        except Exception as exc:  # pragma: no cover
            log.warning("Preis-Prognose fehlgeschlagen (%s).", exc)
    # Auch bei komplett ausgefallener Preisquelle bleibt der Optimierer
    # lauffähig. Median der echten Historie, sonst konfigurierter Fixpreis.
    remaining = raw.isna()
    if remaining.any():
        verfahren.append("Medianpreis" if not hist.empty else "Festpreis")
        fallback = (float(hist.median()) if not hist.empty else
                    float(config.tariff.fixed_ct_kwh)
                    if config.tariff.enabled else 30.0)
        raw = raw.fillna(fallback)
        log.warning("Strompreis: %d Slots ohne Daten/Schätzung durch %.2f ct/kWh "
                    "ersetzt.", int(remaining.sum()), fallback)
    price = raw
    # Unsicherheits-Dämpfung: geschätzte Slots zur Mitte stauchen, damit auf
    # Phantom-Preistäler/-spitzen nicht spekuliert wird.
    price = dampen_estimated(price, estimated, config.forecast.price_damping)
    _last_price_fill = " + ".join(verfahren) or None
    if return_estimated:
        return price, estimated
    return price


def _sd_notify(message: str) -> None:
    """Meldung an systemd (Type=notify / WatchdogSec); no-op ohne systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        import socket
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(message.encode(), addr)
        finally:
            s.close()
    except Exception as exc:  # pragma: no cover
        log.debug("systemd-Notify (%s) nicht absetzbar: %s", message, exc)


def _apply_system_limits(config: Config, lim: dict) -> None:
    """Vom E3DC gelesene Anlagengrenzen in die Config übernehmen (überschreibt
    die konfigurierten Werte). Kapazität/SoC bleiben unberührt."""
    if not lim:
        return
    changes = []
    # BAT_SPECIFIED_CAPACITY ist die nominelle Herstellerkapazität. Für die
    # SoC-Bilanz wird dagegen die konfigurierte effektive Kapazität verwendet;
    # sie darf beim Start nicht durch den Nominalwert überschrieben werden.
    if "inverter_max_ac_power_w" in lim:
        config.inverter.max_ac_power_w = lim["inverter_max_ac_power_w"]
        changes.append(f"WR max_ac={lim['inverter_max_ac_power_w']:.0f} W")
    if "max_charge_w" in lim:
        config.house_battery.max_charge_w = lim["max_charge_w"]
        changes.append(f"max_charge={lim['max_charge_w']:.0f} W")
    if "max_discharge_w" in lim:
        config.house_battery.max_discharge_w = lim["max_discharge_w"]
        changes.append(f"max_discharge={lim['max_discharge_w']:.0f} W")
    if "min_discharge_w" in lim:
        config.optimization.min_discharge_w = lim["min_discharge_w"]
        changes.append(f"min_discharge={lim['min_discharge_w']:.0f} W")
    if changes:
        log.info("Anlagengrenzen vom E3DC übernommen: %s", ", ".join(changes))


def check_config(config) -> dict:
    """Trockenlauf: die (bereits von load_config validierte) Konfiguration mit
    EINEM Optimierer-Solve auf konservativen Fallback-Eingaben prüfen – ganz
    OHNE MQTT/RSCP/Dashboard/Netzwerk. Fängt Konfigurationsfehler, die das
    Modell unzulässig machen (falsche Grenzen, unerfüllbare Lastvorgaben), vor
    dem Deploy ab. Rückgabe: Report-Dict inkl. ok/infeasible/Status."""
    now = _now_slot(config)
    index = _optimization_index(config, now)
    n = len(index)
    hb = config.house_battery
    inp = OptimizerInputs(
        index=index,
        house_load_w=np.full(n, float(config.forecast.fallback_load_w)),
        pv_w=np.zeros(n),
        price_ct_kwh=np.full(n, 30.0),
        feedin_ct_kwh=np.full(n, float(config.feed_in.fixed_ct_kwh)),
        initial_house_soc_wh=float(hb.min_soc_wh),
        ambient_temp_c=np.full(n, 15.0),
        solar_w_m2=np.zeros(n),
    )
    result = Optimizer(config, store_warm=False).solve(inp)
    loads = [ld.name for ld in getattr(config, "controllable_loads", [])
             if ld.enabled]
    return {
        "ok": not result.infeasible,
        "status": result.status,
        "infeasible": bool(result.infeasible),
        "horizon_slots": n,
        "slot_minutes": config.general.slot_minutes,
        "charge_strategy": config.optimization.charge_strategy,
        "rscp_control_enabled": bool(config.e3dc_rscp.control_enabled),
        "rscp_enabled": bool(config.e3dc_rscp.enabled),
        "mqtt_enabled": bool(getattr(config.mqtt, "enabled", False)),
        "pv_source": ("solcast" if config.solcast.enabled
                      else "pvlib" if config.pv_model.arrays else "—"),
        "controllable_loads": loads,
        "fallback_plan_cost_ct": round(float(result.total_cost_ct), 2),
    }


def _run_check(config_path: str) -> int:
    """--check: Config laden+validieren und check_config ausführen; Report
    drucken. Exit 0 = ok, 1 = Modell unzulässig, 2 = Config ungültig."""
    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"Konfiguration UNGÜLTIG: {exc}")
        return 2
    report = check_config(config)
    width = max(len(k) for k in report)
    for key, value in report.items():
        print(f"  {key.ljust(width)} : {value}")
    if report["ok"]:
        print("OK – Konfiguration gültig und Optimierer lösbar (Trockenlauf).")
    else:
        print("FEHLER – Optimierer meldet den Fallback-Plan als unzulässig.")
    return 0 if report["ok"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="EMS – Energy Management System")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur Konfiguration")
    parser.add_argument("--loop", action="store_true", help="Dauerbetrieb im Intervall")
    parser.add_argument("--check", action="store_true",
                        help="Trockenlauf: Config validieren + einen Solve rechnen, "
                             "ohne zu steuern (Exit 0=ok, 1=unlösbar, 2=Config ungültig)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.check:
        raise SystemExit(_run_check(args.config))
    config = load_config(args.config)

    if not args.loop:
        run_once(config)
        return

    global _price_model_async
    _price_model_async = True
    interval = config.general.run_interval_minutes * 60
    # Kleiner Versatz, damit die neuen 15-Minuten-Werte (Preis, Zähler) schon in
    # der InfluxDB stehen, bevor gerechnet wird.
    offset = float(getattr(config.general, "run_offset_seconds", 10))
    log.info("Dauerbetrieb: Intervall %d min, auf Uhr-Raster synchronisiert "
             "(+%.0fs Versatz).", config.general.run_interval_minutes, offset)
    # Persistente MQTT-Verbindung mit Last Will: stirbt der Prozess, setzt der
    # Broker ems/status selbst auf "offline" (Watchdog-Signal für Homey).
    publisher = HomeyMqttPublisher(config)
    publisher.event_sink = _make_alert_event_sink(config)
    try:
        # Temperatur-, Kommando- und WP-Rückmeldungen bereits vor dem ersten
        # Solve abonnieren. So stehen retained/live Istwerte rechtzeitig zur
        # Verfügung und erzeugen beim Dienststart keinen Fehlalarm.
        publisher.start()
    except Exception as exc:
        log.warning("Früher MQTT-Verbindungsaufbau fehlgeschlagen (%s).", exc)
    # Persistente RSCP-Verbindung: hält den Steuer-Watchdog-Thread über die
    # Zyklen (Mode 3/4 alle 5 s). Stirbt der Prozess, fällt der E3DC nach 10 s
    # selbst auf auto zurück (Fail-safe).
    e3dc = None
    schedule_runner = None
    if config.e3dc_rscp.enabled:
        try:
            from .rscp import E3DCLink
            e3dc = E3DCLink(config)
            if config.e3dc_rscp.autoread_limits:
                try:
                    _apply_system_limits(config, e3dc.read_system_limits())
                except Exception as exc:
                    log.warning("Auto-Auslesen der Anlagengrenzen fehlgeschlagen "
                                "(%s) – nutze Config-Werte.", exc)
        except Exception as exc:
            log.warning("RSCP-Anbindung nicht verfügbar (%s).", exc)

    # Sofort-Reapply: den zuletzt gesicherten Steuerbefehl direkt beim Start
    # anwenden, BEVOR der erste (10-20s) Solve läuft. Schließt die Steuer-Lücke
    # nach einem Neustart (sauberes Herunterfahren gibt die Limits frei ->
    # Peak-Shaping würde sonst bis zum ersten Zyklus aussetzen). Nur wenn der
    # Befehl frisch ist (<= 2 Slots alt), sonst ist er überholt und der erste
    # Solve regelt ohnehin gleich.
    if e3dc is not None and config.e3dc_rscp.control_enabled:
        try:
            from .local_history import read_last_control
            ts, cmd = read_last_control(config.e3dc_rscp.history_db_path,
                                        config.general.timezone)
            if ts is not None and cmd:
                age = pd.Timestamp.now(tz=config.general.timezone) - ts
                if age <= pd.Timedelta(minutes=2 * config.general.run_interval_minutes):
                    startup_status = _apply_curtailment_and_control(e3dc, cmd)
                    _store_control_status(config, startup_status)
                    _publish_control_alarm(publisher, config, startup_status)
                    log.info("Sofort-Reapply: letzter Steuerbefehl (%s, Laden<=%.0f "
                             "Entladen<=%.0f W, Netzladen %.0f W) beim Start "
                             "angewandt.", ts.strftime("%H:%M"),
                             cmd.get("batt_charge_limit_w") or 0.0,
                             cmd.get("batt_discharge_limit_w") or 0.0,
                             cmd.get("batt_grid_charge_w") or 0.0)
                else:
                    log.info("Sofort-Reapply übersprungen: letzter Befehl zu alt "
                             "(%s).", ts.strftime("%d.%m %H:%M"))
        except Exception as exc:  # pragma: no cover
            log.warning("Sofort-Reapply fehlgeschlagen (%s).", exc)

    # Persistente manuelle Lade-/Entladepläne laufen unabhängig von Browser
    # und 15-min-Optimierungszyklus. Nur bei explizit aktivierter Dashboard-
    # Steuerung starten, da sie reale RSCP-Eingriffe ausführen.
    if e3dc is not None and config.dashboard.controls_enabled:
        try:
            from .manual_schedule import ManualScheduleRunner
            schedule_runner = ManualScheduleRunner(config, e3dc)
            schedule_runner.start()
        except Exception as exc:
            log.warning("Manuelle Akku-Zeitplanung nicht verfügbar (%s).", exc)

    # Dashboard-Server NACH publisher/e3dc starten, damit die interaktive
    # Steuerung (/api/control/*) direkt auf deren Laufzeit-Mechanik zugreift.
    if config.dashboard.enabled and config.dashboard.serve:
        start_dashboard_server(config, publisher=publisher, e3dc=e3dc,
                               config_path=args.config,
                               schedule_runner=schedule_runner)

    live_execution = _LiveExecutionMonitor(config, publisher, e3dc)
    # Staleness-Watchdog: alarmiert, wenn zu lange kein Zyklus erfolgreich lief.
    watchdog_stop = threading.Event()
    _start_cycle_watchdog(config, publisher, watchdog_stop)

    # Geordnetes Beenden: SIGTERM (systemctl stop/restart) und SIGINT lösen ein
    # SystemExit aus, damit der finally-Block läuft und u. a. die persistenten
    # RSCP-Lade-/Entlade-Limits freigegeben werden (EMS_POWER_LIMITS_USED=false) –
    # sie haben keinen Watchdog. (Bei SIGKILL/Stromausfall unmöglich; dann heilt
    # der nächste Zyklus nach dem systemd-Neustart die Grenze selbst.)
    shutdown = {"requested": False, "in_cycle": False}
    recalc_gate = None

    def _handle_signal(signum, _frame):
        shutdown["requested"] = True
        if solver_is_running():
            log.info("Dienstende angefordert – laufender Solver wird abgebrochen.")
            request_solver_cancel(final=True)
            if shutdown["in_cycle"]:
                # Der Hauptthread steckt selbst im Zyklus: run_once endet mit
                # SolverCancelled bzw. die Schleife sieht shutdown["requested"].
                return
            # Der Solver gehoert einem Hintergrundlauf (Modusvergleich,
            # What-if), der Hauptthread wartet nur auf die naechste Marke.
            # Frueher kehrte der Handler hier zurueck und der Dienst wartete
            # bis zu 15 min weiter -> systemd beendete ihn nach 90 s per
            # SIGKILL, ohne dass close() die Limits freigab (12.11.2026).
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _sd_notify("READY=1")
    try:
        while True:
            if _service_restart_requested.is_set():
                break
            shutdown["in_cycle"] = True
            try:
                run_once(config, publisher, e3dc)
            except SolverCancelled:
                log.info("Optimierung fuer Dienstende sauber abgebrochen.")
                break
            except Exception as exc:  # pragma: no cover
                log.exception("Fehler im EMS-Zyklus – fahre fort.")
                publisher.publish_alert("error", f"EMS-Zyklus fehlgeschlagen: {exc}")
            finally:
                shutdown["in_cycle"] = False
            if shutdown["requested"]:
                break
            if _service_restart_requested.is_set():
                break
            # Lebenszeichen an systemd (WatchdogSec): bleibt es aus (Prozess hängt),
            # startet systemd den Dienst neu.
            _sd_notify("WATCHDOG=1")
            # Bis zur nächsten glatten Raster-Marke (z. B. :00/:15/:30/:45) warten;
            # ein MQTT-Kommando (ems/cmd/recalc, car_boost) bricht das Warten ab.
            now = _time.time()
            next_mark = (now // interval + 1) * interval + offset
            if next_mark - now < 5.0:      # zu knapp -> erst zur übernächsten Marke
                next_mark += interval
            # In Schritten warten: MQTT-Recalc bricht sofort ab; zusätzlich prüft
            # jeder Schritt eine große Live-Abweichung vom Plan (Auto-Recalc).
            check = (max(10.0, config.recalc.check_seconds)
                     if config.recalc.enabled else None)
            if check is None:
                recalc_gate = None
            elif recalc_gate is None or recalc_gate.check_s != check:
                recalc_gate = _RecalcGate(check)
            live_check = None
            if (e3dc is not None and (
                    config.forecast.live_nowcast_enabled
                    or (config.monitoring.execution_audit_enabled
                        and config.monitoring.execution_live_enabled))):
                live_check = max(
                    1.0, config.monitoring.execution_live_sample_seconds)
            while True:
                remaining = next_mark - _time.time()
                if remaining <= 0:
                    break
                waits = [remaining]
                if check is not None:
                    waits.append(check)
                if live_check is not None:
                    waits.append(live_check)
                step = min(waits)
                if publisher.wait_for_recalc(step):
                    if _service_restart_requested.is_set():
                        break
                    log.info("Neuberechnung per Dashboard/MQTT angefordert – "
                             "Zyklus startet sofort.")
                    break
                live = live_execution.sample()
                delta = None
                if recalc_gate is not None and recalc_gate.due(_time.time()):
                    delta = recalc_gate.observe(
                        _live_recalc_needed(config, e3dc, live=live),
                        _time.time())
                if delta is not None:
                    log.info("Große Live-Abweichung (%.0f W Netz vs. Plan) – "
                             "Neuberechnung sofort.", delta)
                    break
    except (KeyboardInterrupt, SystemExit):
        log.info("EMS wird beendet – Verbindungen werden geschlossen.")
    finally:
        _sd_notify("STOPPING=1")
        watchdog_stop.set()
        if schedule_runner is not None:
            try:
                schedule_runner.close()
            except Exception as exc:  # pragma: no cover
                log.debug("Zeitplan-Runner nicht sauber geschlossen: %s", exc)
        if e3dc is not None:
            try:
                e3dc.close()   # gibt persistente Lade-/Entlade-Limits frei
            except Exception as exc:  # pragma: no cover
                log.warning("RSCP-Abschluss fehlgeschlagen (%s).", exc)
        try:
            publisher.close()
        except Exception as exc:  # pragma: no cover
            log.debug("MQTT-Verbindung nicht sauber geschlossen: %s", exc)
    if _service_restart_requested.is_set():
        # ems.service verwendet Restart=on-failure. Ein eigener Exit-Code erlaubt
        # dem unprivilegierten Dienst einen sauberen Neustart ohne systemctl/sudo.
        raise SystemExit(75)


if __name__ == "__main__":
    main()
