"""Gemeinsame Konventionen für Kennzahlen und Plausibilitätsgrenzen.

Zwei Dinge, die vorher verstreut lagen und dadurch auseinandergelaufen sind:

**Vorzeichen des Bias.** Projektweit gilt ``Prognose − Ist``: ein POSITIVER Bias
heißt "das Modell sagt zu viel voraus". Vorher rechneten ``observability``,
``pv_eval``, ``kalibrierung`` und ``DriftMonitor.check`` so, ``check_load_bias``
und der Ausführungsversatz aber umgekehrt - dasselbe Vorzeichen bedeutete in
derselben Datei Gegenteiliges, und eine Dashboard-Karte zeigte "+359 W" für eine
Prognose, die zu NIEDRIG lag. Deshalb gibt es hier genau eine Funktion dafür.

**Plausibilitätsgrenzen.** Jede Kalibrierung begrenzt, was sie übernehmen darf -
das ist der Schutz gegen ein kaputtes Signal, das sonst direkt in die Planung
läuft. Die Grenzen standen in fünf Modulen unter vier verschiedenen Namen, und
genau eine fehlte (``apply_load_profile`` multiplizierte ungeprüft). Gesammelt
ist auf einen Blick sichtbar, wo eine fehlt.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

# Verbindliche Leserichtung aller Bias-Kennzahlen im Projekt.
BIAS_CONVENTION = "predicted_minus_actual"


def bias_w(actual, predicted) -> float:
    """Mittlerer Bias als ``Prognose − Ist`` (positiv = Modell sagt zu viel).

    Nimmt Listen, Arrays oder Series. Nicht-endliche Paare fallen heraus, damit
    ein einzelnes NaN nicht die ganze Kennzahl auf NaN zieht.
    """
    a = np.asarray(actual, dtype="float64").ravel()
    p = np.asarray(predicted, dtype="float64").ravel()
    n = min(len(a), len(p))
    if n == 0:
        return float("nan")
    a, p = a[:n], p[:n]
    mask = np.isfinite(a) & np.isfinite(p)
    if not mask.any():
        return float("nan")
    return float(np.mean(p[mask] - a[mask]))


def bias_direction(bias: float, tolerance: float = 0.0) -> str:
    """Klartext-Richtung zu einem Bias nach obiger Konvention.

    Anzeigen sollen das benutzen statt das Vorzeichen selbst zu deuten.
    """
    if bias is None or not np.isfinite(float(bias)):
        return "unbekannt"
    if abs(float(bias)) <= float(tolerance):
        return "kein Versatz"
    return "Prognose zu hoch" if float(bias) > 0 else "Prognose zu niedrig"


def median_bias_w(actual, predicted) -> float:
    """Wie ``bias_w``, aber als Median - robust gegen einzelne Ausreißer.

    Für Lastspitzen (Herd, Wallbox) ist der Median die richtige Wahl: sie sind
    kein Prognosefehler, ein verschobener Sockel schon.
    """
    a = pd.Series(np.asarray(actual, dtype="float64").ravel())
    p = pd.Series(np.asarray(predicted, dtype="float64").ravel())
    n = min(len(a), len(p))
    if n == 0:
        return float("nan")
    diff = (p[:n].to_numpy() - a[:n].to_numpy())
    diff = diff[np.isfinite(diff)]
    return float(np.median(diff)) if len(diff) else float("nan")


# --------------------------------------------------------------------------- #
# Plausibilitätsgrenzen der Auto-Übernahmen
# --------------------------------------------------------------------------- #
# Jede Grenze schützt einen Übernahmepfad. Wird hier etwas ergänzt, gehört in
# das zugehörige maybe_apply() eine Prüfung dagegen - eine Kalibrierung ohne
# Grenze kann ein kaputtes Signal ungebremst in die Planung schreiben.
BOUNDS: dict[str, Tuple[float, float]] = {
    # ems/battery_calibration.py - Entladewirkungsgrad (gemessen 0.79)
    "discharge_efficiency": (0.55, 0.98),
    # ems/pool_calibration.py - Thermomodell
    "pool_loss_w_per_k": (30.0, 3000.0),
    "pool_solar_absorption": (0.05, 1.0),
    "pool_stage_heat_w": (300.0, 30000.0),
    # ems/load_learning.py - angelerntes Lastprofil
    "load_profile_minutes": (15.0, 720.0),
    "load_profile_kwh": (0.05, 30.0),
    # ems/calibration_competition.py - produktive Stundenfaktoren
    "load_correction_factor": (0.2, 5.0),
    # kalibrierung.py - Korrekturprofil der Lastprognose
    "hourly_correction": (0.6, 1.8),
    # ems/pv_eval.py - PV-Unsicherheitsband
    "pv_uncertainty": (0.02, 0.95),
}


def clamp(name: str, value: float) -> float:
    """Wert auf die registrierte Grenze begrenzen (unbekannter Name: unverändert)."""
    low, high = BOUNDS.get(name, (float("-inf"), float("inf")))
    return float(min(max(float(value), low), high))


def within(name: str, value) -> bool:
    """True, wenn der Wert innerhalb der registrierten Grenze liegt."""
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not np.isfinite(number):
        return False
    low, high = BOUNDS.get(name, (float("-inf"), float("inf")))
    return low <= number <= high


# --------------------------------------------------------------------------- #
# Mindeststichproben
# --------------------------------------------------------------------------- #
# "Keine Zahl ohne Stichprobe": unterhalb dieser Zahl liefert eine Kennzahl
# None plus Grund, statt einen Wert zu melden, hinter dem ein einzelner
# Messpunkt steht.
MIN_SAMPLES: dict[str, int] = {
    "drift_window": 96,          # ein Tag im 15-min-Raster
    "promotion": 96,
    "nowcast": 96,
}


def enough(name: str, n: int) -> bool:
    return int(n or 0) >= MIN_SAMPLES.get(name, 1)


def shortfall_note(name: str, n: int) -> str:
    need = MIN_SAMPLES.get(name, 1)
    return f"sammelt noch (n = {int(n or 0)} von {need})"


# --------------------------------------------------------------------------- #
# SoC-Quantisierung
# --------------------------------------------------------------------------- #
# Der E3/DC meldet den Ladezustand nur in GANZEN Prozent. Bei 22,3 kWh ist ein
# Schritt rund 223 Wh - mehr als ein 15-min-Slot bei 800 W Entladung bewegt.
# Daraus folgt eine harte Regel: SoC-Energie NIE einem einzelnen Slot zuordnen.
# Eine slotweise Auswertung greift genau die Slots heraus, in denen der Zaehler
# umspringt, und liefert Wirkungsgrade bis ueber 1,0 (real passiert, siehe
# ems/battery_calibration). Nur ueber zusammenhaengende Phasen mit mehreren
# Schritten Hub mittelt sich die Quantisierung heraus.
SOC_STEP_PERCENT = 1.0
# So viele Schritte muss ein Fenster mindestens umfassen, damit der relative
# Quantisierungsfehler unter etwa 10 % bleibt.
MIN_SOC_STEPS_PER_WINDOW = 5


def soc_step_wh(capacity_wh: float) -> float:
    """Energie hinter EINEM gemeldeten SoC-Schritt."""
    return float(capacity_wh) * SOC_STEP_PERCENT / 100.0


def min_soc_window_wh(capacity_wh: float) -> float:
    """Mindest-Hub eines Auswertefensters (siehe MIN_SOC_STEPS_PER_WINDOW)."""
    return soc_step_wh(capacity_wh) * MIN_SOC_STEPS_PER_WINDOW


def soc_energy_wh(soc_start_percent: float, soc_end_percent: float,
                  capacity_wh: float) -> float:
    """SoC-Differenz zweier ENDPUNKTE in Wh (positiv = Entnahme).

    Bewusst nur Endpunkt zu Endpunkt - genau um die slotweise Zuordnung zu
    verhindern, die an der Quantisierung scheitert.
    """
    return ((float(soc_start_percent) - float(soc_end_percent)) / 100.0
            * float(capacity_wh))
