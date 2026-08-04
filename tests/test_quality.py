"""Gemeinsame Kennzahlen-Konventionen (ems/quality.py).

Diese Tests nageln fest, was in dieser Codebasis mehrfach auseinandergelaufen
ist: die Leserichtung des Bias, die Plausibilitaetsgrenzen und die Regel, dass
SoC-Energie nie einem einzelnen Slot zugeordnet wird.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
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


# --------------------------------------------------------------------------- #
# Zeitstand-Disziplin
# --------------------------------------------------------------------------- #
# Bewertende Module duerfen eine Prognose NIE gegen den frischesten Stand
# vergleichen - der enthaelt die eigene Nowcast-Korrektur und die Kennzahl
# schmeichelt sich selbst. Genau diese Falle hatte der Drift-Monitor: er
# verglich gegen eine Prognose, die alle 15 min neu beim gemessenen SoC
# ansetzte, und meldete deshalb 0,7 pp, waehrend der Plan ueber eine Nacht um
# zweistellige Prozentpunkte danebenlag.
_EVALUATING_MODULES = (
    "ems/observability.py", "ems/pv_eval.py", "ems/drift.py",
    "ems/planvalue.py", "kalibrierung.py", "ems/archive.py",
)
# Diese Leser liefern den JUENGSTEN Stand und sind fuer Bewertungen tabu.
_LATEST_VINTAGE_READERS = ("read_pv_forecast(", "read_pv_forecast ")


def test_evaluating_modules_only_read_as_of_forecasts():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for name in _EVALUATING_MODULES:
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for reader in _LATEST_VINTAGE_READERS:
            if reader in text:
                offenders.append(f"{name}: {reader.strip()}")
    assert not offenders, (
        "Bewertung gegen den frischesten Prognosestand: " + ", ".join(offenders)
        + " - stattdessen read_*_asof oder read_optimizer_forecast_snapshots")


def test_as_of_readers_exist_and_are_used():
    """Gegenprobe: der erlaubte Weg muss auch tatsaechlich benutzt werden,
    sonst geht der Test oben trivial durch."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = ("read_optimizer_forecast_asof", "read_group_asof",
               "read_optimizer_forecast_snapshots", "read_pv_forecast_asof")
    used = set()
    for name in _EVALUATING_MODULES:
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        used.update(reader for reader in allowed if reader in text)
    assert len(used) >= 3, f"nur {sorted(used)} genutzt"


def test_guard_report_marks_an_inactive_check():
    """Ein Waechter, der nichts geprueft hat, muss das SAGEN - drei Fehler
    dieser Codebasis waren stillschweigend wirkungslose Pruefungen."""
    from ems.quality import guard_report
    active = guard_report("segment_degradation", 4, skipped=0, detail="ok")
    assert active["active"] is True and active["checked"] == 4
    idle = guard_report("segment_degradation", 0, skipped=4)
    assert idle["active"] is False
    assert "inaktiv" in idle["detail"] and idle["skipped"] == 4


def test_planned_soc_lands_one_slot_later_than_it_was_stored():
    """Der Plan-SoC eines Slots ist der Wert am SlotENDE.

    Der Optimierer schreibt ``soc[t+1]`` unter Slot t. Gemessen wird am
    Slotanfang. Ohne Verschiebung liegen beide Reihen um einen Slot versetzt -
    beim Laden sieht der Plan dann zu hoch aus, beim Entladen zu tief.
    """
    from ems.quality import planned_soc_on_measurement_axis

    index = pd.date_range("2026-08-04 07:00", periods=4, freq="15min",
                          tz="Europe/Berlin")
    planned = pd.Series([12.0, 15.0, 18.0, 21.0], index=index)
    out = planned_soc_on_measurement_axis(planned, 15)

    assert list(out.values) == [12.0, 15.0, 18.0, 21.0]   # Werte unveraendert
    assert list(out.index) == list(index + pd.Timedelta(minutes=15))
    # Der zum Slot 07:00 geplante Wert gehoert an 07:15.
    assert out.loc[index[0] + pd.Timedelta(minutes=15)] == 12.0


def test_perfectly_executed_plan_shows_no_soc_deviation():
    """Die Invariante, um die es geht: laeuft der Akku exakt wie geplant,
    muss die Abweichung null sein - nicht ein Slot-Hub."""
    from ems.quality import planned_soc_on_measurement_axis

    index = pd.date_range("2026-08-04 07:00", periods=5, freq="15min",
                          tz="Europe/Berlin")
    # Steiler Ladevormittag: 3 pp je Slot.
    planned = pd.Series([15.0, 18.0, 21.0, 24.0, 27.0], index=index)
    # Perfekte Ausfuehrung: der am Slotende geplante Wert wird zum Anfang des
    # NAECHSTEN Slots gemessen.
    measured = pd.Series([12.0, 15.0, 18.0, 21.0, 24.0], index=index)

    roh = (measured - planned).abs().mean()
    aligned = planned_soc_on_measurement_axis(planned, 15).reindex(index)
    korrigiert = (measured - aligned).abs().dropna()

    assert roh == pytest.approx(3.0)          # voller Slot-Hub aus dem Nichts
    assert korrigiert.max() == pytest.approx(0.0)
