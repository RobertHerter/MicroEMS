"""Invarianten-Validator für Optimierungspläne.

Prüft einen fertigen Plan (OptimizerResult.table) gegen eine Liste von
Eigenschaften, die IMMER gelten müssen ("das darf nie passieren"), sowie
gegen ökonomische Plausibilität. Findet die Klasse von Modellfehlern, die
sich bisher nur zufällig im Dashboard zeigte:

  * physikalisch/ausführbar: SoC-/Leistungsgrenzen, Energiebilanz, kein
    gleichzeitiges Laden/Entladen bzw. Import/Export, DC-Laden nur aus dem
    PV-Überschuss, kein Entladen bei PV-Überschuss (getarntes Akku->Netz),
    Einspeisebegrenzung.
  * ökonomisch (Warnungen): Plan darf gegenüber der "Ohne-EMS"-Baseline
    (terminalwert-bereinigt) nicht schlechter sein; kein Netzladen bzw. keine
    Entladesperre bei teurem Import ohne Grund.

Reines Prüfmodul ohne Seiteneffekte -> nutzbar im Backtest, live (ems/alert)
und in Tests. Severity "error" = echter Bug, "warning" = verdächtig.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from .config import Config
from .optimizer import OptimizerInputs, OptimizerResult, natural_battery_step

TOL_W = 5.0        # Leistungs-Toleranz (Rundung in der Tabelle: 0.1 W)
TOL_WH = 10.0      # SoC-/Energie-Toleranz
_MAX_SLOTS = 4     # je Regel höchstens so viele Slots einzeln melden


@dataclass
class Violation:
    rule: str
    severity: str                 # "error" | "warning"
    count: int                    # betroffene Slots (bzw. 1 für Gesamtchecks)
    detail: str
    slots: List[pd.Timestamp]

    def __str__(self) -> str:
        tag = "✗" if self.severity == "error" else "⚠"
        when = ""
        if self.slots:
            heads = ", ".join(s.strftime("%d.%m %H:%M") for s in self.slots[:_MAX_SLOTS])
            more = f" (+{self.count - _MAX_SLOTS})" if self.count > _MAX_SLOTS else ""
            when = f"  [{heads}{more}]"
        return f"{tag} {self.rule}: {self.detail}{when}"


def _mask_violation(rule, severity, mask: pd.Series, detail) -> Optional[Violation]:
    idx = list(mask.index[mask.fillna(False)])
    if not idx:
        return None
    return Violation(rule, severity, len(idx), detail, idx)


def validate_plan(config: Config, result: OptimizerResult,
                  inputs: Optional[OptimizerInputs] = None) -> List[Violation]:
    """Prüft einen Plan gegen alle Invarianten. Gibt die Verstöße zurück."""
    t = result.table
    hb = config.house_battery
    dt = config.general.dt_hours
    kwh = dt / 1000.0
    v: List[Violation] = []

    def col(name, default=0.0):
        return t[name] if name in t.columns else pd.Series(default, index=t.index)

    dc, ac, dis = col("batt_dc_charge_w"), col("batt_ac_charge_w"), col("batt_discharge_w")
    imp, exp = col("grid_import_w"), col("grid_export_w")
    curt = col("pv_curtail_w")
    pv = col("pv_w")
    load = col("house_load_w")
    price = col("price_ct_kwh")
    soc = col("house_soc_wh")
    car = col("car_charge_w")

    # ---- Solver-Status -------------------------------------------------- #
    if result.infeasible:
        v.append(Violation("solver.infeasible", "error", 1,
                           f"Optimierung nicht optimal gelöst ({result.status})", []))
    if result.solver_hit_limit:
        v.append(Violation("solver.time_limit", "warning", 1,
                           "Solver-Zeitlimit erreicht – Plan evtl. suboptimal", []))

    # ---- Physikalisch / ausführbar (error) ------------------------------ #
    add = lambda x: v.append(x) if x else None

    add(_mask_violation("soc.bounds", "error",
        (soc < hb.min_soc_wh - TOL_WH) | (soc > hb.max_soc_wh + TOL_WH),
        f"SoC außerhalb [{hb.min_soc_wh:.0f}, {hb.max_soc_wh:.0f}] Wh"))

    add(_mask_violation("power.dc_charge", "error", dc > hb.max_dc_charge_w + TOL_W,
        f"DC-Laden über Grenze {hb.max_dc_charge_w:.0f} W"))
    add(_mask_violation("power.ac_charge", "error", ac > hb.max_ac_charge_w + TOL_W,
        f"AC-Laden über Grenze {hb.max_ac_charge_w:.0f} W"))
    add(_mask_violation("power.total_charge", "error",
        dc + ac > hb.max_total_charge_w + TOL_W,
        f"Gesamt-Laden über Grenze {hb.max_total_charge_w:.0f} W"))
    add(_mask_violation("power.discharge", "error", dis > hb.max_discharge_w + TOL_W,
        f"Entladen über Grenze {hb.max_discharge_w:.0f} W"))

    add(_mask_violation("mutex.charge_discharge", "error",
        (dc + ac > TOL_W) & (dis > TOL_W), "gleichzeitiges Laden UND Entladen"))
    add(_mask_violation("mutex.import_export", "error",
        (imp > TOL_W) & (exp > TOL_W), "gleichzeitiger Netzbezug UND Einspeisung"))

    # DC-Laden nur aus PV, und nur aus dem PV-ÜBERSCHUSS (getarntes Netzladen)
    add(_mask_violation("pv.dc_only_from_pv", "error", dc + curt > pv + TOL_W,
        "DC-Laden + Abregelung übersteigt PV"))
    add(_mask_violation("pv.dc_le_surplus", "error",
        dc > np.maximum(0.0, pv - load) + TOL_W,
        "DC-Laden über PV-Überschuss (getarntes Netzladen)"))

    # Kein Entladen bei PV-Überschuss (außer erlaubtem Akku->Netz)
    if not config.optimization.allow_grid_discharge:
        add(_mask_violation("battery.no_discharge_on_surplus", "error",
            (pv > load + TOL_W) & (dis > TOL_W),
            "Entladen trotz PV-Überschuss (getarntes Akku->Netz)"))
        add(_mask_violation("grid.no_discharge", "error",
            col("batt_grid_discharge_w") > TOL_W,
            "Akku->Netz obwohl allow_grid_discharge=false"))

    if config.inverter.max_export_w is not None:
        add(_mask_violation("grid.export_cap", "error",
            exp > config.inverter.max_export_w + TOL_W,
            f"Einspeisung über Netzanschluss-Limit {config.inverter.max_export_w:.0f} W"))

    # Energiebilanz je Slot (muss per Konstruktion ~0 sein)
    pv_to_ac = pv - dc - curt
    balance = imp - exp - (load + car + ac - pv_to_ac - dis)
    add(_mask_violation("balance.node", "error", balance.abs() > 2 * TOL_W,
        "AC-Knotenbilanz verletzt"))

    # SoC-Dynamik (nur mit Anfangs-SoC prüfbar)
    if inputs is not None:
        prev = np.empty(len(soc))
        prev[0] = min(hb.max_soc_wh, max(hb.min_soc_wh, inputs.initial_house_soc_wh))
        prev[1:] = soc.values[:-1]
        expected = (prev + hb.charge_efficiency * dc.values * dt
                    + hb.eff_ac_charge * ac.values * dt
                    - dis.values * dt / hb.discharge_efficiency)
        add(_mask_violation("soc.dynamics", "error",
            pd.Series(np.abs(soc.values - expected) > 5 * TOL_WH, index=soc.index),
            "SoC-Fortschreibung inkonsistent"))

    # ---- Ökonomisch (warning) ------------------------------------------ #
    # Diese Regeln sind MYOPISCH (Ein-Slot): sie sehen nicht, dass ein späterer
    # Slot noch teurer ist. Am Horizontende verzerrt zudem der Terminalwert
    # (letzte kWh ~so wertvoll wie teurer Import). Daher die letzten 2 h
    # ausschließen - dort ist "halten statt entladen" ein erwarteter Randeffekt.
    # Die RIGOROSE Aussage liefert econ.worse_than_baseline (s.u.).
    med_price = float(price.median()) if len(price) else 0.0
    tail = pd.Series(False, index=t.index)
    n_tail = int(2 / dt)   # letzte 2 h
    if len(tail) > n_tail:
        tail.iloc[-n_tail:] = True

    add(_mask_violation("econ.grid_charge_expensive", "warning",
        (ac > TOL_W) & (price > med_price) & (~tail),
        f"Netzladen über Median-Preis ({med_price:.1f} ct/kWh)"))

    if "discharge_limited" in t.columns:
        # "Keine teurere Stunde mehr voraus": dann gibt es keinen Grund, die
        # Ladung weiter zu halten und stattdessen teuer zu importieren. Das ist
        # die echte Bug-Signatur (z.B. die frühere p10-Abend-Sperre). Das
        # legitime "Sparen für eine spätere, noch teurere Spitze" wird dadurch
        # NICHT gemeldet - das war die Ursache des Winter-Rauschens.
        pa = price.values.astype(float)
        suffix_max = np.maximum.accumulate(pa[::-1])[::-1]
        fut_max = np.concatenate([suffix_max[1:], [-np.inf]])  # max(price[t+1:])
        no_pricier_ahead = pd.Series(pa >= fut_max - 1e-9, index=t.index)
        add(_mask_violation("econ.hold_while_expensive", "warning",
            (col("discharge_limited") > 0.5) & (imp > TOL_W)
            & (price >= float(price.quantile(0.75)))
            & (soc > hb.min_soc_wh + 0.05 * hb.capacity_wh)
            & no_pricier_ahead & (~tail),
            "Entladesperre bei teurem Netzbezug, obwohl keine teurere Stunde folgt"))

    # Plan vs. Ohne-EMS-Baseline (terminalwert-bereinigt): der perfekt
    # informierte MILP darf nie teurer sein als das naive Eigenverbrauchs-
    # verhalten. term_val = Mittelpreis (wie terminal_soc_value "auto").
    if inputs is not None:
        term = float(np.mean(inputs.price_ct_kwh)) * hb.discharge_efficiency
        plan_cost = float(t["slot_cost_ct"].sum()) - term * float(soc.iloc[-1]) / 1000.0
        b_cost, b_soc = 0.0, prev[0]
        for i in range(len(t)):
            b_soc, _c, _d, b_imp, b_exp = natural_battery_step(
                b_soc, inputs.pv_w[i], inputs.house_load_w[i], hb, dt,
                max_export_w=config.inverter.max_export_w)
            b_cost += (b_imp * inputs.price_ct_kwh[i]
                       - b_exp * inputs.feedin_ct_kwh[i]) * kwh
        b_cost -= term * b_soc / 1000.0
        margin = max(50.0, abs(b_cost) * 0.05)   # 50 ct oder 5 %
        if plan_cost > b_cost + margin:
            v.append(Violation("econ.worse_than_baseline", "warning", 1,
                f"Plan {plan_cost/100:.2f} € teurer als Ohne-EMS-Baseline "
                f"{b_cost/100:.2f} € (terminalwert-bereinigt)", []))

    return v


def summarize(violations: List[Violation]) -> str:
    if not violations:
        return "keine Verstöße"
    errs = sum(1 for x in violations if x.severity == "error")
    warns = sum(1 for x in violations if x.severity == "warning")
    return f"{errs} Fehler, {warns} Warnungen"
