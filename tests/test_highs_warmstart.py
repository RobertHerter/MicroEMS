"""HiGHS-Warmstart: duenne Startloesung und Sicherheitsnetz.

highspy 1.16.0 uebernahm den frueher dicht uebergebenen Start (alle
unbekannten Spalten = 0, meist unzulaessig) in einen falschen "Optimal"-Plan:
gemeldetes Ziel -863,25, die gelieferten Werte ergaben -791,25, das echte
Optimum lag bei -848,29 (test_short_term_plan_stability_prefers_published_slot).
Seitdem gehen nur die bekannten Spalten hinein, und nach jedem Lauf wird der
gemeldete Zielwert gegen die gelieferten Werte geprueft.
"""
import logging
from types import SimpleNamespace

import highspy
import pulp
import pytest

import ems.optimizer as O
from ems.lp_compat import lp_var
from ems.optimizer import _WarmHiGHS, clear_solver_cancel


@pytest.fixture(autouse=True)
def _kein_abbruch():
    clear_solver_cancel()
    yield
    clear_solver_cancel()


def _problem():
    """Kleines MILP, Optimum 3 bei x=4, y=0, b=1."""
    prob = pulp.LpProblem("warmstart", pulp.LpMinimize)
    x = lp_var(prob, "x", 0, 10)
    y = lp_var(prob, "y", 0, 10)
    b = lp_var(prob, "b", cat=pulp.LpBinary)
    prob += 2 * x + 3 * y - 5 * b
    prob += x + y >= 4
    prob += x <= 10 * b
    return prob


def _zaehle(monkeypatch, methode):
    aufrufe = []
    orig = getattr(highspy.Highs, methode)

    def spion(self, *args):
        aufrufe.append(args)
        return orig(self, *args)

    monkeypatch.setattr(highspy.Highs, methode, spion)
    return aufrufe


def test_warmstart_uebergibt_nur_bekannte_spalten(monkeypatch):
    aufrufe = _zaehle(monkeypatch, "setSolution")
    prob = _problem()
    prob.solve(_WarmHiGHS(warm_values={"y": 4.0, "gibt_es_nicht_7": 1.0}, msg=0))
    assert len(aufrufe) == 1
    anzahl, spalten, werte = aufrufe[0]          # duenn: Anzahl, Index, Wert
    assert anzahl == 1
    assert list(werte) == [4.0]
    assert len(spalten) == 1
    assert pulp.value(prob.objective) == pytest.approx(3.0)


def test_ohne_bekannte_spalten_kein_start(monkeypatch):
    aufrufe = _zaehle(monkeypatch, "setSolution")
    prob = _problem()
    prob.solve(_WarmHiGHS(warm_values={"gibt_es_nicht": 1.0}, msg=0))
    assert aufrufe == []
    assert pulp.value(prob.objective) == pytest.approx(3.0)


def test_zielwert_einer_echten_loesung_ist_stimmig():
    prob = _problem()
    prob.solve(_WarmHiGHS(warm_values={"y": 4.0}, msg=0))
    assert O._highs_ziel_abweichung(prob.solverModel) == pytest.approx(0.0, abs=1e-9)
    assert O._highs_ziel_stimmig(prob.solverModel)


def _fake_highs(gemeldet, kosten, werte, gueltig=True, offset=0.0):
    return SimpleNamespace(
        getSolution=lambda: SimpleNamespace(value_valid=gueltig, col_value=werte),
        getInfo=lambda: SimpleNamespace(objective_function_value=gemeldet),
        getLp=lambda: SimpleNamespace(col_cost_=kosten, offset_=offset))


def test_abweichung_rechnet_ziel_aus_spaltenwerten():
    # 1*1 + 3*2 + Offset 2 = 9, gemeldet 10 -> 1 ct daneben
    h = _fake_highs(10.0, [1.0, 3.0], [1.0, 2.0], offset=2.0)
    assert O._highs_ziel_abweichung(h) == pytest.approx(1.0)
    assert not O._highs_ziel_stimmig(h)
    # Rundungsrauschen unterhalb der Toleranz zaehlt nicht
    h = _fake_highs(9.001, [1.0, 3.0], [1.0, 2.0], offset=2.0)
    assert O._highs_ziel_stimmig(h)


def test_ohne_loesung_keine_abweichung():
    h = _fake_highs(float("inf"), [1.0], [0.0], gueltig=False)
    assert O._highs_ziel_abweichung(h) is None
    assert O._highs_ziel_stimmig(h)


def test_unstimmiges_ziel_mit_warmstart_rechnet_kalt_neu(monkeypatch, caplog):
    antworten = iter([False, True])          # erster Lauf faul, zweiter gut
    monkeypatch.setattr(O, "_highs_ziel_stimmig", lambda h: next(antworten))
    monkeypatch.setattr(O, "_highs_ziel_abweichung", lambda h: 72.0)
    laeufe = _zaehle(monkeypatch, "run")
    geleert = _zaehle(monkeypatch, "clearSolver")
    prob = _problem()
    with caplog.at_level(logging.WARNING, logger="ems.optimizer"):
        prob.solve(_WarmHiGHS(warm_values={"y": 4.0}, msg=0))
    assert len(laeufe) == 2
    assert len(geleert) == 1
    assert "rechne kalt neu" in caplog.text
    assert pulp.value(prob.objective) == pytest.approx(3.0)


def test_unstimmiges_ziel_ohne_warmstart_nur_warnung(monkeypatch, caplog):
    monkeypatch.setattr(O, "_highs_ziel_stimmig", lambda h: False)
    monkeypatch.setattr(O, "_highs_ziel_abweichung", lambda h: 72.0)
    laeufe = _zaehle(monkeypatch, "run")
    geleert = _zaehle(monkeypatch, "clearSolver")
    prob = _problem()
    with caplog.at_level(logging.WARNING, logger="ems.optimizer"):
        prob.solve(_WarmHiGHS(msg=0))
    assert len(laeufe) == 1
    assert geleert == []
    assert "ohne Warmstart" in caplog.text
