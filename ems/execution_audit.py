"""Ausfuehrungs-Audit: Sollslot gegen Mess- und Livewerte pruefen.

Aus ems/main.py ausgelagert (reine Verschiebung). ``ems.main`` re-exportiert
alle Namen; der Logger bleibt ``ems.main``.
"""
from __future__ import annotations

import logging
import time as _time

import numpy as np
import pandas as pd

from .time_utils import floor_time

log = logging.getLogger("ems.main")



# Ab hier gilt der Akku als voll bzw. leer, und ein nicht ausgefuehrtes Laden
# oder Entladen ist keine Geraeteabweichung mehr. 97 % statt 99: der E3DC
# riegelte gemessen bei 97,9 % ab, obwohl die Konfiguration 100 % erlaubt.
_AUDIT_FULL_SOC_PCT = 97.0
_AUDIT_SOC_MARGIN_PCT = 3.0


def _audit_execution(config, now, live, e3dc=None):
    """Sollslot mit Livewerten oder gereifter E3/DC-Zaehlerenergie pruefen."""
    # Spaet ueber ems.main nachschlagen: dort liegt der Laufzeitzustand, und
    # Tests ersetzen Namen per monkeypatch auf ems.main (kein Zirkelimport,
    # weil erst beim Aufruf importiert wird).
    from . import main as _main
    if not config.monitoring.execution_audit_enabled or not live:
        return None
    # Startup-Karenz: nach einem (Neu-)Start ist die Steuerung noch nicht wieder
    # gesetzt/eingependelt (die Limits waren zwischenzeitlich freigegeben, der
    # E3DC lief auf auto) -> die erste(n) Vergleiche würden fälschlich eine
    # Akku-Abweichung melden. Erst nach der Karenz prüfen.
    from .local_history import (read_actual, read_execution_plan_slot,
                                write_execution_audit)
    grace_min = float(getattr(
        config.monitoring, "execution_audit_startup_grace_minutes", 5.0) or 0.0)
    # Die Karenz betrifft nur aktuelle Livewerte nach dem Reapply. Ein um
    # 75 Minuten gereifter Zaehler-Slot liegt vor dem Neustart und darf nicht
    # uebersprungen werden (er wuerde im naechsten Lauf sonst nie nachgeholt).
    # Der Zähler-Audit funktioniert nur, wenn die Slotbreite ein Vielfaches der
    # 15-min-Zählerfenster des E3DC ist (15/30/60). Bei 5/10 min kann ein
    # einzelner Plan-Slot nicht aus 15-min-Aggregaten rekonstruiert werden ->
    # dann NICHT den (dauerhaft in data_waiting hängenden) Zähler-Audit fahren,
    # sondern auf die Live-Prüfung zurückfallen (use_meter=False).
    use_meter = (e3dc is not None
                 and config.general.slot_minutes % 15 == 0)
    in_grace = (not use_meter and grace_min > 0.0
                and (_time.monotonic() - _main._PROCESS_START) < grace_min * 60.0)
    audit_ts = now
    audit_end = now
    meter = None
    if use_meter:
        slot = pd.Timedelta(minutes=config.general.slot_minutes)
        delay_minutes = max(float(config.general.slot_minutes), float(getattr(
            config.monitoring, "execution_meter_delay_minutes", 75.0) or 75.0))
        audit_ts = floor_time(pd.Timestamp(now) - pd.Timedelta(
            minutes=delay_minutes), f"{config.general.slot_minutes}min")
        audit_end = audit_ts + slot
        expected_windows = config.general.slot_minutes // 15
        try:
            values = e3dc.read_energy_15min(audit_ts, audit_end)
            # Ein Plan-Slot kann mehrere 15-min-Zählerfenster umfassen (30/60 min).
            # ALLE Fenster des Slots aufsummieren – vorher wurde nur das erste
            # Viertel gelesen (Leistung 2–4× zu niedrig -> falsche Abweichungen).
            # Erst prüfen, wenn der ganze Slot gereift ist (alle Fenster da).
            if expected_windows >= 1 and len(values) == expected_windows:
                keys = ("pv_wh", "load_wh", "bat_in_wh", "bat_out_wh",
                        "grid_import_wh", "grid_export_wh")
                meter = {k: float(sum(w[k] for w in values.values()))
                         for k in keys}
        except Exception as exc:
            log.debug("E3DC-Slotenergie noch nicht lesbar (%s).", exc)
    planned = read_execution_plan_slot(
        config.e3dc_rscp.history_db_path, audit_ts)
    if in_grace:
        # Statt die Prüfung stumm auszusetzen (Kachel fehlte dann bis zum
        # nächsten Lauf), eine NEUTRALE Kachel zeigen: ok=True (kein Alarm,
        # setzt die Alarmzählung zurück), mit Klartext, dass nach dem Neustart
        # die Steuerung erst gesetzt/eingependelt wird - so ist die
        # Betriebsdiagnose sofort da, ohne falsche Akku-Abweichung.
        audit = {"checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
                 "ok": True, "state": "startup",
                 "message": "Nach Neustart – Steuerung wird gesetzt/eingependelt "
                            "(Abweichungsprüfung pausiert).",
                 "planned": planned or {},
                 "actual": ({} if e3dc is not None else {
                     "grid_w": live.get("grid_w"),
                     "battery_w": live.get("battery_w"),
                     "soc": live.get("soc_percent")}),
                 "deviations": {}}
        write_execution_audit(config.e3dc_rscp.history_db_path, audit_ts, audit)
        return audit
    if not planned:
        return None
    if use_meter and meter is None:
        audit = {"checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
                 "ok": True, "state": "data_waiting", "cause": "data",
                 "message": "Abgeschlossenes E3DC-Zählerfenster ist noch nicht verfügbar.",
                 "planned": planned, "actual": {}, "deviations": {}}
        write_execution_audit(config.e3dc_rscp.history_db_path, audit_ts, audit)
        return audit
    if meter is not None:
        dt = config.general.slot_minutes / 60.0
        end_plan = read_execution_plan_slot(
            config.e3dc_rscp.history_db_path, audit_end) or {}
        # Der aktuelle Live-SoC gehoert nicht zu einem 75 Minuten alten Slot.
        # Nur einen passend archivierten Ist-Wert am Slotende verwenden.
        actual_soc = None
        try:
            soc_history = read_actual(
                config.e3dc_rscp.history_db_path, "soc",
                audit_end - slot / 2, audit_end + slot / 2,
                config.general.timezone)
            if not soc_history.empty:
                nearest = min(soc_history.index,
                              key=lambda stamp: abs(stamp - audit_end))
                if abs(nearest - audit_end) <= slot / 2:
                    actual_soc = float(soc_history.loc[nearest])
        except Exception as exc:
            log.debug("Historischer SoC für Ausführungs-Audit fehlt (%s).", exc)
        actual = {
            "grid_w": (float(meter["grid_import_wh"])
                       - float(meter["grid_export_wh"])) / dt,
            "battery_w": (float(meter["bat_in_wh"])
                          - float(meter["bat_out_wh"])) / dt,
            "soc": actual_soc,
            "pv_w": float(meter["pv_wh"]) / dt,
            "load_w": float(meter["load_wh"]) / dt,
            "grid_export_w": float(meter["grid_export_wh"]) / dt,
        }
        planned = dict(planned)
        if end_plan.get("soc") is not None:
            planned["soc"] = end_plan["soc"]
        deviations = {}
        for key in ("grid_w", "battery_w", "soc", "pv_w", "load_w"):
            if actual.get(key) is not None and planned.get(key) is not None:
                deviations[key] = round(
                    float(actual[key]) - float(planned[key]), 2)
        deviations["battery_energy_kwh"] = round(
            (float(meter["bat_in_wh"]) - float(meter["bat_out_wh"])
             - float(planned.get("battery_w", 0.0)) * dt) / 1000.0, 3)
        deviations["grid_energy_kwh"] = round(
            (float(meter["grid_import_wh"]) - float(meter["grid_export_wh"])
             - float(planned.get("grid_w", 0.0)) * dt) / 1000.0, 3)
        def _w(value):
            return f"{float(value or 0.0):.0f} W"

        def _action(value):
            value = float(value or 0.0)
            return "laden" if value > 50.0 else ("entladen" if value < -50.0 else "idle")
        expected_action = _action(planned.get("battery_w"))
        observed_action = _action(actual.get("battery_w"))
        batt_bad = abs(deviations.get("battery_w", 0.0)) > float(
            config.monitoring.execution_battery_tolerance_w)
        soc_bad = abs(deviations.get("soc", 0.0)) > float(
            config.monitoring.execution_soc_tolerance_percent)
        grid_bad = abs(deviations.get("grid_w", 0.0)) > float(
            config.monitoring.execution_grid_tolerance_w)
        forecast_bad = (abs(deviations.get("pv_w", 0.0))
                        + abs(deviations.get("load_w", 0.0))) > float(
                            config.monitoring.execution_grid_tolerance_w)
        # Dem E3DC wird KEIN Akku-Sollwert vorgegeben, sondern ein Rahmen:
        # Ladelimit, Entladelimit und (nur dann) ein Netzladebefehl. Innerhalb
        # dieses Rahmens folgt der Akku der tatsaechlichen Hauslast, nicht dem
        # Plan. Eine Abweichung von planned["battery_w"] ist deshalb fuer sich
        # noch kein Geraetefehler - sie ist die Fortsetzung des
        # Prognosefehlers. Geprueft wird der Rahmen.
        #
        # Gemessen am 31.08.2026 ueber 14 Tage: von 1370 Slots lagen ALLE im
        # Rahmen, darunter die 112, die als "Geraeteabweichung" gemeldet wurden
        # (5 bis 18 Warnungen pro Tag). Die Regel kann Verletzungen finden, sie
        # fand keine.
        #
        # Die Knotenbilanz taugt dafuer NICHT: Plan und Ist erfuellen sie
        # jeweils einzeln, also erfuellen die Differenzen sie zwangsläufig
        # (gemessen: Restfehler 0,03 W - auch bei den 1127 unauffaelligen
        # Slots). Sie wuerde jede Abweichung entschuldigen, auch eine echte.
        surplus_actual_w = float(actual.get("pv_w") or 0.0) - float(
            actual.get("load_w") or 0.0)
        grid_charge_cmd_w = float(planned.get("grid_charge_w") or 0.0)
        batt_tol_w = float(config.monitoring.execution_battery_tolerance_w)
        charge_limit_w = planned.get("charge_limit_w")
        discharge_limit_w = planned.get("discharge_limit_w")
        actual_batt_w = float(actual.get("battery_w") or 0.0)
        actual_soc_pct = actual.get("soc")
        # Der E3DC hoert nahe der Obergrenze von selbst auf zu laden - gemessen
        # am 29.08.2026 stand der SoC sechs Slots lang auf exakt 97,9 %,
        # waehrend 2,6 kW Ueberschuss ins Netz gingen. Die Konfiguration sagt
        # max_soc = 100 %, das Geraet riegelt aber darunter ab; deshalb ein
        # Abstand statt einer harten 99. Symmetrisch unten am Mindest-SoC.
        soc_full = actual_soc_pct is not None and float(
            actual_soc_pct) >= _AUDIT_FULL_SOC_PCT
        min_soc_pct = (100.0 * config.house_battery.min_soc_wh
                       / max(1.0, config.house_battery.capacity_wh))
        soc_empty = actual_soc_pct is not None and float(
            actual_soc_pct) <= min_soc_pct + _AUDIT_SOC_MARGIN_PCT
        # Ohne belastbare Zaehlerwerte ist der Rahmen nicht beurteilbar: eine
        # Hauslast von 0 W gibt es nicht. Gemessen 7 von 1379 Slots in 14
        # Tagen; heute (31.08.) erzeugten drei davon prompt Fehlbefunde, obwohl
        # der Akku exakt dem Plan folgte (2790 gegen 2780 W Soll).
        meter_usable = float(actual.get("load_w") or 0.0) > 1.0
        envelope_why = []
        if (charge_limit_w is not None
                and actual_batt_w > float(charge_limit_w) + batt_tol_w):
            envelope_why.append(
                f"lud {_w(actual_batt_w)} über dem Ladelimit "
                f"{_w(charge_limit_w)}")
        if (discharge_limit_w is not None
                and -actual_batt_w > float(discharge_limit_w) + batt_tol_w):
            envelope_why.append(
                f"entlud {_w(-actual_batt_w)} über dem Entladelimit "
                f"{_w(discharge_limit_w)}")
        if (grid_charge_cmd_w <= 1.0
                and actual_batt_w > max(0.0, surplus_actual_w) + batt_tol_w):
            envelope_why.append(
                f"lud {_w(actual_batt_w)} ohne Überschuss "
                f"({_w(surplus_actual_w)}) und ohne Netzladebefehl")
        # Gegenrichtung: der Rahmen ERLAUBTE die geplante Leistung und die
        # Energie war da, der Akku tat es aber nicht. Das ist der Fall, der
        # wirklich zaehlt - so faellt befohlenes Netzladen auf, das ausbleibt.
        planned_batt_w = float(planned.get("battery_w") or 0.0)
        if planned_batt_w > 50.0:
            reachable_w = min(
                planned_batt_w,
                float(charge_limit_w) if charge_limit_w is not None
                else planned_batt_w,
                max(0.0, surplus_actual_w) + grid_charge_cmd_w)
            if (reachable_w > batt_tol_w
                    and actual_batt_w < reachable_w - batt_tol_w
                    and not soc_full):
                envelope_why.append(
                    f"lud nur {_w(actual_batt_w)}, möglich waren "
                    f"{_w(reachable_w)}" + (
                        f" (davon {_w(grid_charge_cmd_w)} befohlenes Netzladen)"
                        if grid_charge_cmd_w > 1.0 else ""))
        elif planned_batt_w < -50.0:
            reachable_w = min(
                -planned_batt_w,
                float(discharge_limit_w) if discharge_limit_w is not None
                else -planned_batt_w,
                max(0.0, -surplus_actual_w))
            if (reachable_w > batt_tol_w
                    and -actual_batt_w < reachable_w - batt_tol_w
                    and not soc_empty):
                envelope_why.append(
                    f"entlud nur {_w(-actual_batt_w)}, möglich waren "
                    f"{_w(reachable_w)}")
        if not meter_usable:
            envelope_why = []
        envelope_bad = bool(envelope_why)

        # planned["export_limit_w"] ist die Peak-LINIE des Optimierers, also ein
        # ZIEL - keine Grenze, die jemand befohlen hat. Sie zu ueberschreiten
        # heisst nur, dass mehr PV kam als geplant und der Akku sie nicht
        # abnahm. Als Geraetebefund zaehlt allein die physische Einspeise-
        # begrenzung aus der Konfiguration.
        #
        # Gemessen am 02.09.2026: fuenf Slots mit "Einspeisung ueber der
        # Grenze", z.B. 6920 gegen 4274 W - bei max_export_w = None, also ohne
        # jede echte Grenze. Dieselbe Verwechslung wie beim Akku (11f07ff):
        # ein internes Optimierungsziel wurde fuer einen Befehl gehalten.
        limit = planned.get("export_limit_w")
        hard_export_limit_w = getattr(config.inverter, "max_export_w", None)
        export_bad = (hard_export_limit_w is not None
                      and actual["grid_export_w"]
                      > float(hard_export_limit_w)
                      + config.monitoring.execution_grid_tolerance_w)
        # Die Linie bleibt sichtbar - als Prognosehinweis, nicht als Befund.
        line_over = (limit is not None and not export_bad
                     and actual["grid_export_w"] > float(limit)
                     + config.monitoring.execution_grid_tolerance_w)
        model_bad = (float(planned.get("pv_curtail_w", 0.0) or 0.0) > 5.0
                     and planned.get("execution_path") == "model")
        failed = envelope_bad or export_bad or (
            config.monitoring.execution_audit_grid and grid_bad)
        if model_bad:
            cause, state = "model", "model_error"
        elif envelope_bad or export_bad:
            cause, state = "device", "device_error"
        elif batt_bad or soc_bad or forecast_bad or grid_bad or line_over:
            cause, state = "forecast", "forecast_deviation"
        else:
            cause, state = "none", "ok"
        ok = not failed and not model_bad
        labels = {"device": "Geräteabweichung", "forecast": "Prognoseabweichung",
                  "model": "nicht ausführbare Modellannahme", "none": "Soll erfüllt"}
        message = labels[cause]

        # Das nackte Label sagt nicht, WAS abwich - dann ist die gelbe
        # Betriebsdiagnose nicht deutbar. Deshalb die auslösenden Groessen mit
        # Ist- und Sollwert in die Meldung schreiben.
        why = []
        why.extend(f"Akku {t}" for t in envelope_why)
        if batt_bad and not envelope_bad:
            why.append(
                f"Akku {_w(actual.get('battery_w'))} statt geplant "
                f"{_w(planned.get('battery_w'))} - im Rahmen des Befehls "
                f"(Laden<={_w(charge_limit_w)}, Entladen<={_w(discharge_limit_w)}), "
                f"der Akku folgte dem tatsächlichen Überschuss "
                f"{_w(surplus_actual_w)}")
        if soc_bad:
            why.append(f"SoC {float(actual.get('soc') or 0.0):.1f} % statt "
                       f"{float(planned.get('soc') or 0.0):.1f} %")
        if export_bad:
            why.append(f"Einspeisung {_w(actual.get('grid_export_w'))} über der "
                       f"Einspeisebegrenzung von {_w(hard_export_limit_w)}")
        if line_over:
            why.append(f"Einspeisung {_w(actual.get('grid_export_w'))} über der "
                       f"Peak-Linie von {_w(limit)} - mehr PV als geplant, "
                       f"keine Grenzverletzung")
        if cause == "forecast":
            why.append(f"PV {deviations.get('pv_w', 0.0):+.0f} W, Hauslast "
                       f"{deviations.get('load_w', 0.0):+.0f} W gegenüber der Prognose")
        # Haeufigster Fall: der Akku sollte laden, der Ueberschuss ging aber ins
        # Netz. Das ist die eigentlich interessante Information.
        if (batt_bad
                and float(planned.get("battery_w") or 0.0)
                > float(actual.get("battery_w") or 0.0) + 50.0
                and float(actual.get("grid_export_w") or 0.0)
                > float(planned.get("grid_export_w") or 0.0) + 50.0):
            why.append("der Überschuss ging stattdessen ins Netz")
        if why:
            message += ": " + "; ".join(why)
        audit = {"checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
                 "ok": ok, "state": state, "cause": cause, "message": message + ".",
                 "planned": planned, "actual": actual, "deviations": deviations,
                 "meter": meter,
                 # Nur aussagekraeftig, wenn es eine echte Begrenzung gibt.
                 "export_limit_ok": (None if hard_export_limit_w is None
                                     else not export_bad),
                 "export_line_w": None if limit is None else round(float(limit), 1),
                 "export_line_exceeded": bool(line_over)}
        audit["battery_action"] = {
            "planned": expected_action, "actual": observed_action,
            "ok": not batt_bad,
        }
        write_execution_audit(config.e3dc_rscp.history_db_path, audit_ts, audit)
        return audit
    actual = {"grid_w": live.get("grid_w"), "battery_w": live.get("battery_w"),
              "soc": live.get("soc_percent")}
    tolerances = {
        "grid_w": config.monitoring.execution_grid_tolerance_w,
        "battery_w": config.monitoring.execution_battery_tolerance_w,
        "soc": config.monitoring.execution_soc_tolerance_percent,
    }
    # Netz ist ein reines Bilanz-Residuum (Netz = Last - PV + Akku): weicht die
    # Erzeugung/Last von der Prognose ab, gleicht der E3DC die Differenz übers
    # Netz aus (z.B. PV über Prognose im Peak -> Mehr-Einspeisung). Das ist kein
    # Steuerfehler - das echte Ausführungssignal ist die Akku-Abweichung. Netz
    # wird daher nur informativ erfasst und triggert nur mit ausdrücklichem Flag.
    trigger = {"battery_w", "soc"}
    if config.monitoring.execution_audit_grid:
        trigger.add("grid_w")
    deviations, failed = {}, []
    for key, tolerance in tolerances.items():
        if actual[key] is None or planned.get(key) is None:
            continue
        delta = float(actual[key]) - float(planned[key])
        deviations[key] = round(delta, 2)
        if key in trigger and abs(delta) > tolerance:
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


