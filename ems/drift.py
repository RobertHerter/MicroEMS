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
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd

from .config import Config

log = logging.getLogger("ems.drift")


class DriftMonitor:
    def __init__(self, config: Config):
        self.cfg = config
        self.window_h = float(getattr(config.monitoring, "drift_window_hours", 12.0))
        self.alert_pp = float(getattr(config.monitoring, "drift_alert_percent", 8.0))

    def check(self, repo, now: pd.Timestamp) -> Optional[float]:
        """Vergleicht Prognose- und Ist-SoC im zurückliegenden Fenster.
        Rückgabe: MAE in Prozentpunkten, oder None (zu wenig Daten)."""
        if not repo.signal_available("battery_soc"):
            return None
        start = now - timedelta(hours=self.window_h)
        try:
            actual = repo.read_slots("battery_soc", start, now, fill=False).dropna()
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
