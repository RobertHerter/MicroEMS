"""HiGHS teilt sich je Prozess einen Thread-Pool; Abbruch braucht einen Schalter.

16.11.2026: in der GitHub-CI scheiterten 11 Optimierer-Tests eines Workers mit
"Not Solved". Dort fehlt das System-CBC, planvalue rechnete sein Hilfs-LP mit
HiGHS und automatischer Thread-Zahl - danach verweigerte jeder Optimiererlauf
(threads=1) im selben Prozess die Arbeit. Bei der Suche fiel auf, dass
cancelSolve() mit highspy 1.16 ohne HandleUserInterrupt wirkungslos ist.
"""
import random
import threading
import time

import highspy
import pulp
import pytest

from ems import optimizer as O
from ems import planvalue
from ems.lp_compat import lp_solve, lp_var


def _rucksack(n, m, seed):
    rnd = random.Random(seed)
    prob = pulp.LpProblem("rucksack", pulp.LpMaximize)
    xs = [lp_var(prob, f"x{i}", cat=pulp.LpBinary) for i in range(n)]
    prob += pulp.lpSum((rnd.randint(1000, 2000) + rnd.randint(-5, 5)) * x
                       for x in xs)
    for k in range(m):
        gewichte = [rnd.randint(1000, 2000) for _ in xs]
        prob += (pulp.lpSum(g * x for g, x in zip(gewichte, xs))
                 <= sum(gewichte) // 2 + k)
    return prob


@pytest.fixture(autouse=True)
def _sauberer_zustand():
    O.clear_solver_cancel()
    yield
    O.clear_solver_cancel()
    highspy.Highs.resetGlobalScheduler(True)


def test_warmhighs_rechnet_ohne_angabe_mit_einem_thread():
    assert O._WarmHiGHS(msg=0).threads == 1
    assert O._WarmHiGHS(msg=0, threads=3).threads == 3


def test_planvalue_ersatzsolver_rechnet_mit_einem_thread(monkeypatch):
    monkeypatch.setattr(planvalue, "_LP_SOLVER", None)
    monkeypatch.setattr(pulp.COIN_CMD, "available", lambda self: False)
    monkeypatch.setattr(planvalue, "eingebauter_cbc", lambda: None)
    solver = planvalue._lp_solver()
    assert isinstance(solver, pulp.HiGHS)
    assert solver.threads == 1


def test_fremder_threadpool_laesst_den_optimierer_nicht_scheitern(caplog):
    # Ein fremder Aufruf legt den Pool mit drei Threads an ...
    assert lp_solve(_rucksack(60, 8, 1),
                    pulp.HiGHS(msg=False, threads=3, timeLimit=30)) == O.OPTIMAL
    # ... danach muss der Optimierer-Solver trotzdem rechnen.
    with caplog.at_level("WARNING", logger="ems.optimizer"):
        status = lp_solve(_rucksack(40, 5, 2), O._WarmHiGHS(msg=0, timeLimit=30))
    assert status == O.OPTIMAL
    assert "Thread-Pool" in caplog.text


def test_abbruch_beendet_einen_echten_lauf():
    prob = _rucksack(200, 20, 1)          # rechnet ohne Abbruch bis zum Limit
    ergebnis = {}

    def rechnen():
        try:
            ergebnis["status"] = lp_solve(prob, O._WarmHiGHS(msg=0, timeLimit=30))
        except Exception as exc:
            ergebnis["fehler"] = exc

    faden = threading.Thread(target=rechnen)
    start = time.monotonic()
    faden.start()
    grenze = start + 5.0
    while not O.solver_is_running() and time.monotonic() < grenze:
        time.sleep(0.01)
    time.sleep(0.5)
    O.request_solver_cancel()
    faden.join(timeout=20.0)
    assert not faden.is_alive()
    assert isinstance(ergebnis.get("fehler"), O.SolverCancelled), ergebnis
    assert time.monotonic() - start < 15.0