# Abstand zweier Strang-Abfragen. Der Watchdog laeuft alle 5 s; ein PVI-Aufruf
# kostet ~60 ms und wird dort nicht gebraucht - fuer ein Slotmittel reichen
# 15 Proben je Viertelstunde reichlich.
STRING_SAMPLE_S = 60.0


# Eine Info-Meldung "mit bekannter Ursache" je Ursachen-ART hoechstens einmal in
# dieser Zeit. Vorher hing die Sperre am Ursachen-TEXT, und der traegt Live-
# Zahlen: jede Aenderung galt als neue Ursache, und der Slotwechsel setzte die
# Sperre zurueck. Gemessen am 03.10.2026 ueber drei Tage: 971 Meldungen, davon
# 953 Lastsprung bei nur 12 Episoden; mit dieser Sperre waeren es 15 gewesen.
_INFO_SPERRE = pd.Timedelta(minutes=60)


class _LiveExecutionMonitor:
    """Schnelle, vorlaeufige Planpruefung aus geglaetteten E3/DC-Livewerten."""

    def __init__(self, config, publisher, e3dc):
        self.config, self.publisher, self.e3dc = config, publisher, e3dc
        self.samples = []
        # Strangleistungen: eigener RSCP-Aufruf (~60 ms), deshalb NICHT im
        # 5-s-Takt. Einmal je Minute reicht fuer ein belastbares Slotmittel und
        # belastet den Watchdog nicht, der nebenher den Akkumodus halten muss.
        self.string_samples = []
        # None = noch nie gelesen. Nicht 0.0: monotonic() zaehlt ab Systemstart,
        # in der ersten Minute nach dem Booten griffe sonst schon die Sperre.
        self._string_last = None
        self._string_slot = None
        self.slot = None
        self.bad_count = self.good_count = 0
        self.alarm = False
        self.explained = None
        self.last_state = None
        self.last_cause = None
        self.last_write = None
        # Letzte Info-Meldung je Ursachenart - bewusst NICHT je Slot.
        self._info_zuletzt = {}

    def _sample_strings(self, now) -> None:
        """Strangleistungen sammeln und beim Slotwechsel als Mittel schreiben.

        Der Sammler erkennt den Slot selbst - so braucht die Zyklusschleife
        nichts davon zu wissen, und ein uebersprungener Zyklus verliert nur die
        Proben seines eigenen Slots.

        Mittel statt letzter Wert: eine Stichprobe am Slotanfang liegt auf der
        Vormittagsflanke systematisch daneben - derselbe Fehler, den die
        Leistungs-Istwerte hatten.
        """
        slot = floor_time(now, f"{self.config.general.slot_minutes}min")
        if self._string_slot is not None and slot != self._string_slot:
            self._flush_strings(self._string_slot)
        self._string_slot = slot

        jetzt = _time.monotonic()
        if (self._string_last is not None
                and jetzt - self._string_last < STRING_SAMPLE_S):
            return
        self._string_last = jetzt
        try:
            werte = self.e3dc.read_strings()
        except Exception as exc:                        # pragma: no cover
            log.debug("Strangwerte nicht lesbar (%s).", exc)
            return
        if werte:
            self.string_samples.append(werte)

    def _flush_strings(self, slot) -> int:
        proben, self.string_samples = self.string_samples, []
        if not proben:
            return 0
        summe: dict = {}
        for probe in proben:
            for index, watt in probe.items():
                summe.setdefault(index, []).append(watt)
        mittel = {i: float(np.mean(w)) for i, w in summe.items() if w}
        try:
            from .local_history import write_pv_strings
            return write_pv_strings(
                self.config.e3dc_rscp.history_db_path, slot, mittel)
        except Exception as exc:                        # pragma: no cover
            log.debug("Strangwerte nicht archivierbar (%s).", exc)
            return 0

    @staticmethod
    def _interpolated_median(samples, field: str, window_s: float,
                             step_s: float, max_gap_s: float):
        points = []
        for ts, values in samples:
            try:
                value = float(values[field])
            except (KeyError, TypeError, ValueError):
                continue
            if np.isfinite(value):
                points.append((pd.Timestamp(ts), value))
        if len(points) < 2:
            return None
        points.sort(key=lambda item: item[0])
        end = points[-1][0]
        points = [(ts, value) for ts, value in points
                  if (end - ts).total_seconds() <= max(window_s, step_s)]
        if len(points) < 2:
            return None
        origin = points[0][0]
        xp = np.asarray([(ts - origin).total_seconds() for ts, _ in points])
        fp = np.asarray([value for _, value in points], dtype=float)
        grid_start = max(0.0, xp[-1] - max(window_s, step_s))
        grid = np.arange(grid_start, xp[-1] + step_s * 0.25,
                         max(1.0, step_s))
        values = []
        for target in grid:
            pos = int(np.searchsorted(xp, target))
            if pos < len(xp) and abs(xp[pos] - target) < 1e-6:
                values.append(fp[pos])
            elif 0 < pos < len(xp) and xp[pos] - xp[pos - 1] <= max_gap_s:
                ratio = (target - xp[pos - 1]) / (xp[pos] - xp[pos - 1])
                values.append(fp[pos - 1] + ratio * (fp[pos] - fp[pos - 1]))
        return float(np.median(values)) if values else None

    @staticmethod
    def _window_energy_wh(samples, field: str, max_gap_s: float) -> float:
        points = []
        for ts, values in samples:
            try:
                value = float(values[field])
            except (KeyError, TypeError, ValueError):
                continue
            if np.isfinite(value):
                points.append((pd.Timestamp(ts), value))
        points.sort(key=lambda item: item[0])
        energy = 0.0
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            seconds = (t1 - t0).total_seconds()
            if 0.0 < seconds <= max_gap_s:
                energy += 0.5 * (v0 + v1) * seconds / 3600.0
        return energy

    def _deviation_cause(self, delta, planned, mon):
        """Benennt die Ursache, wenn die Abweichung NICHT am Regelpfad liegt.

        Zwei Ursachen, am 13./14.08.2026 auf dieser Anlage ausgemessen:

        * **Lastsprung** - ein Verbraucher (Ofen, Herd) geht in einem einzigen
          5-s-Sample an, hier 1200 -> 3660 W. Der E3DC deckt ihn korrekt aus dem
          Akku, das Netz bleibt bei 0 W. Die Akkuabweichung IST dann die
          Lastabweichung: die Summe beider Deltas lag bei 10 W. Der Regelpfad
          ist unschuldig, die Lastprognose hat den Verbraucher nicht gesehen.
        * **SoC-Grenze** - der Akku stand real bei 4,4 %, unter
          ``min_soc_percent`` = 10. Der Plan wollte deshalb halten (0 W), das
          Geraet entlud weiter ~800 W: seine Eigenreserve liegt tiefer als die
          EMS-Grenze. An der Grenze kann das Geraet dem Plan nicht folgen.

        Beides bleibt eine Abweichung und wird auch so protokolliert - aber als
        Info mit Ursache statt als Regelwarnung. Zaehler, Audit-Zustand und der
        Neuberechnungs-Trigger (der am NETZ haengt, nicht hier) bleiben
        unberuehrt.
        """
        last_ist = self._interpolated_median(
            self.samples, "house_load_w", mon.execution_live_window_seconds,
            mon.execution_live_sample_seconds,
            mon.execution_live_max_gap_seconds)
        plan_last = planned.get("load_w")
        if last_ist is not None and plan_last is not None:
            d_last = float(last_ist) - float(plan_last)
            rest = delta + d_last
            # Der Rest wird ENGER geprueft als die Abweichung selbst: mit der
            # vollen Toleranz koennte sich eine echte Regelabweichung von bis zu
            # 1500 W hinter einem Lastsprung verstecken.
            if abs(rest) <= 0.5 * float(mon.execution_battery_tolerance_w):
                return ("lastsprung",
                        f"unprognostizierter Lastsprung {d_last:+.0f} W "
                        f"(ungeklaerter Rest nur {rest:+.0f} W)")
        soc = self.samples[-1][1].get("soc_percent") if self.samples else None
        hb = self.config.house_battery
        if soc is not None:
            soc = float(soc)
            if soc <= hb.min_soc_percent + 1.0:
                return ("soc_unten",
                        f"SoC {soc:.1f} % an der Untergrenze "
                        f"{hb.min_soc_percent:.0f} % - dort kann das Geraet dem "
                        f"Plan nicht folgen")
            if soc >= hb.max_soc_percent - 1.0:
                return ("soc_oben",
                        f"SoC {soc:.1f} % an der Obergrenze "
                        f"{hb.max_soc_percent:.0f} % - dort kann das Geraet dem "
                        f"Plan nicht folgen")
        return None

    def sample(self, now=None):
        mon = self.config.monitoring
        audit_enabled = bool(
            mon.execution_audit_enabled and mon.execution_live_enabled)
        nowcast_enabled = bool(
            self.config.forecast.live_nowcast_enabled)
        if (not audit_enabled and not nowcast_enabled) or self.e3dc is None:
            return None
        now = pd.Timestamp(now if now is not None else
                           pd.Timestamp.now(tz=self.config.general.timezone))
        if now.tzinfo is None:
            now = now.tz_localize(self.config.general.timezone)
        else:
            now = now.tz_convert(self.config.general.timezone)
        try:
            live = self.e3dc.read_live(force=True)
        except Exception as exc:
            # Fehlende Livewerte duerfen den EMS-Zyklus nie beenden. Die
            # bestaetigte Zählerprüfung läuft unabhängig davon später weiter.
            log.debug("Vorläufige Live-Ausführungsprüfung ohne Daten (%s).", exc)
            return None
        if not live:
            return None
        if nowcast_enabled:
            try:
                from .local_history import write_live_sample
                write_live_sample(
                    self.config.e3dc_rscp.history_db_path, now, live)
            except Exception as exc:
                log.debug("E3DC-Livewert nicht archivierbar (%s).", exc)
        self._sample_strings(now)
        if not audit_enabled:
            return live
        from .local_history import (read_execution_plan_slot,
                                    write_execution_audit)
        slot = floor_time(now, f"{self.config.general.slot_minutes}min")
        planned = read_execution_plan_slot(
            self.config.e3dc_rscp.history_db_path, slot)
        if not planned or planned.get("battery_w") is None:
            return live
        if self.slot != slot:
            self.slot, self.samples = slot, []
            self.bad_count = self.good_count = 0
            self.last_state = self.last_write = self.last_cause = None
            self.explained = None
        self.samples.append((now, dict(live)))
        keep_s = max(mon.execution_live_window_seconds,
                     mon.execution_live_max_gap_seconds) + 10.0
        self.samples = [(ts, value) for ts, value in self.samples
                        if (now - ts).total_seconds() <= keep_s]
        actual_w = self._interpolated_median(
            self.samples, "battery_w", mon.execution_live_window_seconds,
            mon.execution_live_sample_seconds,
            mon.execution_live_max_gap_seconds)
        span_s = ((self.samples[-1][0] - self.samples[0][0]).total_seconds()
                  if len(self.samples) > 1 else 0.0)
        settled = ((now - slot).total_seconds() >= mon.execution_live_settle_seconds
                   and span_s >= min(mon.execution_live_settle_seconds,
                                     mon.execution_live_window_seconds))
        if actual_w is None or not settled:
            return live
        planned_w = float(planned["battery_w"])
        if not np.isfinite(planned_w):
            return live
        delta = actual_w - planned_w
        bad = abs(delta) > float(mon.execution_battery_tolerance_w)
        if bad:
            self.bad_count += 1
            self.good_count = 0
        else:
            self.good_count += 1
            self.bad_count = 0
        consecutive = max(1, int(mon.execution_live_consecutive))
        # Erklärbare Abweichungen (Lastsprung, SoC-Grenze) sind keine
        # Regelfehler. Als Warnung erzogen sie nur dazu, die Meldung zu
        # überlesen - sechs am Tag auf dieser Anlage.
        erklaerung = (self._deviation_cause(delta, planned, mon)
                      if bad and self.bad_count >= consecutive else None)
        ursache_art, ursache = erklaerung if erklaerung else (None, None)
        if bad and self.bad_count >= consecutive and ursache is not None:
            zuletzt = self._info_zuletzt.get(ursache_art)
            if (self.publisher is not None
                    and (zuletzt is None or now - zuletzt >= _INFO_SPERRE)):
                self.publisher.publish_alert(
                    "info", "EMS-Live-Abweichung mit bekannter Ursache: "
                    f"{ursache}. Akku Soll {planned_w:.0f} W, Ist-Median "
                    f"{actual_w:.0f} W.")
                self._info_zuletzt[ursache_art] = now
            self.explained = ursache_art
        elif bad and self.bad_count >= consecutive and not self.alarm:
            self.alarm = True
            self.explained = None
            if self.publisher is not None:
                self.publisher.publish_alert(
                    "warning", "Vorläufige EMS-Live-Abweichung: Akku Soll "
                    f"{planned_w:.0f} W, Ist-Median {actual_w:.0f} W. "
                    "Bestätigung folgt mit E3/DC-Zählerenergie.")
        elif not bad and self.good_count >= consecutive:
            self.explained = None
            if self.alarm:
                self.alarm = False
                if self.publisher is not None:
                    self.publisher.publish_alert(
                        "info", "EMS-Live-Ausführung folgt wieder dem Sollplan.")
        state = ("live_deviation" if self.alarm else
                 "live_suspect" if bad else "live_ok")
        ok = not self.alarm
        message = (f"Vorläufige Live-Abweichung Akku: Soll {planned_w:.0f} W, "
                   f"Ist-Median {actual_w:.0f} W."
                   + (f" Ursache: {ursache}." if ursache else "")
                   if bad else "Vorläufige Live-Prüfung innerhalb der Toleranz.")
        energy_wh = self._window_energy_wh(
            self.samples, "battery_w", mon.execution_live_max_gap_seconds)
        audit = {
            "checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "ok": ok, "state": state, "cause": "live",
            "message": message, "planned": planned,
            "actual": {"battery_w": round(actual_w, 1),
                       "window_battery_wh": round(energy_wh, 2),
                       "samples": len(self.samples),
                       "window_seconds": round(span_s, 1)},
            # Ursache in "deviations", nicht als eigener Schluessel: die
            # Audit-Tabelle hat ein festes Schema, alles andere fiele still weg.
            # Nur battery_w wird numerisch weiterverarbeitet (drift.py).
            "deviations": {"battery_w": round(delta, 1), "cause": ursache},
        }
        # Auch eine neu erkannte Ursache ist eine Aenderung: sie steht erst
        # fest, wenn die Entprellung durch ist - ohne diese Bedingung bliebe im
        # Audit die ursachenlose erste Probe stehen.
        changed = state != self.last_state or ursache_art != self.last_cause
        due = (self.last_write is None
               or (now - self.last_write).total_seconds() >= 30.0)
        if changed or due:
            try:
                write_execution_audit(
                    self.config.e3dc_rscp.history_db_path, slot, audit)
                self.last_state, self.last_write = state, now
                self.last_cause = ursache_art
            except Exception as exc:
                log.debug("Vorläufiges Live-Audit nicht speicherbar (%s).", exc)
        return live
