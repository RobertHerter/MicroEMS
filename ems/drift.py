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
