"""Gemeinsame Kennzahlen-Konventionen (ems/quality.py).

Diese Tests nageln fest, was in dieser Codebasis mehrfach auseinandergelaufen
ist: die Leserichtung des Bias, die Plausibilitaetsgrenzen und die Regel, dass
SoC-Energie nie einem einzelnen Slot zugeordnet wird.
"""
from __future__ import annotations

import numpy as np
import pytest

from ems.quality import (BIAS_CONVENTION, BOUNDS, MIN_SAMPLES, bias_direction,
                         bias_w, clamp, enough, median_bias_w,
                         min_soc_window_wh, shortfall_note, soc_energy_wh,
                         soc_step_wh, within)


# --------------------------------------------------------------------------- #
# Vorzeichen
# --------------------------------------------------------------------------- #
def test_bias_is_predicted_minus_actual():
    """Positiv = das Modell sagt zu VIEL voraus. Genau diese Richtung war in
    drift.check_load_bias umgekehrt und wurde in der Karte falsch gelesen."""
    assert BIAS_CONVENTION == "predicted_minus_actual"
    assert bias_w([1000.0], [1200.0]) == pytest.approx(200.0)
    assert bias_w([1200.0], [400.0]) == pytest.approx(-800.0)


def test_bias_direction_names_the_side():
    assert bias_direction(200.0) == "Prognose zu hoch"
    assert bias_direction(-800.0) == "Prognose zu niedrig"
    assert bias_direction(0.0) == "kein Versatz"
    assert bias_direction(float("nan")) == "unbekannt"
    assert bias_direction(5.0, tolerance=10.0) == "kein Versatz"


def test_bias_ignores_incomplete_pairs():
    """Ein einzelnes NaN darf die Kennzahl nicht auf NaN ziehen."""
    value = bias_w([100.0, np.nan, 300.0], [150.0, 200.0, 350.0])
    assert value == pytest.approx(50.0)
    assert np.isnan(bias_w([], []))


def test_median_bias_is_robust_against_a_spike():
    """Eine Lastspitze ist kein Prognosefehler, ein verschobener Sockel schon."""
    actual = [1000.0] * 9 + [5000.0]
    predicted = [900.0] * 10
    assert median_bias_w(actual, predicted) == pytest.approx(-100.0)
    assert bias_w(actual, predicted) < -400.0     # Mittel kippt, Median nicht


def test_every_module_uses_the_shared_helper():
    """Regressionsschutz: die Bias-Berechnung darf nicht wieder je Modul
    eigenhaendig geschrieben werden."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    files = [root / "ems" / "observability.py", root / "ems" / "pv_eval.py",
             root / "kalibrierung.py",
             root / "ems" / "calibration_competition.py"]
    pattern = re.compile(r"np\.mean\(\s*p(red)? - a\b")
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{path.name} rechnet Bias selbst"
        assert "bias_w" in text, f"{path.name} nutzt den Helfer nicht"


# --------------------------------------------------------------------------- #
# Plausibilitaetsgrenzen
# --------------------------------------------------------------------------- #
def test_bounds_are_ordered_and_finite():
    for name, (low, high) in BOUNDS.items():
        assert np.isfinite(low) and np.isfinite(high), name
        assert low < high, name


def test_clamp_and_within():
    assert clamp("discharge_efficiency", 1.5) == pytest.approx(0.98)
    assert clamp("discharge_efficiency", 0.1) == pytest.approx(0.55)
    assert clamp("unbekannt", 42.0) == pytest.approx(42.0)
    assert within("discharge_efficiency", 0.79) is True
    assert within("discharge_efficiency", 1.06) is False
    assert within("discharge_efficiency", None) is False
    assert within("discharge_efficiency", float("nan")) is False


def test_modules_reference_the_registry():
    """Die Grenzen lagen in fuenf Modulen unter vier Namen - eine fehlte."""
    from ems.battery_calibration import EFF_BOUNDS
    from ems.calibration_competition import FACTOR_BOUNDS
    from ems.load_learning import DURATION_BOUNDS_MIN, ENERGY_BOUNDS_KWH
    assert EFF_BOUNDS == BOUNDS["discharge_efficiency"]
    assert FACTOR_BOUNDS == BOUNDS["load_correction_factor"]
    assert DURATION_BOUNDS_MIN == BOUNDS["load_profile_minutes"]
    assert ENERGY_BOUNDS_KWH == BOUNDS["load_profile_kwh"]


# --------------------------------------------------------------------------- #
# Mindeststichproben
# --------------------------------------------------------------------------- #
def test_sample_gates():
    assert enough("drift_window", 96) is True
    assert enough("drift_window", 95) is False
    assert "sammelt noch" in shortfall_note("nowcast", 4)
    assert str(MIN_SAMPLES["nowcast"]) in shortfall_note("nowcast", 4)


# --------------------------------------------------------------------------- #
# SoC-Quantisierung
# --------------------------------------------------------------------------- #
def test_soc_step_is_one_percent_of_capacity():
    """Bei 22,3 kWh ist ein gemeldeter Schritt ~223 Wh - mehr als ein Slot bei
    800 W bewegt. Daher nie slotweise zuordnen."""
    assert soc_step_wh(22344.0) == pytest.approx(223.44)
    assert min_soc_window_wh(22344.0) == pytest.approx(1117.2)
    # Kleiner Speicher: die Schranke skaliert mit, statt bei 1 kWh zu kleben.
    assert min_soc_window_wh(5000.0) == pytest.approx(250.0)


def test_soc_energy_uses_endpoints_only():
    assert soc_energy_wh(95.0, 45.0, 22344.0) == pytest.approx(11172.0)
    assert soc_energy_wh(45.0, 95.0, 22344.0) == pytest.approx(-11172.0)
