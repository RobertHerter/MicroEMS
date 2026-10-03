"""PuLP 3 und PuLP 4 aus EINER Codebasis bedienen.

PuLP 4.0.0 (25.09.2026) hat die Schnittstelle an drei Stellen umgebaut, auf
die der Optimierer angewiesen ist:

* Variablen gehoeren einem Problem und entstehen ueber
  ``prob.add_variable(name, lowBound, upBound, cat)``. Den freistehenden
  Konstruktor ``pulp.LpVariable(name, low, up, cat)`` gibt es nicht mehr - mit
  4.0.0 scheiterte deshalb jeder Optimierungslauf an einem TypeError.
* Das Problem traegt keinen Status mehr (``prob.status``, ``pulp.LpStatus``
  entfallen); ``solve()`` liefert ein ``LpSolveStats`` mit feineren Zustaenden.
* ``var.index`` entfaellt; HiGHS-Spalten folgen ``lp.exported_variables()``.

Die Funktionen hier waehlen den Weg nach dem, was die installierte Version
kann. Derselbe Code laeuft unter beiden; der Wechsel haengt allein am Lock und
laesst sich ohne Codeaenderung zuruecknehmen.
"""
from __future__ import annotations

import pulp

# Die Texte, die PuLP 3 lieferte und die der Optimierer, die Datenbank
# (solver_runs.status) und das Dashboard verwenden.
OPTIMAL = "Optimal"
INFEASIBLE = "Infeasible"
UNBOUNDED = "Unbounded"
NOT_SOLVED = "Not Solved"


def lp_var(prob, name: str, lowBound=None, upBound=None,
           cat: str = pulp.LpContinuous):
    """Variable ``name`` im Problem ``prob`` anlegen (PuLP 3 und 4)."""
    anlegen = getattr(prob, "add_variable", None)
    if anlegen is not None:                                  # PuLP >= 4
        return anlegen(name, lowBound, upBound, cat)
    return pulp.LpVariable(name, lowBound=lowBound, upBound=upBound, cat=cat)


def lp_solve(prob, solver) -> str:
    """Loesen und den Status so benennen, wie PuLP 3 es tat.

    PuLP 3 meldete bei HiGHS "Optimal", SOBALD EINE LOESUNG DA WAR - auch nach
    Zeit- oder Iterationslimit, Abbruch oder erreichter Zielschranke; ob das
    Zeitlimit griff, misst der Optimierer selbst. PuLP 4 nennt dieselben Faelle
    TimeLimit, IterationLimit, Interrupted und GapLimit. Ohne diese Uebersetzung
    wuerde jeder zeitbegrenzte Lauf als gescheitert verworfen.
    """
    ergebnis = prob.solve(solver)
    if not hasattr(ergebnis, "has_solution"):                # PuLP 3
        return pulp.LpStatus[prob.status]
    zustand = pulp.LpSolveStatus
    status = ergebnis.status
    if status == zustand.Optimal:
        return OPTIMAL
    if status == zustand.Infeasible:
        return INFEASIBLE
    if status == zustand.Unbounded:
        return UNBOUNDED
    if status == zustand.NotSolved:
        return NOT_SOLVED
    if status == zustand.Undefined:
        # PuLP 4 fasst HiGHS' "unbeschraenkt ODER unzulaessig" mit Lade- und
        # Modellfehlern zusammen. PuLP 3 nannte Ersteres "Infeasible" - und nur
        # darauf springt die Unzulaessigkeits-Diagnose des Optimierers an.
        return INFEASIBLE if _highs_unbeschraenkt_oder_unzulaessig(prob) else NOT_SOLVED
    return OPTIMAL if ergebnis.has_solution else NOT_SOLVED


def _highs_unbeschraenkt_oder_unzulaessig(prob) -> bool:
    modell = getattr(prob, "solverModel", None)
    if modell is None:
        return False
    try:
        import highspy
        return (modell.getModelStatus()
                == highspy.HighsModelStatus.kUnboundedOrInfeasible)
    except Exception:
        return False


def lp_spalten(lp):
    """(HiGHS-Spalte, Variable) fuer einen Warmstart, in beiden Versionen."""
    exportiert = getattr(lp, "exported_variables", None)
    if exportiert is not None:                               # PuLP >= 4
        return list(enumerate(exportiert()))
    return [(var.index, var) for var in lp.variables()]


def eingebauter_cbc():
    """``PULP_CBC_CMD`` oder None - den eingebauten CBC gibt es ab PuLP 4 nur
    noch ueber das Extra ``pulp[cbc]``."""
    return getattr(pulp, "PULP_CBC_CMD", None)
