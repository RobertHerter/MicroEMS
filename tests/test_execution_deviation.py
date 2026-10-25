"""Kleine echte Steuerabweichungen erkennen, Prognosefehler nicht umetikettieren."""
import pytest

from ems.execution import battery_execution_deviation


@pytest.mark.parametrize("planned,actual,expected", [
    # Mehr Hauslast: Eigenverbrauch darf staerker entladen als prognostiziert.
    ({"battery_w": -500}, {"battery_w": -620, "load_w": 620}, 0),
    # Weniger PV: die prognostizierte Ladeleistung ist nicht erreichbar.
    ({"battery_w": 1500}, {"battery_w": 500, "pv_w": 1000}, 0),
    # Die physikalischen Limits gelten trotzdem, auch bei passender Bilanz.
    ({"battery_w": -500, "discharge_limit_w": 0},
     {"battery_w": -120, "load_w": 620}, -120),
    ({"battery_w": 500, "charge_limit_w": 500},
     {"battery_w": 620, "pv_w": 1120}, 120),
    # Ausbleibendes Netzladen ist auch mit korrekter Energiebilanz ein Fehler.
    ({"battery_w": 1000, "grid_charge_w": 1000}, {"battery_w": 0}, -1000),
    # Physische SoC-Grenzen koennen Lade-/Entladeziele verhindern.
    ({"battery_w": 1000}, {"battery_w": 0, "pv_w": 2000, "soc": 98}, 0),
    ({"battery_w": -500}, {"battery_w": 0, "soc": 10}, 0),
    # Unreife Messungen und unbekannte Grenzen sind kein Erfolgsnachweis.
    ({}, {"load_w": 0}, None),
    ({"charge_limit_w": None}, {}, None),
])
def test_command_envelope(planned, actual, expected):
    p = {"battery_w": 0, "charge_limit_w": 5000, "discharge_limit_w": 5000,
         "grid_charge_w": 0, **planned}
    a = {"battery_w": 0, "pv_w": 0, "load_w": 500, "soc": 50, **actual}
    result = battery_execution_deviation(p, a, 10)
    assert result == (pytest.approx(expected) if expected is not None else None)
