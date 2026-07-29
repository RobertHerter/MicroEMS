"""Predicted-vs-Actual-Drift: vergleicht den prognostizierten Haus-SoC-Verlauf
mit dem tatsächlich gemessenen und meldet systematische Abweichung.

Modell gegen Realität: Jeder Zyklus schreibt seine SoC-Prognose nach
ems_prediction. Für vergangene Slots steht dort die Prognose, die zu jenem
Zeitpunkt für den (damals aktuellen) Slot galt. Der Vergleich mit dem echten
battery_soc deckt Modellfehler auf, die kein einzelner Plan zeigt: falsche
Wirkungsgrade, Standby, Kapazitätsalterung, nicht ausgeführte Sollwerte.

Kennzahl je Lauf: MAE über das Fenster (Prozentpunkte SoC), nach ems_drift
geschrieben. Übersteigt sie den Schwellwert -> Warnung (ems/alert).
Kein harter Eingriff: reines Monitoring.

WICHTIG - was dieser Vergleich NICHT findet: jeder Zyklus rechnet seine Prognose
neu ab dem GEMESSENEN SoC. Der Fehler je Punkt bleibt damit klein, auch wenn ein
Modellparameter systematisch daneben liegt; er summiert sich in dieser Kennzahl
nie auf. Ein zu hoher Entladewirkungsgrad (0.93 statt real 0.79) erzeugte hier
0,7 pp MAE und blieb monatelang unbemerkt, obwohl der Plan über eine Nacht um
zweistellige Prozentpunkte danebenlag.

Deshalb prüft ``check_energy_model`` zusätzlich die BILANZ statt der Kurve: wie
viel SoC eine ans Haus gelieferte kWh real kostet, verglichen mit dem
konfigurierten Wirkungsgrad. Dieser Vergleich ist gegen die Neuberechnung
immun und schlägt binnen ein, zwei Tagen an.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import pandas as pd

from .config import Config

log = logging.getLogger("ems.drift")


class DriftMonitor:
    def __init__(self, config: Config):
        self.cfg = config
        self.window_h = float(getattr(config.monitoring, "drift_window_hours", 12.0))
        self.alert_pp = float(getattr(config.monitoring, "drift_alert_percent", 8.0))
        self.eff_days = float(getattr(
            config.monitoring, "efficiency_window_days", 7.0))
        self.eff_alert = float(getattr(
            config.monitoring, "efficiency_alert_percent", 6.0))
        self.exec_days = float(getattr(
            config.monitoring, "execution_bias_window_days", 7.0))
        self.exec_alert_w = float(getattr(
            config.monitoring, "execution_bias_alert_w", 50.0))

    def check(self, repo, now: pd.Timestamp) -> Optional[float]:
        """Vergleicht Prognose- und Ist-SoC im zurückliegenden Fenster.
        Rückgabe: MAE in Prozentpunkten, oder None (zu wenig Daten)."""
        if not (repo.signal_available("battery_soc")
                or self.cfg.e3dc_rscp.history_source):
            return None
        start = now - timedelta(hours=self.window_h)
        try:
            from .local_history import read_actual_signal
            actual = read_actual_signal(self.cfg, repo, "battery_soc",
                                        start, now).dropna()
            pred = repo.read_slots_output("predicted_state", "house_soc_percent",
                                          start, now)
        except Exception as exc:  # pragma: no cover
            log.warning("Drift-Check nicht möglich (%s).", exc)
            return None
        if pred is None or pred.empty or actual.empty:
            return None
        df = pd.DataFrame({"pred": pred, "act": actual}).dropna()
        if len(df) < 4:
            return None
        err = (df["pred"] - df["act"])
        mae = float(err.abs().mean())
        bias = float(err.mean())
        try:
            repo.write_frame("drift", pd.DataFrame(
                {"soc_mae_pp": [round(mae, 3)], "soc_bias_pp": [round(bias, 3)],
                 "n": [len(df)]}, index=[now]))
        except Exception as exc:  # pragma: no cover
            log.warning("Drift-Kennzahl konnte nicht geschrieben werden (%s).", exc)
        log.info("SoC-Drift (Prognose-Ist): MAE %.1f pp, Bias %+.1f pp (%d Slots).",
                 mae, bias, len(df))
        return mae

    def check_energy_model(self, now: pd.Timestamp) -> Optional[dict]:
        """Entladewirkungsgrad aus den Ist-Werten gegen den Modellwert prüfen.

        Gemessen wird über zusammenhängende Entladephasen (siehe
        ``ems/battery_calibration.py``) - je Slot ginge es nicht, weil der SoC
        nur in ganzen Prozent gemeldet wird.

        Rückgabe: Kennzahlen inkl. ``deviation_percent`` (relativ) und
        ``alert`` (bool), oder None bei zu dünner Stichprobe.
        """
        from .battery_calibration import fit_discharge_efficiency
        from .local_history import read_actual

        hb = self.cfg.house_battery
        db = self.cfg.e3dc_rscp.history_db_path
        tz = self.cfg.general.timezone
        start = now - timedelta(days=self.eff_days)
        try:
            frame = pd.DataFrame({
                "battery_w": read_actual(db, "battery_w", start, now, tz),
                "soc": read_actual(db, "soc", start, now, tz)})
            fit = fit_discharge_efficiency(
                frame, hb.capacity_wh, self.cfg.general.dt_hours,
                float(getattr(self.cfg.optimization,
                              "standby_discharge_w", 0.0)))
        except Exception as exc:  # pragma: no cover
            log.warning("Wirkungsgrad-Prüfung nicht möglich (%s).", exc)
            return None
        if not fit.usable:
            return None
        model = float(hb.discharge_efficiency)
        deviation = 100.0 * (fit.efficiency - model) / max(1e-6, model)
        out = {"measured": fit.efficiency, "model": round(model, 3),
               "deviation_percent": round(deviation, 1),
               "windows": fit.n_windows, "hours": fit.hours,
               "alert": abs(deviation) > self.eff_alert}
        # Nur die Abweichung ist interessant - im Normalfall still bleiben,
        # sonst geht die Meldung im Log unter.
        if out["alert"]:
            log.warning(
                "Entladewirkungsgrad: gemessen %.3f gegen Modell %.3f "
                "(%+.1f %%, %d Phasen / %.0f h). Der geplante SoC-Verlauf "
                "läuft dadurch systematisch weg - "
                "'python -m ems.battery_calibration --apply' führt ihn nach.",
                fit.efficiency, model, deviation, fit.n_windows, fit.hours)
        else:
            log.info("Entladewirkungsgrad: gemessen %.3f gegen Modell %.3f "
                     "(%+.1f %%).", fit.efficiency, model, deviation)
        return out

    def check_execution_bias(self, now: pd.Timestamp) -> Optional[dict]:
        """Einseitigen Ausführungsversatz über ein Fenster aufsummieren.

        Der Ausführungs-Audit prüft je Slot gegen die gereifte Zählerenergie -
        Zeitbezug korrekt, aber er summiert NICHT auf. Ein Versatz von wenigen
        Watt je Slot bleibt unter jeder Einzelschwelle und ist über eine Woche
        trotzdem kWh. Alarm gibt es nur, wenn der Versatz nennenswert UND
        einseitig ist (Rauschen wechselt das Vorzeichen).
        """
        from .local_history import read_execution_audits

        try:
            rows = read_execution_audits(
                self.cfg.e3dc_rscp.history_db_path,
                limit=int(self.exec_days * 24 * 60
                          / max(1, self.cfg.general.slot_minutes)) + 50)
        except Exception as exc:  # pragma: no cover
            log.warning("Ausführungs-Bias nicht prüfbar (%s).", exc)
            return None
        cutoff = now - timedelta(days=self.exec_days)
        values = []
        for row in rows or []:
            # Nur zählerbasierte Prüfungen: die Live-Variante mittelt ~1 min
            # und gegen einen 15-min-Sollwert gestellt streut sie um ±1 kW -
            # das wäre Rauschen, kein Versatz.
            if str(row.get("state", "")) not in ("ok", "deviation"):
                continue
            try:
                ts = pd.Timestamp(row.get("ts"))
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                if ts < cutoff:
                    continue
                dev = (row.get("deviations") or {}).get("battery_w")
                if dev is not None and pd.notna(dev):
                    values.append(float(dev))
            except Exception:      # einzelne kaputte Zeile ignorieren
                continue
        if len(values) < 96:                    # unter einem Tag nicht bewerten
            return None
        arr = pd.Series(values, dtype="float64")
        # Der MEDIAN entscheidet, nicht der Mittelwert: die Verteilung hat einen
        # schweren Rand (einzelne Slots weichen um >800 W ab). Der Mittelwert
        # meldete dadurch -67 W, obwohl die Anlage typischerweise auf -12 W
        # genau folgt. Beides wird ausgegeben, alarmiert wird auf dem Median.
        median_w = float(arr.median())
        mean_w = float(arr.mean())
        nonzero = arr[arr.abs() > 1.0]
        share = (float((nonzero > 0).mean()) if len(nonzero) else 0.5)
        one_sided = max(share, 1.0 - share)
        out = {"median_w": round(median_w, 1), "mean_w": round(mean_w, 1),
               "kwh_per_day": round(median_w * 24.0 / 1000.0, 2),
               "same_sign_share": round(one_sided, 2), "n": len(values),
               "alert": bool(abs(median_w) > self.exec_alert_w
                             and one_sided >= 0.65)}
        if out["alert"]:
            log.warning(
                "Ausführungs-Versatz: Median %+.0f W über %d Slots "
                "(%.0f %% gleiches Vorzeichen) = %+.2f kWh/Tag. Die Anlage "
                "folgt den Sollwerten systematisch anders als geplant.",
                median_w, len(values), 100.0 * one_sided, out["kwh_per_day"])
        else:
            log.info("Ausführungs-Versatz: Median %+.0f W, Mittel %+.0f W "
                     "(%d Slots, %.0f %% gleiches Vorzeichen).",
                     median_w, mean_w, len(values), 100.0 * one_sided)
        return out
