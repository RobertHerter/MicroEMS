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
import logging
import os
import signal
import time as _time
from datetime import timedelta

import numpy as np
import pandas as pd

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
from .tariff import read_price_signal
from . import solcast
from .validate import summarize, validate_plan

log = logging.getLogger("ems.main")

_intraday_state = {
    "load": {"issue": None, "ratio": 1.0, "applied": None, "seeded": False},
    "pv": {"issue": None, "ratio": 1.0, "applied": None, "seeded": False},
}
_intraday_raw = {"load": None, "pv": None}
_control_alarm = {"failed": False, "key": None, "last": 0.0}
_execution_alarm = {"failed": False}
_solver_alarm = {"failed": False}
_load_feedback_alarm = {"failed": set()}


def _audit_execution(config, now, live):
    """Vorherigen Sollslot mit aktuellem E3DC-Snapshot vergleichen."""
    if not config.monitoring.execution_audit_enabled or not live:
        return None
    from .local_history import (read_execution_plan_slot,
                                write_execution_audit)
    planned = read_execution_plan_slot(config.e3dc_rscp.history_db_path, now)
    if not planned:
        return None
    actual = {"grid_w": live.get("grid_w"), "battery_w": live.get("battery_w"),
              "soc": live.get("soc_percent")}
    tolerances = {
        "grid_w": config.monitoring.execution_grid_tolerance_w,
        "battery_w": config.monitoring.execution_battery_tolerance_w,
        "soc": config.monitoring.execution_soc_tolerance_percent,
    }
    deviations, failed = {}, []
    for key, tolerance in tolerances.items():
        if actual[key] is None or planned.get(key) is None:
            continue
        delta = float(actual[key]) - float(planned[key])
        deviations[key] = round(delta, 2)
        if abs(delta) > tolerance:
            failed.append(key)
    labels = {"grid_w": "Netz", "battery_w": "Akku", "soc": "SoC"}
    ok = not failed
    message = ("Soll/Ist innerhalb der Toleranzen." if ok else
               "Abweichung bei " + ", ".join(labels[k] for k in failed) + ".")
    audit = {"checked_at": pd.Timestamp.now(tz="UTC").isoformat(), "ok": ok,
             "state": "ok" if ok else "deviation", "message": message,
             "planned": planned, "actual": actual, "deviations": deviations}
    write_execution_audit(config.e3dc_rscp.history_db_path, now, audit)
    return audit


def _publish_execution_alarm(publisher, config, audit) -> None:
    if publisher is None or not audit:
        return
    from .local_history import read_execution_audits
    recent = read_execution_audits(
        config.e3dc_rscp.history_db_path,
        max(1, config.monitoring.execution_alert_consecutive))
    consecutive = (len(recent) >= config.monitoring.execution_alert_consecutive
                   and all(not item["ok"] for item in recent))
    if consecutive and not _execution_alarm["failed"]:
        publisher.publish_alert(
            "warning", "EMS-Ausführung weicht wiederholt vom Plan ab: "
            + audit["message"])
        _execution_alarm["failed"] = True
    elif audit.get("ok") and _execution_alarm["failed"]:
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
            "warning", f"Pool-Rückmeldung fehlt oder ist veraltet: {label}.")
    for label in sorted(old - bad):
        publisher.publish_alert("info", f"Pool-Rückmeldung wieder aktuell: {label}.")
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

CONTROL_COLS = [
    # E3DC-Steuerbefehle (Limits nur bei Abweichung vom Eigenverbrauch):
    "batt_charge_limit_w", "batt_discharge_limit_w",
    "batt_grid_charge_w", "batt_grid_discharge_w",
    "charge_limited", "discharge_limited",
    # rohe Optimierer-Leistungen (Referenz/Analyse):
    "batt_dc_charge_w", "batt_ac_charge_w", "batt_discharge_w",
    "car_charge_w", "grid_import_w", "grid_export_w", "pv_curtail_w",
]


def _house_history_refresh_window(now, last, slot_minutes: int,
                                  settle_minutes: int,
                                  overlap_hours: int):
    """Sicheres RSCP-Historienfenster: gereift, überlappend, max. drei Tage."""
    freq = pd.Timedelta(minutes=slot_minutes)
    now = pd.Timestamp(now)
    safe_end = (now - pd.Timedelta(minutes=settle_minutes)).floor(freq)
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
PREDICTION_COLS = [
    "house_soc_wh", "house_soc_percent", "car_soc_wh", "car_soc_percent",
    "slot_cost_ct", "price_ct_kwh", "feedin_ct_kwh", "pv_w", "house_load_w",
]


def _now_slot(config: Config) -> pd.Timestamp:
    freq = f"{config.general.slot_minutes}min"
    return pd.Timestamp.now(tz=config.general.timezone).floor(freq)


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
    # Nur ein NICHT bereits auf Mitternacht liegendes Ende aufrunden. Das alte
    # <= machte aus 48 h genau um 00:00 versehentlich 72 h.
    if rounded_end < raw_end:
        rounded_end += pd.DateOffset(days=1)
    return pd.date_range(start, rounded_end, freq=freq,
                         tz=config.general.timezone, inclusive="left")


