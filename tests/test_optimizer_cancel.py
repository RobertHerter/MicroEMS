import threading
import time

import pulp

from ems.optimizer import (_WarmHiGHS, SolverCancelled, clear_solver_cancel,
                           request_solver_cancel, solver_is_running)


def test_highs_running_solve_can_be_cancelled(monkeypatch):
    class Model:
        cancelled = False

        def cancelSolve(self):
            self.cancelled = True

    class Problem:
        solverModel = Model()

    def blocking_call(_self, lp):
        while not lp.solverModel.cancelled:
            time.sleep(0.005)

    monkeypatch.setattr(pulp.HiGHS, "callSolver", blocking_call)
    clear_solver_cancel()
    errors = []

    def solve():
        try:
            _WarmHiGHS().callSolver(Problem())
        except Exception as exc:  # Ergebnis im Hauptthread pruefen
            errors.append(exc)

    thread = threading.Thread(target=solve)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not solver_is_running() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert solver_is_running()
    request_solver_cancel()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SolverCancelled)
    clear_solver_cancel()

