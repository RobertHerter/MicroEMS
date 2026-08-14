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
    # ems/battery_calibration.py - gealterte Energiekapazitaet. Grobe
    # Geraetespanne; die eigentliche Absicherung ist der Schrittdeckel
    # (Alterung geht langsam) und die Vertraeglichkeit mit der Entladeseite.
    "battery_capacity_wh": (1000.0, 100000.0),
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
    # Eine Zelle des Korrekturprofils (Stunde, Monat oder Monat x Stunde).
    # 12 Slots = drei Tage derselben Stunde. Ohne Gate entstand ein voll
    # angewandter Faktor schon aus EINEM Slot - beim Monatswechsel also aus
    # dem ersten Tag des neuen Monats, genau dort, wo das Profil saisonal
    # umschwenken soll.
    "profile_cell": 12,
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


def planned_soc_on_measurement_axis(planned, slot_minutes: float):
    """Plan-SoC auf die Zeitachse der MESSUNG legen.

    Der Optimierer schreibt je Slot den SoC am ENDE des Slots
    (``soc_v = val(soc[t + 1])`` in ``ems/optimizer.py``), gemessen wird am
    ANFANG. Beide Reihen unbesehen auf denselben Index zu legen verschiebt sie
    um genau einen Slot gegeneinander: beim Laden erscheint der Plan zu hoch,
    beim Entladen zu tief, und das Vorzeichen kippt genau dort, wo der Akku
    von Entladen auf Laden umschaltet.

    Sichtbar wird das mit der Steilheit der Kurve. An einem Sommermorgen mit
    3 pp Hub je Slot entsteht so eine scheinbare Abweichung von 3 pp aus dem
    Nichts - gemessen am 04.08.2026 sank der MAE gegen die Messung von 1,24 pp
    auf 0,43 pp, sobald die Reihen richtig lagen.

    Der Planwert von Slot t gehoert an den Zeitpunkt t+1. Verschoben wird
    deshalb der PLAN, nicht die Messung: die gespeicherte Spalte bleibt
    unangetastet (Historie, Ersparnisrechnung und Archiv haengen daran).
    """
    if planned is None or len(planned) == 0:
        return planned
    shifted = pd.Series(planned).copy()
    shifted.index = shifted.index + pd.Timedelta(minutes=float(slot_minutes))
    return shifted


def soc_energy_wh(soc_start_percent: float, soc_end_percent: float,
                  capacity_wh: float) -> float:
    """SoC-Differenz zweier ENDPUNKTE in Wh (positiv = Entnahme).

    Bewusst nur Endpunkt zu Endpunkt - genau um die slotweise Zuordnung zu
    verhindern, die an der Quantisierung scheitert.
    """
    return ((float(soc_start_percent) - float(soc_end_percent)) / 100.0
            * float(capacity_wh))


# --------------------------------------------------------------------------- #
# Wirksamkeit von Pruefungen
# --------------------------------------------------------------------------- #
# Drei Fehler dieser Codebasis hatten dieselbe Form: eine Pruefung, die
# stillschweigend NICHTS getan hat.
#   * die Schaetz-Maske des Preises war None und wurde als "veroeffentlicht"
#     gelesen - damit galt ein Schaetzwert als Boersenpreis,
#   * die Segmentschranke der Befoerderung lief ohne qualifizierten Abschnitt
#     ins Leere (worst = 0),
#   * der Drift-Monitor konnte einen systematischen Bias strukturell nicht
#     sehen und meldete jahrelang 0,7 pp.
# Ein Waechter, der sich selbst abschaltet, ist schlimmer als keiner: er
# erzeugt Vertrauen. Deshalb liefert jede Pruefung mit, WIE VIEL sie geprueft
# hat - und ein Test stellt sicher, dass der Zaehler im Normalfall > 0 ist.
def guard_report(name: str, checked: int, skipped: int = 0,
                 detail: str | None = None) -> dict:
    """Wirksamkeits-Bericht einer Pruefung.

    ``checked``  Zahl tatsaechlich gepruefter Einheiten (Slots, Segmente, ...)
    ``skipped``  Zahl uebersprungener Einheiten - wird NICHT verschwiegen
    ``active``   False heisst: die Pruefung hat nichts geprueft
    """
    report = {"guard": str(name), "checked": int(checked),
              "skipped": int(skipped), "active": int(checked) > 0}
    if detail:
        report["detail"] = str(detail)
    if not report["active"]:
        report["detail"] = (
            (detail + " · " if detail else "")
            + "Schranke inaktiv - nichts geprueft")
    return report