def run_once(config: Config, publisher: HomeyMqttPublisher | None = None,
             e3dc=None) -> None:
    """Ein Rechenzyklus. `publisher`/`e3dc`: persistente Verbindungen im Loop-
    Betrieb (MQTT Last Will bzw. RSCP-Watchdog-Thread); ohne werden sie pro Lauf
    erzeugt und wieder geschlossen."""
    repo = make_repository(config)
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
            e3dc.set_alarm_callback(publisher.publish_alert)
        except Exception as exc:
            log.warning("RSCP-Anbindung nicht verfügbar (%s).", exc)
    try:
        _reload_thermal_calibration(config)
        now = _now_slot(config)
        freq = f"{config.general.slot_minutes}min"
        # Per MQTT gesetzte Overrides (ems/cmd/car_departure_time,
        # car_target_soc, min_soc, max_soc) für diesen Lauf anwenden.
        if publisher is not None:
            publisher.apply_vehicle_overrides(config.vehicle)
            publisher.apply_battery_overrides(config.house_battery)
            publisher.apply_load_overrides(config.controllable_loads)
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
        log.info("Lade Verbrauchs-Historie und erstelle Prognose ...")
        history = load_history(repo, config, now)
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
        load_source_missing = history.dropna().empty
        if load_source_missing:
            load_fc = pd.Series(
                config.forecast.fallback_load_w,
                index=pd.date_range(now, periods=config.general.n_forecast_slots,
                                    freq=freq, tz=config.general.timezone))
            log.warning("Keine verwertbare Verbrauchshistorie – verwende "
                        "konservativ %.0f W.", config.forecast.fallback_load_w)
        else:
            load_fc = forecaster.forecast(
                history, now, config.general.n_forecast_slots,
                hist_temp=temp, fut_temp=temp, hist_pv=hist_pv, fut_pv=fut_pv)

        # Intraday-Korrektur: Ist/Prognose-Verhältnis der letzten Stunden auf
        # die Zukunft anwenden (abklingend) - fängt Tagesabweichungen, die das
        # Ähnliche-Tage-Modell nicht sehen kann (Besuch, Wetter).
        load_ratio, pv_ratio = _intraday_ratios(repo, config, forecaster,
                                                history, temp, now, hist_pv=hist_pv)
        if load_ratio is not None:
            load_fc = load_fc * intraday_factor_series(
                load_ratio, load_fc.index, now,
                config.forecast.intraday_load_decay_hours)
            log.info("Intraday-Korrektur Last: x%.2f (roh x%.2f; klingt über "
                     "%.1f h ab).", load_ratio,
                     _intraday_raw["load"],
                     config.forecast.intraday_load_decay_hours)

        load_p10, load_p90 = forecaster.uncertainty_band(history, load_fc)
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
        house_load = house_series.values
        load_p10_opt = load_p10.reindex(opt_index)
        load_p90_opt = load_p90.reindex(opt_index)

        # --- 2) Eingangsdaten lesen ------------------------------------- #
        cal_profile = None
        if config.calibration.enabled:
            from .calibration import load_profile
            cal_profile = load_profile(config.calibration.pv_profile)
        calibration_source = "solcast" if config.solcast.enabled else "pvlib"
        cal_profile_active = (cal_profile if
                              solcast.selected_source(config) == calibration_source
                              else None)

        pv_missing_slots = {}

        def _pv_series(signal: str, required: bool = True) -> pd.Series:
            """PV-Signal auf den Horizont + Kalibrierprofil + Intraday-Korrektur."""
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
            if pv_ratio is not None:
                s = s * intraday_factor_series(
                    pv_ratio, s.index, now,
                    config.forecast.intraday_pv_decay_hours)
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
            log.info("Intraday-Korrektur PV: x%.2f (roh x%.2f; klingt über "
                     "%.1f h ab).", pv_ratio, _intraday_raw["pv"],
                     config.forecast.intraday_pv_decay_hours)
        # Pessimistische PV (Solcast p10, optional): dimensioniert die
        # Einspeise-Linie an Peak-Tagen wolken-robust.
        pv10 = (_pv_series("pv_forecast_p10", required=False)
                if solcast.available(config, repo, "pv_forecast_p10") else None)
        price, price_estimated = _price_series(
            repo, config, opt_index, now, return_estimated=True)

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
            feedin = feedin.where(price >= 0.0, 0.0)

        # Plausibilitäts-Grenzen auf die externen Eingaben (Preis/PV) UND die
        # Hauslast, bevor irgendetwas davon in Archiv/Optimierer fließt: ein
        # einzelner API-Ausreißer (Solcast-Sprung, Preis-Spike, negativer Wert)
        # darf keinen ganzen Steuerzyklus verzerren.
        if getattr(config, "sanity", None) and config.sanity.enabled:
            from .sanity import sanitize_inputs
            price, pv, pv10, house_series = sanitize_inputs(
                config, price=price, pv=pv, pv10=pv10, load=house_series)
            house_load = house_series.values

        # Live-Werte optional direkt vom E3DC (RSCP) statt aus der InfluxDB.
        # force=True: im Loop-Betrieb frisch pollen (kein Zyklus-übergreifender Cache).
        live = e3dc.read_live(force=True) if (e3dc and config.e3dc_rscp.read_live) else None
        live_src = "RSCP"
        if live is None:               # ohne RSCP: extern per Ingest-API eingespeiste Werte
            from .ingest import get_live
            live = get_live()
            live_src = "Ingest-API"
        if live:
            log.info("%s live: SoC %.0f%%, PV %.0f W, Last %.0f W.", live_src,
                     live.get("soc_percent") or -1, live.get("pv_w") or 0,
                     live.get("house_load_w") or 0)
            # Ist-Werte lokal protokollieren (Ersatz für die InfluxDB-Ist-Signale).
            if config.e3dc_rscp.history_source:
                try:
                    write_actuals(config.e3dc_rscp.history_db_path, now, live)
                except Exception as exc:
                    log.warning("Ist-Wert-Protokollierung fehlgeschlagen (%s).", exc)
            try:
                execution_status = _audit_execution(config, now, live)
            except Exception as exc:
                log.warning("Soll/Ist-Ausführungsprüfung fehlgeschlagen (%s).", exc)

        # Slot-0-Anker: für den unmittelbar laufenden Slot schlägt die
        # Live-Messung (RSCP direkt, sonst Mittel der letzten Slot-Länge aus
        # InfluxDB) die Prognose - der Sollwert basiert auf dem echten Zustand.
        try:
            slot_td = pd.Timedelta(freq)
            if (live and live.get("house_load_w") is not None
                    and np.isfinite(float(live["house_load_w"]))):
                house_load[0] = max(0.0, float(live["house_load_w"]))
            else:
                m = repo.read_slots("house_consumption", now - slot_td, now,
                                    fill=False).mean()
                if np.isfinite(m):
                    house_load[0] = max(0.0, float(m))
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
        except Exception as exc:  # pragma: no cover
            log.debug("Slot-0-Anker fehlgeschlagen (%s).", exc)

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
        log.info("Starte MILP-Optimierung (%d Slots) ...", len(opt_index))
        load_state = _read_load_state(config, publisher)
        load_feedback, load_feedback_status = _read_load_feedback(
            config, publisher, now)
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
        if pv_selection and pv_selection.get("reason"):
            pv_note += f" ({pv_selection['reason']})"
        forecast_quality = [
            _forecast_quality_entry(
                "Hauslast", total_slots,
                total_slots if load_source_missing else n_load_fallback,
                "konservativer Lastwert", forecast_issue,
                "empirisches p10–p90-Band"),
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
        forecast_quality.append(_forecast_quality_entry(
            "Strompreis", total_slots, int(price_estimated.sum()),
            "Ähnliche-Tage-Preisprognose", forecast_issue))
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
                "house_load_w": house_series,
                "house_load_p10_w": load_p10_opt,
                "house_load_p90_w": load_p90_opt,
                "pv_w": pv,
                "price_ct_kwh": price,
                "feedin_ct_kwh": feedin,
            }
            if pv10 is not None:
                archive_series["pv10_w"] = pv10
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
        if config.monitoring.drift_enabled:
            try:
                from .drift import DriftMonitor
                drift_mae = DriftMonitor(config).check(repo, now)
            except Exception as exc:
                log.warning("Drift-Check fehlgeschlagen (%s).", exc)

        # --- 3d) Debug-Schnappschuss (für den Mail-Report-Button) ------- #
        try:
            from .debugdump import save_snapshot
            save_snapshot(config, now, inputs, result, violations, drift_mae)
        except Exception as exc:
            log.warning("Debug-Schnappschuss fehlgeschlagen (%s).", exc)

        # --- 4) MQTT (best effort – darf den Lauf nicht abbrechen) ------ #
        try:
            if one_shot:
                publisher = HomeyMqttPublisher(config)
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
            if drift_mae is not None and drift_mae > config.monitoring.drift_alert_percent:
                publisher.publish_alert(
                    "warning", f"SoC-Drift {drift_mae:.1f} pp über Schwelle "
                               f"({config.monitoring.drift_alert_percent:.0f} pp) – "
                               f"Modell weicht von der Realität ab.")
            _publish_solver_alarm(publisher, solver_status)
            _publish_execution_alarm(publisher, config, execution_status)
            _publish_load_feedback_alarm(publisher, load_feedback_status)
            load_cmds = publisher.publish(result.table, now, result.load_mqtt_map)
            plan_published = True
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
                slot_ts = pd.Timestamp(now).floor("15min")
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
                    pos = result.table.index.get_indexer([now], method="ffill")[0]
                    row = result.table.iloc[max(pos, 0)]
                    control_status = e3dc.apply_control(row)
                    # Aktuellen Befehl lokal sichern -> beim nächsten Dienststart
                    # sofort re-applybar (schließt die Steuer-Lücke bis zum 1. Solve).
                    try:
                        from .local_history import write_last_control
                        write_last_control(
                            config.e3dc_rscp.history_db_path, now,
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
            except Exception:
                pass

        if plan_published:
            try:
                from .local_history import write_execution_plan
                write_execution_plan(
                    config.e3dc_rscp.history_db_path, now, result.table,
                    initial_soc_percent=float(soc_pct))
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
        # Anzeige ab heute 00:00: Ist-Werte (bis jetzt) und Prognose (ganzer
        # Bereich) vergleichbar; Steuerung/Prognose-SoC für die Zukunft.
        if config.dashboard.enabled:
            display = _build_display_frame(repo, config, now, history, result,
                                           intraday=(load_ratio, pv_ratio),
                                           hist_pv=hist_pv, fut_pv=fut_pv,
                                           e3dc=e3dc)
            # Ist-Temperatur-Verlauf thermischer Lasten für das Temperatur-Panel.
            load_temp_actual = {}
            try:
                from .local_history import read_load_temp
                tz = config.general.timezone
                for ld in getattr(config, "controllable_loads", []):
                    if ld.type == "thermal":
                        s = read_load_temp(config.e3dc_rscp.history_db_path, ld.name,
                                           display.index[0], display.index[-1], tz)
                        if not s.empty:
                            load_temp_actual[ld.name] = s
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
                            thermal_calibration=thermal_calibration)
            if getattr(config.dashboard, "api_enabled", False):
                api_file = os.path.join(os.path.dirname(config.dashboard.output_path) or ".", "api_data.json")
                try:
                    display.reset_index().rename(columns={"index": "timestamp"}).to_json(
                        api_file, orient="records", date_format="iso"
                    )
                except Exception as exc:
                    log.warning("Fehler beim Schreiben von %s: %s", api_file, exc)
    finally:
        repo.close()
        if own_e3dc and e3dc is not None:
            e3dc.close()


