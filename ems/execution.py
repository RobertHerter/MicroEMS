"""Leistungsabweichung vom ausfuehrbaren Akku-Befehlsrahmen.

Eine Prognose ist kein Leistungssollwert: freie Eigenverbrauchsregelung folgt
der echten Last/PV. Wie im Zaehler-Audit werden Limits und die unter den
gemessenen Bedingungen erreichbare geplante Leistung bewertet. Die
Knotenbilanz allein waere kein Test: auch ein falsch steuernder Akku erfuellt sie.
"""
import math


def battery_execution_deviation(planned, actual, min_soc_percent):
    """Signierter Abstand vom Befehlsrahmen in W (Ist minus Grenze).

    Im Rahmen: 0. Ohne belastbare Messungen oder archivierte Limits: None.
    SoC-Ausnahmen entsprechen dem Slot-Audit (voll ab 97 %, leer bis min+3).
    """
    try:
        batt, pv, load, soc = (float(actual[k])
                               for k in ("battery_w", "pv_w", "load_w", "soc"))
        target, charge, discharge = (float(planned[k]) for k in
                                     ("battery_w", "charge_limit_w", "discharge_limit_w"))
        grid_charge = float(planned.get("grid_charge_w") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    if (not all(math.isfinite(v) for v in
                (batt, pv, load, soc, target, charge, discharge, grid_charge))
            or load <= 1.0 or min(charge, discharge) < 0.0):
        return None
    # Gezielte Akku->Netz-Entladung folgt einer anderen Regel; alte Archive
    # ohne diesen Befehl nicht als freie Eigenverbrauchsregelung auslegen.
    dashboard = planned.get("dashboard") or {}
    if (float(planned.get("grid_discharge_w") or 0.0) > 1.0
            or float(dashboard.get("batt_grid_discharge_w") or 0.0) > 1.0):
        return None
    surplus = pv - load
    lower, upper = -discharge, charge
    if grid_charge <= 1.0:
        upper = min(upper, max(0.0, surplus))
    if target > 50.0 and soc < 97.0:
        lower = max(lower, min(target, charge, max(0.0, surplus) + grid_charge))
    elif target < -50.0 and soc > min_soc_percent + 3.0:
        upper = min(upper, -min(-target, discharge, max(0.0, -surplus)))
    return batt - min(upper, max(lower, batt))
