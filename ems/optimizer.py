"""MILP-Optimierer für die Steuertabelle (48 h, 15-min-Slots).

Minimiert die Netto-Stromkosten (Import * Preis - Export * Einspeisevergütung)
über den Horizont. Steuergrößen je Slot:

  * dc_charge  – Haus-Akku DC-Ladeleistung (nur aus PV)         [W]
  * ac_charge  – Haus-Akku AC-Ladeleistung (aus dem Netz)       [W]
  * dis        – Haus-Akku Entladeleistung (AC-seitig geliefert)[W]
  * car        – Auto-Ladeleistung (semikontinuierlich)         [W]

Nebenbedingungen: SoC-Grenzen Haus/Auto, Leistungsgrenzen, Wechselrichter-
Durchsatz, kein gleichzeitiges Laden/Entladen (Binärvariablen), Auto-Ziel-SoC
zur Abfahrtzeit.

Modellannahmen (dokumentiert):
  * PV ist am DC-Bus verfügbar; DC-Laden reduziert die zu Wechselrichter/AC
    geführte PV-Leistung: pv_to_ac = pv - dc_charge (>= 0).
  * Wechselrichter-Durchsatz begrenzt: pv_to_ac + dis + ac_charge <= WR_max.
  * Auto lädt AC-seitig und zählt nicht in den WR-Batterieport-Durchsatz.
  * Wirkungsgrade: Laden/Entladen wirken auf die SoC-Bilanz.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd
import pulp

from .config import Config

log = logging.getLogger("ems.optimizer")


@dataclass
class OptimizerInputs:
    index: pd.DatetimeIndex            # tz-aware, N Slots
    house_load_w: np.ndarray
    pv_w: np.ndarray
    price_ct_kwh: np.ndarray
    feedin_ct_kwh: np.ndarray
    initial_house_soc_wh: float
    initial_car_soc_wh: Optional[float] = None
    car_present: bool = False


@dataclass
class OptimizerResult:
    table: pd.DataFrame                # je Slot alle Größen + prognostizierte SoCs
    total_cost_ct: float
    status: str
    infeasible: bool = False


class Optimizer:
    def __init__(self, config: Config):
        self.cfg = config

    def _departure_slot_indices(self, index: pd.DatetimeIndex) -> List[int]:
        """Slot-Indizes, an denen die tägliche Abfahrtzeit liegt (innerhalb Horizont)."""
        if not (self.cfg.vehicle.enabled):
            return []
        dep = self.cfg.vehicle.departure_time
        local = index.tz_convert(self.cfg.general.timezone)
        out = []
        for i, ts in enumerate(local):
            if ts.hour == dep.hour and ts.minute == dep.minute:
                out.append(i)
        return out

    def solve(self, inp: OptimizerInputs) -> OptimizerResult:
        cfg = self.cfg
        hb = cfg.house_battery
        veh = cfg.vehicle
        dt = cfg.general.dt_hours
        N = len(inp.index)

        use_car = bool(veh.enabled and inp.car_present and inp.initial_car_soc_wh is not None)

        prob = pulp.LpProblem("EMS", pulp.LpMinimize)

        # ---- Entscheidungsvariablen ------------------------------------- #
        dc = [pulp.LpVariable(f"dc_{t}", 0, hb.max_dc_charge_w) for t in range(N)]
        ac = [pulp.LpVariable(f"ac_{t}", 0, hb.max_ac_charge_w) for t in range(N)]
        dis = [pulp.LpVariable(f"dis_{t}", 0, hb.max_discharge_w) for t in range(N)]
        g_imp = [pulp.LpVariable(f"gimp_{t}", 0) for t in range(N)]
        g_exp = [pulp.LpVariable(f"gexp_{t}", 0) for t in range(N)]
        is_ch = [pulp.LpVariable(f"isch_{t}", cat="Binary") for t in range(N)]
        is_di = [pulp.LpVariable(f"isdi_{t}", cat="Binary") for t in range(N)]

        # SoC-Zustände (Wh), soc[0] = Anfangswert
        soc = [pulp.LpVariable(f"soc_{t}", hb.min_soc_wh, hb.max_soc_wh) for t in range(N + 1)]

        if use_car:
            car = [pulp.LpVariable(f"car_{t}", 0, veh.max_charge_w) for t in range(N)]
            is_car = [pulp.LpVariable(f"iscar_{t}", cat="Binary") for t in range(N)]
            soc_car = [
                pulp.LpVariable(f"soccar_{t}", veh.min_soc_wh, veh.capacity_wh)
                for t in range(N + 1)
            ]
        else:
            car = [0.0] * N
            soc_car = None

        # ---- Anfangszustände -------------------------------------------- #
        prob += soc[0] == max(hb.min_soc_wh, min(hb.max_soc_wh, inp.initial_house_soc_wh))
        if use_car:
            prob += soc_car[0] == max(
                veh.min_soc_wh, min(veh.capacity_wh, inp.initial_car_soc_wh)
            )

        dep_slots = set(self._departure_slot_indices(inp.index)) if use_car else set()

        M_ch = hb.max_dc_charge_w + hb.max_ac_charge_w

        # Netz-Entladen (Akku -> Netz) nur wirtschaftlich sinnvoll: erlaubt in
        # Slot t, wenn der künftige Importpreis unter die Einspeisung fällt (jetzt
        # zur Einspeisung verkaufen, später günstiger zurückladen) ODER der aktuelle
        # Preis negativ ist. Sonst bleibt Akku->Netz gesperrt (verhindert das
        # unwirtschaftliche "Dumpen").
        price = inp.price_ct_kwh
        feedin = inp.feedin_ct_kwh
        gd_allowed = [False] * N
        if cfg.optimization.allow_grid_discharge:
            for t in range(N):
                future_min = min(price[t + 1:]) if t + 1 < N else float("inf")
                gd_allowed[t] = bool(future_min < feedin[t] or price[t] < 0.0)

        # Binär je Slot: entweder Netzbezug ODER Einspeisung – nie gleichzeitig.
        # Damit schließen sich AC-Laden (Import) und Einspeisen physikalisch aus.
        b_grid = [pulp.LpVariable(f"bgrid_{t}", cat="Binary") for t in range(N)]
        BIGG = 60000.0

        # Eigenverbrauchs-Priorität (harte Regel): Einspeisen nur erlaubt, wenn der
        # Akku VOLL ist (is_full) ODER mit maximaler Leistung lädt (at_max). Dadurch
        # lädt jeder PV-Überschuss zuerst in den Akku – früh und deterministisch,
        # erst der echte Überlauf wird eingespeist. In Arbitrage-Slots (gd_allowed)
        # entfällt die Regel (dann darf der Akku ins Netz entladen).
        is_full = [pulp.LpVariable(f"full_{t}", cat="Binary") for t in range(N)]
        at_max = [pulp.LpVariable(f"atmax_{t}", cat="Binary") for t in range(N)]
        EPS_SOC = 20.0   # Wh
        EPS_P = 20.0     # W

        for t in range(N):
            pv_t = float(max(0.0, inp.pv_w[t]))
            load_t = float(max(0.0, inp.house_load_w[t]))

            # DC-Laden nur aus PV
            prob += dc[t] <= pv_t
            pv_to_ac = pv_t - dc[t]   # LpAffineExpression, >= 0 durch dc<=pv

            # Wechselrichter-Durchsatz
            prob += pv_to_ac + dis[t] + ac[t] <= cfg.inverter.max_ac_power_w

            # AC-Knotenbilanz: Import - Export = Last + Auto + AC-Laden - PV_AC - Entladung
            prob += (
                g_imp[t] - g_exp[t]
                == load_t + car[t] + ac[t] - pv_to_ac - dis[t]
            )

            # kein gleichzeitiges Laden/Entladen
            prob += dc[t] + ac[t] <= M_ch * is_ch[t]
            prob += dis[t] <= hb.max_discharge_w * is_di[t]
            prob += is_ch[t] + is_di[t] <= 1

            # physikalische Gesamt-Ladeleistung des Akkus (DC + AC zusammen)
            prob += dc[t] + ac[t] <= hb.max_total_charge_w

            # Eigenverbrauchs-Priorität / kein Akku->Netz (außer Arbitrage):
            # Einspeisen nur, wenn Akku voll ODER mit Maximalleistung lädt.
            prob += soc[t + 1] >= (hb.max_soc_wh - EPS_SOC) * is_full[t]
            prob += dc[t] + ac[t] >= (hb.max_total_charge_w - EPS_P) * at_max[t]
            if not gd_allowed[t]:
                prob += g_exp[t] <= BIGG * (is_full[t] + at_max[t])
            # Kein gleichzeitiges Netzladen (Import) und Einspeisen (Export)
            prob += g_imp[t] <= BIGG * b_grid[t]
            prob += g_exp[t] <= BIGG * (1 - b_grid[t])

            # SoC-Dynamik Haus (Wirkungsgrade)
            prob += soc[t + 1] == (
                soc[t]
                + hb.charge_efficiency * (dc[t] + ac[t]) * dt
                - (1.0 / hb.discharge_efficiency) * dis[t] * dt
            )

            if use_car:
                # semikontinuierlich: 0 oder [min,max]
                prob += car[t] <= veh.max_charge_w * is_car[t]
                prob += car[t] >= veh.min_charge_w * is_car[t]
                prob += soc_car[t + 1] == soc_car[t] + veh.charge_efficiency * car[t] * dt
                # zur Abfahrtzeit Ziel-SoC erreichen
                if t in dep_slots:
                    prob += soc_car[t] >= veh.target_soc_wh

        # Ziel-SoC am letzten Slot ebenfalls sichern, falls keine Abfahrt im Horizont
        if use_car and not dep_slots:
            prob += soc_car[N] >= veh.target_soc_wh

        # ---- Zielfunktion ----------------------------------------------- #
        kwh = dt / 1000.0
        cost_terms = []
        for t in range(N):
            cost_terms.append(g_imp[t] * inp.price_ct_kwh[t] * kwh)
            cost_terms.append(-g_exp[t] * inp.feedin_ct_kwh[t] * kwh)

        # Zyklus-Malus
        pen = cfg.optimization.cycle_penalty_ct_kwh
        if pen:
            for t in range(N):
                cost_terms.append(pen * (dc[t] + ac[t] + dis[t]) * kwh)

        # Kleiner Tie-Breaker: DC-Laden (PV) gegenüber AC-Laden (Netz) bevorzugen,
        # wenn kostengleich. So wird AC-Laden nur genutzt, wenn es echten Vorteil
        # bringt (günstiger Netzbezug), nicht zum Wegrouten von PV-Überschuss.
        for t in range(N):
            cost_terms.append(0.02 * ac[t] * kwh)

        # Hinweis: Die Eigenverbrauchs-Priorität (Akku aus PV zuerst voll laden)
        # wird oben als HARTE Nebenbedingung erzwungen (Einspeisen nur bei vollem
        # Akku / Maximalladung), nicht mehr über einen Einspeise-Malus. Das
        # vermeidet Degeneration (spätes Laden) und Energievernichtung.

        # Terminalwert des gespeicherten Akku-Inhalts (Nutzen -> negativ)
        tv = cfg.optimization.terminal_soc_value
        if tv == "auto":
            term_val = float(np.mean(inp.price_ct_kwh))
        else:
            term_val = float(tv)
        cost_terms.append(-term_val * soc[N] / 1000.0)

        prob += pulp.lpSum(cost_terms)

        # ---- Lösen ------------------------------------------------------- #
        solver = pulp.PULP_CBC_CMD(
            timeLimit=cfg.optimization.solver_time_limit_s, msg=0
        )
        prob.solve(solver)
        status = pulp.LpStatus[prob.status]
        infeasible = prob.status != pulp.LpStatusOptimal
        if infeasible:
            log.error("Optimierung nicht optimal gelöst: %s", status)

        # ---- Ergebnis extrahieren --------------------------------------- #
        def val(v):
            return float(pulp.value(v)) if not isinstance(v, (int, float)) else float(v)

        rows = []
        for t in range(N):
            dc_v, ac_v, dis_v = val(dc[t]), val(ac[t]), val(dis[t])
            car_v = val(car[t]) if use_car else 0.0
            imp_v, exp_v = val(g_imp[t]), val(g_exp[t])
            soc_v = val(soc[t + 1])
            slot_cost = (imp_v * inp.price_ct_kwh[t] - exp_v * inp.feedin_ct_kwh[t]) * kwh

            # --- Übersetzung in E3DC-Steuerbefehle -----------------------
            # Der E3DC regelt selbst (Eigenverbrauch): lädt aus PV-Überschuss,
            # entlädt zur Lastdeckung. Es wird nur ein LIMIT gesetzt, wenn der
            # Optimierer vom natürlichen Verhalten abweichen will:
            #   * Lade-/Entladelimit = Hardware-Maximum  -> "frei laufen" (kein Eingriff)
            #   * Lade-/Entladelimit < Maximum           -> gezielte Begrenzung/Sperre
            #   * grid_charge_w > 0                      -> Netzladen erzwingen
            pv_t = float(max(0.0, inp.pv_w[t]))
            load_t = float(max(0.0, inp.house_load_w[t]))
            nat_charge = min(max(0.0, pv_t - load_t), hb.max_dc_charge_w)
            nat_dis = min(max(0.0, load_t - pv_t), hb.max_discharge_w)
            tol = 5.0
            full_tol = 0.02 * hb.capacity_wh   # 2 % Toleranz für "praktisch voll/leer"
            battery_full = soc_v >= hb.max_soc_wh - full_tol
            battery_empty = soc_v <= hb.min_soc_wh + full_tol
            # PV-Laden nur dann als "begrenzt" markieren, wenn der Akku noch Platz
            # hätte (sonst lädt er ohnehin nicht weiter -> kein Eingriff, "auto").
            if dc_v < nat_charge - tol and not battery_full:
                charge_limit, charge_limited = round(dc_v, 1), 1
            else:
                charge_limit, charge_limited = hb.max_dc_charge_w, 0
            # Entladen nur dann als "gesperrt/gedrosselt" markieren, wenn noch
            # Ladung vorhanden ist (sonst kann er ohnehin nicht entladen).
            if dis_v < nat_dis - tol and not battery_empty:
                dis_limit, dis_limited = round(dis_v, 1), 1
            else:
                dis_limit, dis_limited = hb.max_discharge_w, 0
            if ac_v > tol:
                mode = "grid_charge"
            elif charge_limited and charge_limit < tol:
                mode = "block_charge"
            elif dis_limited and dis_limit < tol:
                mode = "hold"                         # Entladen gesperrt (Akku halten)
            elif charge_limited or dis_limited:
                mode = "limit"
            else:
                mode = "auto"
            # Netz-Entladen (Akku -> Netz): der Teil der Einspeisung, der nicht aus
            # PV stammt. Bei allow_grid_discharge=False ist das 0.
            grid_discharge_v = max(0.0, exp_v - max(0.0, pv_t - dc_v))
            if grid_discharge_v > tol:
                mode = "grid_discharge"

            row = {
                "house_load_w": float(inp.house_load_w[t]),
                "pv_w": float(inp.pv_w[t]),
                "price_ct_kwh": float(inp.price_ct_kwh[t]),
                "feedin_ct_kwh": float(inp.feedin_ct_kwh[t]),
                "batt_dc_charge_w": round(dc_v, 1),
                "batt_ac_charge_w": round(ac_v, 1),
                "batt_discharge_w": round(dis_v, 1),
                # E3DC-Steuerbefehle (Limits nur bei Abweichung vom Eigenverbrauch):
                "batt_charge_limit_w": charge_limit,
                "batt_discharge_limit_w": dis_limit,
                "batt_grid_charge_w": round(ac_v, 1),
                "batt_grid_discharge_w": round(grid_discharge_v, 1),
                "charge_limited": float(charge_limited),
                "discharge_limited": float(dis_limited),
                "mode": mode,
                "car_charge_w": round(car_v, 1),
                "grid_import_w": round(imp_v, 1),
                "grid_export_w": round(exp_v, 1),
                "house_soc_wh": round(soc_v, 1),
                "house_soc_percent": round(100.0 * soc_v / hb.capacity_wh, 2),
                "slot_cost_ct": round(slot_cost, 4),
            }
            if use_car:
                soc_car_v = val(soc_car[t + 1])
                row["car_soc_wh"] = round(soc_car_v, 1)
                row["car_soc_percent"] = round(100.0 * soc_car_v / veh.capacity_wh, 2)
            rows.append(row)

        table = pd.DataFrame(rows, index=inp.index)
        total = float(table["slot_cost_ct"].sum())
        return OptimizerResult(
            table=table, total_cost_ct=total, status=status, infeasible=infeasible
        )