def _intraday_ratios(repo, config, forecaster, history, temp, now, hist_pv=None):
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
        act = read_actual_signal(config, repo, "house_consumption", load_start, now)
        # Prognose für das Fenster aus der Historie DAVOR (sonst fließen die
        # Ist-Werte des Fensters in ihre eigene Prognose ein).
        hist_before = history[history.index < load_start]
        pred = forecaster.forecast(hist_before, load_start, len(act),
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
                from .calibration import apply_pv_correction, load_profile
                prof = load_profile(config.calibration.pv_profile)
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
        temp_map, rad_map = fetch_forecast(w.latitude, w.longitude,
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
    """Day-Ahead-Spotpreis von Energy-Charts holen und lokal cachen (throttled,
    höchstens einmal je ~5 min). Holt jüngste Historie + Folgetag (Day-Ahead),
    damit der Optimierungshorizont echte Preise hat. Nur bei dynamischem Tarif."""
    if not (config.tariff.enabled and config.tariff.type == "dynamic"):
        return
    global _last_spot_fetch
    if _time.time() - _last_spot_fetch <= 300:
        return
    try:
        from .energycharts import fetch_spot
        from .local_history import write_spot
        today = pd.Timestamp.now(tz=config.general.timezone).normalize()
        start = (today - pd.Timedelta(days=3)).date().isoformat()
        end = (today + pd.Timedelta(days=2)).date().isoformat()
        n = write_spot(config.e3dc_rscp.history_db_path,
                       fetch_spot(config.tariff.bidding_zone, start, end))
        _last_spot_fetch = _time.time()
        log.info("Energy-Charts: %d Spot-Preiswerte aktualisiert.", n)
    except Exception as exc:
        log.warning("Energy-Charts-Abruf fehlgeschlagen (%s) – nutze Cache.", exc)


def _forecast_quality_entry(name: str, total_slots: int, replaced_slots: int,
                            replacement: str, issued_at=None,
                            note: str | None = None) -> dict:
    """Einheitliche Qualitätsstufe für eine operative Prognosereihe."""
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
    now = pd.Timestamp(now).floor("15min")
    tz = config.general.timezone
    # Spotpreis (Energy-Charts): bis wann liegen ECHTE Preise vor? Erwartung:
    # vormittags bis Ende heute; nach der Day-Ahead-Auktion (~14 Uhr) sollte
    # der Folgetag da sein. Danach ergänzt die Ähnliche-Tage-Schätzung.
    try:
        if config.tariff.enabled and config.tariff.type == "dynamic":
            from .local_history import read_spot
            s = read_spot(config.e3dc_rscp.history_db_path,
                          now - pd.Timedelta(hours=2),
                          now + pd.Timedelta(hours=48), tz)
            sd = s.dropna()
            last = sd.index[-1] if len(sd) else None
            expected = (now.normalize()
                        + pd.Timedelta(days=2 if now.hour >= 15 else 1)
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
            t = publisher.get_load_temp(ld.temp_signal)
            if t is not None and np.isfinite(float(t)):
                st[ld.name] = float(t)
    return st or None


def _reload_thermal_calibration(config) -> None:
    """Extern automatisch kalibrierte Overlay-Werte ohne Neustart übernehmen."""
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
    by_name = {ld.name: ld for ld in fresh.controllable_loads
               if ld.type == "thermal"}
    changes = []
    for current in config.controllable_loads:
        newer = by_name.get(current.name)
        if current.type != "thermal" or newer is None:
            continue
        for field in ("loss_w_per_k", "solar_absorption"):
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
        log.info("Thermokalibrierung automatisch übernommen: %s",
                 ", ".join(changes))


def _read_load_feedback(config, publisher, now):
    """Echte thermische Stufenzustände lesen, archivieren und verdichten."""
    state, statuses = {}, []
    if publisher is None:
        return None, statuses
    from .local_history import write_load_feedback
    for ld in getattr(config, "controllable_loads", []):
        if ld.type != "thermal":
            continue
        for stage in ld.stages:
            label = f"{ld.name}/{stage.name}"
            configured = bool(stage.feedback_topic or stage.power_topic)
            feedback = publisher.get_load_feedback(
                label, ld.feedback_max_age_minutes) if configured else None
            item = {"label": label, "name": ld.name, "stage": stage.name,
                    "configured": configured,
                    "required": bool(ld.feedback_required),
                    "fresh": bool(feedback and feedback.get("fresh")),
                    "on": feedback.get("on") if feedback else None,
                    "power_w": feedback.get("power_w") if feedback else None,
                    "age_seconds": (feedback.get("age_seconds")
                                    if feedback else None)}
            statuses.append(item)
            if feedback is not None:
                try:
                    write_load_feedback(config.e3dc_rscp.history_db_path, now,
                                        ld.name, stage.name, feedback)
                except Exception as exc:
                    log.debug("Last-Rückmeldung nicht speicherbar (%s).", exc)
            if item["fresh"] and item["on"] is not None:
                state[label] = bool(item["on"])
    return state or None, statuses


def _price_series(repo, config, index, now, return_estimated=False):
    """Strompreis über `index`: Ist-Werte wo vorhanden, sonst Ähnliche-Tage-
    Prognose für noch fehlende (Folgetag-)Preise – statt einer flachen ffill-Linie.
    Preise dürfen negativ sein (clip_min=None).
    return_estimated=True: zusätzlich Bool-Maske, welche Slots geschätzt sind.
    """
    slot = pd.Timedelta(f"{config.general.slot_minutes}min")
    raw = read_price_signal(config, repo, index[0], index[-1] + slot).reindex(index)
    estimated = raw.isna()   # Slots ohne echten Börsenpreis -> Schätzung
    hist = pd.Series(dtype="float64")
    if estimated.any():
        try:
            hist = read_price_signal(config, repo, now - timedelta(days=90), now).dropna()
            if not hist.empty:
                fc = LoadForecaster(config).forecast(
                    hist, index[0], len(index), clip_min=None, apply_correction=False)
                raw = raw.fillna(fc.reindex(index))
                log.info("Fehlende Folgetag-Preise per Ähnliche-Tage-Prognose ergänzt.")
        except Exception as exc:  # pragma: no cover
            log.warning("Preis-Prognose fehlgeschlagen (%s).", exc)
    # Auch bei komplett ausgefallener Preisquelle bleibt der Optimierer
    # lauffähig. Median der echten Historie, sonst konfigurierter Fixpreis.
    remaining = raw.isna()
    if remaining.any():
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
    if return_estimated:
        return price, estimated
    return price


def _build_display_frame(repo, config, now, history, result,
                         intraday=(None, None), hist_pv=None, fut_pv=None,
                         e3dc=None) -> pd.DataFrame:
    """Anzeigetabelle heute 00:00 -> Horizontende.

    Enthält Prognosewerte über den gesamten Bereich (pv_w, house_load_w,
    price_ct_kwh) sowie – zum Vergleich – die heutigen IST-Werte bis 'jetzt'
    (actual_*). Steuerbefehle und Prognose-SoC stammen aus der Optimierung
    (Zukunft). Fehlende Signale werden robust übersprungen.
    """
    freq = f"{config.general.slot_minutes}min"
    tz = config.general.timezone
    slot = pd.Timedelta(freq)
    day_start = now.normalize()
    end = result.table.index[-1]
    # Vom Tagesbeginn bis zum letzten optimierten Slot anzeigen.
    full = pd.date_range(day_start, end, freq=freq, tz=tz)  # inkl. Ende
    df = pd.DataFrame(index=full)

    # ---- Prognose über den gesamten Bereich ----
    try:
        temp = _read_temp(repo, config,
                          now - timedelta(days=config.forecast.lookback_days), end + slot)
        forecaster = LoadForecaster(config)
        pred_load = forecaster.forecast(history, day_start, len(full),
                                        hist_temp=temp, fut_temp=temp,
                                        hist_pv=hist_pv, fut_pv=fut_pv)
        df["house_load_w"] = pred_load.reindex(full)
        load_p10, load_p90 = forecaster.uncertainty_band(history, pred_load)
        df["house_load_p10_w"] = load_p10.reindex(full)
        df["house_load_p90_w"] = load_p90.reindex(full)
    except Exception as exc:  # pragma: no cover
        log.warning("Verbrauchsprognose fürs Dashboard fehlgeschlagen: %s", exc)
    try:
        from .calibration import apply_pv_correction, load_profile
        prof = (load_profile(config.calibration.pv_profile)
                if config.calibration.enabled else None)
        calibration_source = "solcast" if config.solcast.enabled else "pvlib"
        if solcast.selected_source(config) != calibration_source:
            prof = None
        for col, signal in (("pv_w", "pv_forecast"),
                            ("pv10_w", "pv_forecast_p10"),
                            ("pv90_w", "pv_forecast_p90")):
            if not solcast.available(config, repo, signal):
                continue
            pv = solcast.read_pv_signal(config, repo, signal, day_start, end + slot)
            if prof:
                pv = apply_pv_correction(pv, prof, tz)
            df[col] = pv.reindex(full)
    except Exception as exc:  # pragma: no cover
        log.warning("PV-Prognose fürs Dashboard fehlgeschlagen: %s", exc)
    try:
        price, estimated = _price_series(repo, config, full, now, return_estimated=True)
        df["price_ct_kwh"] = price
        df["price_estimated"] = estimated.astype(float)  # 1 = Schätzung, 0 = Börsenpreis
    except Exception:
        pass

    # Intraday-Korrektur nur auf den ZUKUNFTS-Teil anwenden (konsistent mit
    # der Optimierung); im Vergangenheits-Teil bleibt die rohe Modellprognose
    # sichtbar (Vergleich Ist vs. Modell).
    load_ratio, pv_ratio = intraday
    for col, ratio in (("house_load_w", load_ratio),
                       ("house_load_p10_w", load_ratio),
                       ("house_load_p90_w", load_ratio), ("pv_w", pv_ratio),
                       ("pv10_w", pv_ratio), ("pv90_w", pv_ratio)):
        if ratio is not None and col in df.columns:
            decay = (config.forecast.intraday_load_decay_hours
                     if col.startswith("house_load_") or col == "house_load_w"
                     else config.forecast.intraday_pv_decay_hours)
            fac = intraday_factor_series(ratio, full, now, decay)
            fac[full <= now] = 1.0
            df[col] = df[col] * fac

    # ---- Zukunftswerte aus der Optimierung (jetzt -> Ende) ----
    ot = result.table
    for c in ["house_soc_percent", "car_soc_percent", "batt_dc_charge_w",
              "batt_ac_charge_w", "batt_discharge_w", "batt_charge_limit_w",
              "batt_discharge_limit_w", "batt_grid_discharge_w", "car_charge_w",
              "grid_import_w", "grid_export_w", "export_line_w", "mode",
              "feedin_ct_kwh", "pv_curtail_w", "decision_reason",
              "decision_energy_kwh", "decision_value_ct",
              "decision_reference_time"]:
        if c in ot.columns:
            df[c] = ot[c].reindex(full)
    for c in ot.columns:                    # steuerbare Lasten (load_*_w / _temp_c)
        if c.startswith("load_"):
            df[c] = ot[c].reindex(full)
    if "mode" in df.columns:
        df["mode"] = df["mode"].fillna("auto")
    else:
        df["mode"] = "auto"

    # Einspeisevergütung durchgehend anzeigen (wie der Preis): die Optimierung
    # liefert sie nur für die Zukunft, die Vergangenheit wird aufgefüllt.
    if "feedin_ct_kwh" in df.columns:
        if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
            try:
                fi = repo.read_slots("feed_in_tariff", day_start, end + slot)
                df["feedin_ct_kwh"] = df["feedin_ct_kwh"].fillna(fi.reindex(full))
            except Exception:  # pragma: no cover
                pass
        df["feedin_ct_kwh"] = df["feedin_ct_kwh"].fillna(config.feed_in.fixed_ct_kwh)
        if config.feed_in.zero_at_negative_price and "price_ct_kwh" in df.columns:
            df["feedin_ct_kwh"] = df["feedin_ct_kwh"].where(
                df["price_ct_kwh"] >= 0.0, 0.0)

    # ---- Heutige IST-Werte (bis jetzt) zum Vergleich ----
    # Bis 'now' lesen (aktuellen Slot einschließen) und im Ist-Bereich vorwärts
    # füllen, damit die Ist-Linie den Jetzt-Marker erreicht und keine Lücke zur
    # Prognose entsteht (überbrückt Mess-Lag der Datenquelle).
    slot = pd.Timedelta(freq)
    past_mask = full <= now
    for col, signal in [("actual_load_w", "house_consumption"),
                        ("actual_pv_w", "pv_generation"),
                        ("actual_soc_percent", "battery_soc"),
                        ("actual_battery_w", "battery_power"),
                        ("actual_grid_w", "grid_power")]:
        try:
            if config.e3dc_rscp.history_source:
                # Lokale Ist-Werte (RSCP), Lücken (z.B. am Umstelltag, bevor die
                # actuals-Tabelle gefüllt war) fürs Dashboard aus der InfluxDB
                # ergänzen, solange dort vorhanden. Rein kosmetisch.
                s = read_actual_signal(config, repo, signal, day_start, now + slot)
                if repo.signal_available(signal):
                    inf = repo.read_slots(signal, day_start, now + slot, fill=False)
                    s = s.combine_first(inf) if not s.empty else inf
            elif repo.signal_available(signal):
                s = read_actual_signal(config, repo, signal, day_start, now + slot)
            else:
                continue
            if not s.empty:
                s = s.reindex(full)
                df[col] = s.where(past_mask).ffill().where(past_mask)
        except Exception:  # pragma: no cover
            pass

    # Ist-Hauslast bevorzugt aus dem 15-min-MITTEL (house_load, Energiebilanz)
    # statt aus dem MOMENTAN-Snapshot (actuals.house_w). Der Snapshot ist ein
    # Spot-Messwert je Zyklus und schwankt stark; bei einem Neustart mitten im
    # Slot schreibt er einen willkürlichen Intra-Slot-Moment (oft ein
    # Momentan-Tief -> tiefer Notch, "Ist zu tief"). Das gereifte 15-min-Mittel
    # ist die korrekte Slot-Ist-Last. Der Momentan-Snapshot füllt nur die
    # jüngsten, noch nicht gereiften Slots (letzte ~Stunde), damit die Ist-Linie
    # den Jetzt-Marker erreicht.
    if config.e3dc_rscp.history_source and "actual_load_w" in df.columns:
        try:
            from .local_history import read_house_load
            hl = read_house_load(config.e3dc_rscp.history_db_path,
                                 day_start, now + slot, config.general.timezone)
            hl = hl.where(hl > 0.0)                 # 0/negativ = unreife Bilanz
            mean = hl.reindex(full) if not hl.empty else pd.Series(index=full, dtype=float)
            mean = mean.where(past_mask)
            # Jüngste, noch nicht in house_load gereifte Slots direkt aus der
            # E3DC-15-min-Historie (Energiebilanz-MITTEL) nachholen - so ist die
            # Ist-Kurve bis JETZT glatt statt momentanwert-verzackt. Read-only,
            # gedeckelt auf die letzten ~2 h; schlägt er fehl, bleibt der
            # Momentan-Snapshot als Fallback.
            if e3dc is not None:
                covered = mean.dropna()
                gap_from = (covered.index.max() + slot if not covered.empty
                            else now - pd.Timedelta(hours=2))
                gap_from = max(gap_from, now - pd.Timedelta(hours=2))
                if gap_from <= now:
                    try:
                        recent = e3dc.read_house_load_15min(gap_from, now + slot)
                        if recent:
                            rs = pd.Series({pd.Timestamp(k): v for k, v in recent.items()},
                                           dtype=float).tz_convert(config.general.timezone)
                            rs = rs.where(rs > 0.0).reindex(full).where(past_mask)
                            mean = mean.combine_first(rs)
                    except Exception:  # pragma: no cover
                        pass
            snap = df["actual_load_w"]               # Momentan-Fallback (Tail)
            df["actual_load_w"] = (mean.combine_first(snap)
                                   .where(past_mask).ffill().where(past_mask))
        except Exception:  # pragma: no cover
            pass
    return df


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
    except Exception:  # pragma: no cover
        pass


def start_dashboard_server(config: Config, publisher=None, e3dc=None,
                           config_path: str = "config.yaml",
                           schedule_runner=None) -> None:
    """Startet im Dienstmodus einen kleinen HTTP-Server, der das Dashboard
    im Browser abrufbar macht (http://<host>:<port>/). Läuft als Daemon-Thread.

    publisher/e3dc: Laufzeit-Mechanik für die interaktive Steuerung
    (/api/control/*); config_path: Basis für die Overlay-Persistenz."""
    import functools
    import http.server
    import json
    import os
    import threading
    import base64

    out = os.path.abspath(config.dashboard.output_path)
    directory = os.path.dirname(out) or "."
    fname = os.path.basename(out)
    snap_path = os.path.abspath(config.report.snapshot_path)

    # Ein gemeinsamer Cache für alle Browser: auch bei mehreren geöffneten
    # Dashboards wird E3/DC höchstens einmal je Live-Intervall gepollt. Die
    # E3DCLink-interne Sperre serialisiert diese Abfrage mit Optimierung und
    # Steuer-Watchdog auf derselben persistenten RSCP-Verbindung.
    live_interval = max(0.0, float(getattr(
        config.dashboard, "live_refresh_seconds", 5.0) or 0.0))
    live_guard = threading.Lock()
    live_cache = {"at": 0.0, "data": None}

    pwa_manifest = json.dumps({
        "name": "E3DC EMS Steuerung", "short_name": "E3DC EMS",
        "description": "Livewerte, Prognose und Steuerung des E3/DC EMS",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#111820", "theme_color": "#1769c2",
        "icons": [{"src": "/app-icon.svg", "sizes": "any",
                   "type": "image/svg+xml", "purpose": "any maskable"}],
    }, ensure_ascii=False).encode("utf-8")
    pwa_icon = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="104" fill="#1769c2"/>
<path d="M292 48 119 285h116l-28 179 186-257H278z" fill="#fff8d8"/>
</svg>'''
    service_worker = b'''const CACHE="e3dc-ems-v1";
self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(["/plotly.min.js","/app-icon.svg"])).then(()=>self.skipWaiting())));
self.addEventListener("activate",e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener("fetch",e=>{const u=new URL(e.request.url);if(u.origin!==location.origin)return;if(u.pathname==="/plotly.min.js"||u.pathname==="/app-icon.svg")e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));});'''

    def _read_dashboard_live():
        if e3dc is None or live_interval <= 0.0:
            return None
        with live_guard:
            mono = _time.monotonic()
            if (live_cache["data"] is not None
                    and mono - live_cache["at"] < live_interval * 0.9):
                return dict(live_cache["data"])
            live = e3dc.read_live(force=True)
            if not live:
                return None
            fields = ("soc_percent", "pv_w", "house_load_w", "grid_w",
                      "battery_w", "wallbox_w")
            data = {k: (float(live[k]) if live.get(k) is not None else None)
                    for k in fields}
            data["updated"] = pd.Timestamp.now(
                tz=config.general.timezone).isoformat()
            live_cache["at"], live_cache["data"] = mono, data
            return dict(data)

    def _power_profile(value):
        """Dashboard-Payload -> sichere 15-min-Leistungskurve oder None."""
        if value in (None, ""):
            return None
        if not isinstance(value, (list, tuple)):
            raise ValueError("power_profile_w muss eine Liste sein")
        if len(value) > 192:
            raise ValueError("Leistungskurve darf höchstens 192 Werte enthalten")
        out = [float(x) for x in value]
        if any(not np.isfinite(x) or x < 0.0 or x > 100000.0 for x in out):
            raise ValueError("Leistungskurve enthält ungültige Wattwerte")
        return out or None

    # Kernparameter, die im Dashboard je Lasttyp editierbar sind (Whitelist).
    _LOAD_PARAMS = {
        "thermal": {"target_c": float, "min_c": float, "max_c": float,
                   "surface_m2": float, "solar_absorption": float},
        "deferrable": {"power_w": float, "runtime_minutes": float,
                       "window_from_hour": int, "window_to_hour": int,
                       "deadline_hours": float,
                       "power_profile_w": _power_profile},
    }

    def _find_load(name):
        for ld in getattr(config, "controllable_loads", []):
            if ld.name == name:
                return ld
        raise ValueError(f"Unbekannte Last: {name!r}")

    def _handle_control(action: str, payload: dict):
        """Führt eine Dashboard-Steuer-Aktion aus (Laufzeit) und persistiert
        Lasten-/Modus-Änderungen ins Overlay (config_overrides.yaml)."""
        from .config import save_override
        from .loads import _slug as _lslug
        from .homey_mqtt import _slug as _hslug
        if action == "load":
            name = str(payload.get("name", ""))
            ld = _find_load(name)
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
            allowed = _LOAD_PARAMS.get(ld.type, {})
            for key, val in (payload.get("params") or {}).items():
                if key not in allowed:
                    raise ValueError(f"Parameter {key!r} für {ld.type} nicht erlaubt")
                cast = allowed[key](val)
                setattr(ld, key, cast)
                save_override(config_path,
                              f"controllable_loads_overrides.{slug}.{key}", cast)
                changed[key] = cast
            if publisher is not None:
                publisher.recalc_event.set()
            return changed
        if action == "mode":
            strat = str(payload.get("strategy", "")).lower()
            if strat not in ("asap", "peak", "auto"):
                raise ValueError("strategy muss asap|peak|auto sein")
            config.optimization.charge_strategy = strat
            save_override(config_path, "optimization.charge_strategy", strat)
            if publisher is not None:
                publisher.recalc_event.set()
            return {"charge_strategy": strat}
        if action == "battery":
            if e3dc is None:
                raise ValueError("Keine RSCP-Verbindung – manuelles Laden nicht möglich")
            act = str(payload.get("action", "")).lower()
            watts = float(payload.get("watts", 0) or 0)
            minutes = float(payload.get("minutes", 15) or 15)
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

    class Handler(http.server.SimpleHTTPRequestHandler):
        def _authed(self) -> bool:
            """Basic-Auth prüfen (Dashboard-username/password). True = ok; bei
            Fehlschlag wird 401 gesendet und False zurückgegeben."""
            u, p = config.dashboard.username, config.dashboard.password
            if not (u and p):
                return True
            expected = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
            if self.headers.get("Authorization") == expected:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="EMS"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _reply(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _raw_reply(self, body: bytes, content_type: str, cache="no-cache"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body_json(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self):
            if not self._authed():
                return
            path = self.path.split("?")[0]
            # Interaktive Steuerung (/api/control/<action>): Lasten, Modus, Akku.
            if path.startswith("/api/control/"):
                if not getattr(config.dashboard, "controls_enabled", False):
                    self.send_error(403, "Steuerung deaktiviert (dashboard.controls_enabled)")
                    return
                action = path[len("/api/control/"):].strip("/")
                try:
                    payload = self._body_json()
                    result = _handle_control(action, payload)
                except KeyError:
                    self.send_error(404, f"Unbekannte Steuer-Aktion: {action}")
                    return
                except ValueError as exc:
                    self.send_error(400, f"Steuer-Fehler: {exc}")
                    return
                except Exception as exc:
                    self.send_error(500, f"Steuer-Fehler: {exc}")
                    return
                self._reply({"status": "ok", "action": action, "result": result})
                return
            # Ingest-API: Live-/Historienwerte extern einspielen (ohne RSCP/Influx).
            if not getattr(config.dashboard, "ingest_enabled", False):
                self.send_error(403, "Ingest deaktiviert (dashboard.ingest_enabled)")
                return
            if not path.startswith("/api/ingest/"):
                self.send_error(404, "Nur /api/ingest/<kind> oder /api/control/<action>")
                return
            kind = path[len("/api/ingest/"):].strip("/")
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                from .ingest import ingest as _do_ingest
                msg = _do_ingest(config, kind, payload)
            except KeyError:
                self.send_error(404, f"Unbekannter Ingest-Typ: {kind}")
                return
            except Exception as exc:
                self.send_error(400, f"Ingest-Fehler: {exc}")
                return
            body = json.dumps({"status": "ok", "kind": kind, "result": msg}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authed():
                return

            path = self.path.split("?")[0]
            if path == "/manifest.webmanifest":
                self._raw_reply(pwa_manifest,
                                "application/manifest+json; charset=utf-8")
                return
            if path == "/app-icon.svg":
                self._raw_reply(pwa_icon, "image/svg+xml; charset=utf-8",
                                "public, max-age=86400")
                return
            if path == "/sw.js":
                self._raw_reply(service_worker,
                                "application/javascript; charset=utf-8")
                return

            # Leichtgewichtige E3/DC-Livewerte für die Kacheln. Die Abfrage
            # startet weder Optimierer noch Dashboard-Neugenerierung.
            if path == "/api/live.json":
                data = _read_dashboard_live()
                if data is None:
                    self._reply({"status": "unavailable"}, 503)
                else:
                    self._reply(data)
                return

            if path == "/api/battery-schedule.json":
                if not getattr(config.dashboard, "controls_enabled", False):
                    self._reply({"status": "disabled"}, 403)
                elif schedule_runner is None:
                    self._reply({"status": "unavailable"}, 503)
                else:
                    self._reply(schedule_runner.snapshot())
                return

            # API Endpunkt für Optimierungsdaten
            if getattr(config.dashboard, "api_enabled", False) and path == "/api/data.json":
                api_path = os.path.join(directory, "api_data.json")
                try:
                    with open(api_path, "rb") as fh:
                        body = fh.read()
                except OSError:
                    self.send_error(404, "Noch keine API-Daten vorhanden")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # Leichtgewichtiger Versions-Endpunkt: mtime der Dashboard-Datei.
            # Die Seite pollt diesen und lädt bei Änderung (neue Berechnung) neu.
            if path in ("/version",):
                try:
                    body = ("%.0f" % os.path.getmtime(out)).encode()
                except OSError:
                    body = b"0"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Debug-Schnappschuss als Download (Button im Dashboard).
            if path == "/report.json":
                try:
                    with open(snap_path, "rb") as fh:
                        body = fh.read()
                except OSError:
                    self.send_error(404, "Noch kein Debug-Schnappschuss vorhanden")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="last_run_debug.json"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
                self.path = "/" + fname
            return super().do_GET()

        def end_headers(self):
            # HTML/Version immer revalidieren, damit reload() die neue Datei
            # holt; das lokale plotly.min.js darf der Browser cachen.
            if not self.path.endswith(".js"):
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            super().end_headers()

        def log_message(self, *a):  # ruhig bleiben
            pass

    handler = functools.partial(Handler, directory=directory)
    try:
        srv = http.server.ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)
    except OSError as exc:
        log.warning("Dashboard-Server konnte nicht starten (%s).", exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Dashboard-Server läuft: http://%s:%d/ (Datei: %s)",
             config.dashboard.host, config.dashboard.port, fname)


def _apply_system_limits(config: Config, lim: dict) -> None:
    """Vom E3DC gelesene Anlagengrenzen in die Config übernehmen (überschreibt
    die konfigurierten Werte). Kapazität/SoC bleiben unberührt."""
    if not lim:
        return
    changes = []
    if "capacity_wh" in lim:
        config.house_battery.capacity_wh = lim["capacity_wh"]
        changes.append(f"capacity={lim['capacity_wh']:.0f} Wh")
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


def main() -> None:
    parser = argparse.ArgumentParser(description="EMS – Energy Management System")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur Konfiguration")
    parser.add_argument("--loop", action="store_true", help="Dauerbetrieb im Intervall")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)

    if not args.loop:
        run_once(config)
        return

    interval = config.general.run_interval_minutes * 60
    # Kleiner Versatz, damit die neuen 15-Minuten-Werte (Preis, Zähler) schon in
    # der InfluxDB stehen, bevor gerechnet wird.
    offset = float(getattr(config.general, "run_offset_seconds", 10))
    log.info("Dauerbetrieb: Intervall %d min, auf Uhr-Raster synchronisiert "
             "(+%.0fs Versatz).", config.general.run_interval_minutes, offset)
    # Persistente MQTT-Verbindung mit Last Will: stirbt der Prozess, setzt der
    # Broker ems/status selbst auf "offline" (Watchdog-Signal für Homey).
    publisher = HomeyMqttPublisher(config)
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
                    startup_status = e3dc.apply_control(cmd)
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

    # Geordnetes Beenden: SIGTERM (systemctl stop/restart) und SIGINT lösen ein
    # SystemExit aus, damit der finally-Block läuft und u. a. die persistenten
    # RSCP-Lade-/Entlade-Limits freigegeben werden (EMS_POWER_LIMITS_USED=false) –
    # sie haben keinen Watchdog. (Bei SIGKILL/Stromausfall unmöglich; dann heilt
    # der nächste Zyklus nach dem systemd-Neustart die Grenze selbst.)
    shutdown = {"requested": False}

    def _handle_signal(signum, _frame):
        shutdown["requested"] = True
        if solver_is_running():
            log.info("Dienstende angefordert – laufender Solver wird abgebrochen.")
            request_solver_cancel()
        else:
            raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _sd_notify("READY=1")
    try:
        while True:
            try:
                run_once(config, publisher, e3dc)
            except SolverCancelled:
                log.info("Optimierung fuer Dienstende sauber abgebrochen.")
                break
            except Exception as exc:  # pragma: no cover
                log.exception("Fehler im EMS-Zyklus – fahre fort.")
                publisher.publish_alert("error", f"EMS-Zyklus fehlgeschlagen: {exc}")
            if shutdown["requested"]:
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
            if publisher.wait_for_recalc(next_mark - _time.time()):
                log.info("Neuberechnung per MQTT-Kommando – Zyklus startet sofort.")
    except (KeyboardInterrupt, SystemExit):
        log.info("EMS wird beendet – Verbindungen werden geschlossen.")
    finally:
        _sd_notify("STOPPING=1")
        if schedule_runner is not None:
            try:
                schedule_runner.close()
            except Exception:  # pragma: no cover
                pass
        if e3dc is not None:
            try:
                e3dc.close()   # gibt persistente Lade-/Entlade-Limits frei
            except Exception as exc:  # pragma: no cover
                log.warning("RSCP-Abschluss fehlgeschlagen (%s).", exc)
        try:
            publisher.close()
        except Exception:  # pragma: no cover
            pass


if __name__ == "__main__":
    main()
