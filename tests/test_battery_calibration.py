"""Kalibrierung des Entladewirkungsgrads (ems/battery_calibration.py).

Der Knackpunkt ist die SoC-Aufloesung: gemeldet werden ganze Prozent (bei 22 kWh
rund 223 Wh je Schritt). Eine Slot-weise Auswertung liefert damit unsinnige
Werte - deshalb wird ueber ganze Entladephasen gemessen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.battery_calibration import (EFF_BOUNDS, discharge_windows,
                                     fit_discharge_efficiency, maybe_apply)
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
CAP = 22344.0
DT = 0.25


def _phase(start, hours, power_w, efficiency, capacity=CAP, quantise=True):
    """Entladephase mit BEKANNTEM Wirkungsgrad erzeugen.

    Der SoC wird wie beim echten Geraet auf ganze Prozent gerundet - genau die
    Quantisierung, an der eine naive Slot-Auswertung scheitert.
    """
    n = int(hours / DT)
    index = pd.date_range(start, periods=n + 1, freq="15min", tz=TZ)
    drawn = power_w * DT / efficiency          # Wh je Slot aus dem Speicher
    soc = 95.0 - np.arange(n + 1) * drawn / capacity * 100.0
    if quantise:
        soc = np.round(soc)
    return pd.DataFrame({"battery_w": [-power_w] * n + [0.0], "soc": soc},
                        index=index)


def test_fit_recovers_a_known_efficiency_despite_soc_quantisation():
    frame = pd.concat([_phase("2026-01-10 20:00", 10.0, 1200.0, 0.78),
                       _phase("2026-01-11 20:00", 11.0, 900.0, 0.78),
                       _phase("2026-01-12 20:00", 9.0, 1400.0, 0.78)])
    fit = fit_discharge_efficiency(frame, CAP, DT)
    assert fit.n_windows == 3
    assert fit.efficiency == pytest.approx(0.78, abs=0.02)
    assert fit.hours == pytest.approx(30.0, abs=0.5)


def test_short_phases_are_ignored():
    """Unter drei Stunden dominiert die 1-%-Quantisierung - solche Fenster
    duerfen den Wert nicht verfaelschen."""
    frame = _phase("2026-01-10 20:00", 1.0, 1200.0, 0.78)
    assert discharge_windows(frame, CAP, DT) == []
    assert fit_discharge_efficiency(frame, CAP, DT).efficiency is None


def test_charging_splits_the_phase():
    """Eine Ladephase dazwischen darf nicht ueberbrueckt werden."""
    a = _phase("2026-01-10 20:00", 5.0, 1000.0, 0.8)
    charge = pd.DataFrame(
        {"battery_w": [2000.0] * 8, "soc": [70.0] * 8},
        index=pd.date_range("2026-01-11 01:15", periods=8, freq="15min", tz=TZ))
    b = _phase("2026-01-11 03:15", 5.0, 1000.0, 0.8)
    windows = discharge_windows(pd.concat([a, charge, b]), CAP, DT)
    assert len(windows) == 2
    assert all(w.slots >= 12 for w in windows)


def test_time_gaps_split_the_phase():
    """Ueber einen Datenausfall darf nicht integriert werden - der SoC-Sprung
    waere sonst der falschen Energie zugeordnet."""
    a = _phase("2026-01-10 20:00", 4.0, 1000.0, 0.8)
    b = _phase("2026-01-11 06:00", 4.0, 1000.0, 0.8)   # 6 h Luecke
    windows = discharge_windows(pd.concat([a, b]), CAP, DT)
    assert len(windows) == 2


def test_standby_is_credited_to_the_delivered_energy():
    """Der Eigenverbrauch des Geraets steckt im SoC-Rueckgang, aber nicht in der
    ans Haus gelieferten Leistung - sonst faellt der Wirkungsgrad zu niedrig aus."""
    frame = _phase("2026-01-10 20:00", 10.0, 1000.0, 0.78)
    without = fit_discharge_efficiency(frame, CAP, DT, standby_w=0.0)
    with_sb = fit_discharge_efficiency(frame, CAP, DT, standby_w=40.0)
    assert with_sb.efficiency > without.efficiency


def test_empty_input_is_handled():
    empty = pd.DataFrame(columns=["battery_w", "soc"])
    fit = fit_discharge_efficiency(empty, CAP, DT)
    assert fit.efficiency is None and fit.n_windows == 0 and not fit.usable


# --------------------------------------------------------------------------- #
# Uebernahme ins Overlay
# --------------------------------------------------------------------------- #
def _fit_with(efficiency, n_windows=8, hours=90.0):
    from ems.battery_calibration import FitResult
    return FitResult(efficiency, n_windows, hours, 100.0, 120.0, 0.05, [])


def test_apply_blends_and_writes_to_the_overlay(tmp_path, monkeypatch):
    cfg = make_config()
    cfg.house_battery.discharge_efficiency = 0.93
    written = {}
    monkeypatch.setattr("ems.config.save_override",
                        lambda path, key, value: written.update({key: value}))
    changed = maybe_apply(_fit_with(0.78), cfg, str(tmp_path / "config.yaml"))
    # Gedaempft: 0.5 * 0.93 + 0.5 * 0.78 - eine einzelne Woche kippt die
    # Planung nicht komplett um.
    assert changed == {"discharge_efficiency": pytest.approx(0.855)}
    assert written == {"house_battery.discharge_efficiency":
                       pytest.approx(0.855)}


def test_apply_refuses_a_thin_sample(tmp_path, monkeypatch):
    cfg = make_config()
    monkeypatch.setattr("ems.config.save_override",
                        lambda *a, **k: pytest.fail("darf nicht schreiben"))
    assert maybe_apply(_fit_with(0.78, n_windows=2, hours=10.0), cfg,
                       str(tmp_path / "config.yaml")) is None


def test_apply_refuses_implausible_values(tmp_path, monkeypatch):
    """Werte ueber 1.0 sind physikalisch unmoeglich und deuten auf ein falsches
    Kapazitaets- oder SoC-Signal - dann lieber nichts uebernehmen."""
    cfg = make_config()
    monkeypatch.setattr("ems.config.save_override",
                        lambda *a, **k: pytest.fail("darf nicht schreiben"))
    for bad in (EFF_BOUNDS[0] - 0.01, EFF_BOUNDS[1] + 0.01, 1.5):
        assert maybe_apply(_fit_with(bad), cfg,
                           str(tmp_path / "config.yaml")) is None
