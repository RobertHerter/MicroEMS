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
import os
import re
import time
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
    # Pessimistische PV-Vorhersage (Solcast p10, W). Wenn vorhanden, wird die
    # Einspeise-Linie an Peak-Tagen dagegen dimensioniert: der Akku wird auch
    # dann voll, wenn der Tag bewölkter ausfällt als der Erwartungswert.
    pv10_w: Optional[np.ndarray] = None
    # Außentemperatur (°C) je Slot – Wärmeverlust thermischer Lasten (Pool).
    ambient_temp_c: Optional[np.ndarray] = None
    # Solar-Einstrahlung (W/m² Globalstrahlung) je Slot – solarer Wärmeeintrag
    # thermischer Lasten (Pool, ems.loads._add_thermal).
    solar_w_m2: Optional[np.ndarray] = None
    # Ist-Zustand steuerbarer Lasten beim Start: {load_name: aktuelle Temperatur °C}
    # für thermische Lasten (T[0]). Fehlt ein Wert -> target_c als Startwert.
    load_state: Optional[dict] = None


@dataclass
class OptimizerResult:
    table: pd.DataFrame                # je Slot alle Größen + prognostizierte SoCs
    total_cost_ct: float
    status: str
    infeasible: bool = False
    export_line_w: Optional[float] = None   # Einspeise-Linie L (nur Peak-Modus)
    # Fehlmenge (Wh) zum Auto-Ziel-SoC bei Abfahrt (weiche Nebenbedingung);
    # > 0 = Ziel im Plan nicht erreichbar -> Alarm.
    car_target_shortfall_wh: float = 0.0
    # Solver hat das Zeitlimit erreicht: CBC liefert dann den besten
    # Zwischenstand und PuLP meldet trotzdem "Optimal" - der Plan kann
    # deutlich suboptimal sein (z.B. sinnlose Dumps/Sperren) -> Alarm.
    solver_hit_limit: bool = False
    # Steuerbare Lasten: [(mqtt_topic, Ergebnis-Spaltenname)] für die Sollwert-Ausgabe.
    load_mqtt_map: Optional[list] = None


# --------------------------------------------------------------------------- #
# MIP-Warmstart: Lösung des letzten Laufs (prozess-lokal). Zwischen zwei
# 15-min-Zyklen ändert sich der Plan kaum - die um den Zeitversatz verschobene
# alte Lösung ist ein sehr guter Startpunkt: HiGHS bekommt sofort einen
# brauchbaren Incumbent (statt ihn per Heuristik suchen zu müssen) und
# repariert unpassende kontinuierliche Werte selbst per LP. Passt die alte
# Lösung gar nicht mehr (Eingaben stark geändert), verwirft HiGHS sie einfach.
# --------------------------------------------------------------------------- #
_warm_cache: dict = {}

_TRAIL_IDX = re.compile(r"^(.+)_(\d+)$")


def _shifted_warm_values(new_start, slot_minutes: int) -> Optional[dict]:
    """Warm-Werte des letzten Laufs auf den neuen Horizont verschieben:
    alter Slot-Index -> neuer = alt - Versatz. Nicht-Slot-Variablen (z.B.
    L_day_<i> = Tagesindex der Einspeise-Linie) werden ausgelassen - ein
    falsch verschobener Tagesindex wäre schädlicher als kein Startwert.
    None, wenn kein (nutzbarer) letzter Lauf vorliegt."""
    c = _warm_cache
    if not c or c.get("slot_min") != slot_minutes:
        return None
    delta_min = (pd.Timestamp(new_start) - c["start"]).total_seconds() / 60.0
    shift = int(round(delta_min / slot_minutes))
    if abs(delta_min / slot_minutes - shift) > 1e-6 or shift < 0:
        return None
    if shift * slot_minutes > 240:      # > 4 h alt: Plan zu weit weg, wertlos
        return None
    out = {}
    for name, val in c["values"].items():
        m = _TRAIL_IDX.match(name)
        if not m or m.group(1).startswith("L_day"):
            continue
        i = int(m.group(2)) - shift
        if i >= 0:
            out[f"{m.group(1)}_{i}"] = val
    return out or None


def _store_warm_solution(prob, start, slot_minutes: int) -> None:
    """Lösung fürs Warmstarten des nächsten Laufs merken (nur Werte != 0;
    fehlende Variablen starten ohnehin bei 0)."""
    vals = {}
    for v in prob.variables():
        x = v.varValue
        if x is not None and abs(x) > 1e-9:
            vals[v.name] = float(x)
    _warm_cache.clear()
    _warm_cache.update({"start": pd.Timestamp(start), "slot_min": slot_minutes,
                        "values": vals})


class _WarmHiGHS(pulp.HiGHS):
    """pulp.HiGHS + MIP-Startlösung via highspy.setSolution (der PuLP-Wrapper
    selbst kennt keinen Warmstart). Ein unvollständiger/unpassender Start ist
    unkritisch: HiGHS prüft ihn, repariert Kontinuierliches per LP oder
    verwirft ihn kommentarlos."""

    def __init__(self, warm_values=None, **kwargs):
        super().__init__(**kwargs)
        self.warm_values = warm_values or {}

    def callSolver(self, lp):
        if self.warm_values:
            try:
                import highspy
                col = [0.0] * lp.solverModel.getNumCol()
                hits = 0
                for var in lp.variables():
                    x = self.warm_values.get(var.name)
                    if x is not None:
                        col[var.index] = float(x)
                        hits += 1
                if hits:
                    sol = highspy.HighsSolution()
                    sol.col_value = col
                    lp.solverModel.setSolution(sol)
                    log.info("Warmstart: %d Variablen aus dem letzten Lauf vorbelegt.",
                             hits)
            except Exception as exc:   # pragma: no cover - nur defensive Hülle
                log.debug("Warmstart übersprungen (%s).", exc)
        super().callSolver(lp)


def _polish_continuous(prob, cfg, free_names=None) -> bool:
    """Politur: Binärentscheidungen der Lösung fixieren und den Rest exakt
    (gap-frei) nachoptimieren.

    Die MIP-Gap-Toleranz (gapAbs/gapRel) beendet die Suche früh - der
    Incumbent kann dann ökonomisch dominierte Reste enthalten. Zwei Klassen:

    * KONTINUIERLICH (real beobachtet: 5 kW Netzladen bei 16,5 ct, dessen
      Energie nur den ohnehin bald vollen Akku früher füllt und dafür
      späteren PV-Export zu 7,77 ct verdrängt - ~10 ct Verlust): mit
      fixierten Binären ist das Problem ein LP -> exaktes Optimum in
      Sekunden, solche Reste verschwinden beweisbar.
    * BINÄRE Einzel-Blasen (real beobachtet: Entladesperre GENAU in der
      teuersten Reststunde, Import 888 W bei 42,8 ct, Nachbar-Slots entladen
      normal - ~3 ct Schaden): dafür lässt der Aufrufer die verdächtigen
      Binärvariablen über `free_names` FREI - es bleibt ein Mini-MIP mit
      einer Handvoll freier Binärer, ebenfalls Sekunden.

    Schlägt die Politur fehl (numerische Randfälle), wird die ursprüngliche
    Lösung wiederhergestellt. Rückgabe: True, wenn poliert wurde."""
    free_names = free_names or set()
    ints = [v for v in prob.variables() if v.cat == pulp.LpInteger]
    if not ints:
        return False
    snapshot = {v: v.varValue for v in prob.variables()}
    bounds = [(v, v.lowBound, v.upBound) for v in ints]
    try:
        for v in ints:
            if v.name in free_names:
                continue
            x = int(round(v.varValue or 0.0))
            v.lowBound = v.upBound = x
        # Keine relative oder absolute MIP-Lücke in der Politur: genau sie
        # soll die Artefakte der schnellen Hauptoptimierung entfernen.
        prob.solve(make_solver(cfg, exact=True))
        if prob.status == pulp.LpStatusOptimal:
            return True
        log.debug("Politur nicht optimal (%s) - ursprüngliche Lösung bleibt.",
                  pulp.LpStatus[prob.status])
    except Exception as exc:   # pragma: no cover - reine Absicherung
        log.debug("Politur übersprungen (%s).", exc)
    finally:
        for v, lo, hi in bounds:
            v.lowBound, v.upBound = lo, hi
    for v, x in snapshot.items():
        v.varValue = x
    prob.status = pulp.LpStatusOptimal
    return False


def natural_battery_step(soc_wh: float, pv_w: float, load_w: float, hb, dt_hours: float,
                         max_export_w: Optional[float] = None):
    """Ein Slot natürliches E3DC-Eigenverbrauchsverhalten (ohne EMS-Eingriffe):
    PV-Überschuss lädt den Akku, Defizit entlädt ihn; Rest geht ins/kommt vom Netz.

    Wird für den Infeasibility-Fallback UND als "Ohne-EMS"-Baseline des
    Ersparnis-Trackings genutzt. max_export_w: Einspeisebegrenzung am
    Netzanschluss (Überschuss darüber wird abgeregelt).
    Rückgabe: (neuer SoC Wh, Laden W, Entladen W, Netzbezug W, Einspeisung W).
    """
    pv_w, load_w = max(0.0, float(pv_w)), max(0.0, float(load_w))
    surplus = pv_w - load_w
    charge = dis = 0.0
    if surplus >= 0.0:
        room_w = (hb.max_soc_wh - soc_wh) / (hb.charge_efficiency * dt_hours)
        charge = min(surplus, hb.max_dc_charge_w, max(0.0, room_w))
        soc_wh += hb.charge_efficiency * charge * dt_hours
    else:
        avail_w = (soc_wh - hb.min_soc_wh) * hb.discharge_efficiency / dt_hours
        dis = min(-surplus, hb.max_discharge_w, max(0.0, avail_w))
        soc_wh -= dis * dt_hours / hb.discharge_efficiency
    imp = max(0.0, load_w - pv_w - dis)
    exp = max(0.0, pv_w - load_w - charge)
    if max_export_w is not None:
        exp = min(exp, float(max_export_w))   # Rest wird abgeregelt
    return soc_wh, charge, dis, imp, exp


def make_solver(cfg: Config, warm_values: Optional[dict] = None,
                exact: bool = False):
    """CBC-Solver: bevorzugt das System-CBC (COIN_CMD, coinor-cbc), da
    PULP_CBC_CMD ab PuLP 4.0 entfällt; sonst Fallback auf den PuLP-CBC.
    Optional: HiGHS-Solver falls in der Konfiguration gewählt; warm_values
    ({Variablenname: Wert}, z.B. verschobene Lösung des letzten Laufs) werden
    dort als MIP-Startlösung gesetzt (CBC: ignoriert)."""
    threads = cfg.optimization.solver_threads or max(1, (os.cpu_count() or 2) - 1)
    kwargs = dict(timeLimit=cfg.optimization.solver_time_limit_s, msg=0,
                  threads=threads)
    # Relative Optimalitätslücke: kappt den teuren "Optimalität beweisen"-Endlauf
    # (v. a. an peak-Tagen mit Pool-Binärvariablen). gapRel wird von HiGHS und
    # COIN_CMD unterstützt; 0 = exakt.
    # Die Politur muss die Lösung mit fixierten Binären tatsächlich exakt
    # nachrechnen. Die regulären MIP-Gaps dort weiterzuverwenden, ließ
    # ökonomisch dominierte Einzelentscheidungen stehen (z.B. eine isolierte
    # Entladesperre vor einem günstigeren Entlade-Slot).
    gap = 0.0 if exact else float(
        getattr(cfg.optimization, "solver_mip_gap", 0.0) or 0.0)
    # Nicht nur weglassen: HiGHS hat eigene kleine Standard-Gaps. Für die
    # Politur müssen beide explizit 0 sein, sonst können Cent-Bruchteile
    # (z.B. eine 15-min-Entladesperre vor einem billigeren Slot) bleiben.
    if exact or gap > 0:
        kwargs["gapRel"] = gap
    # Absolute Lücke (ct) zusätzlich: schützt vor dem "1 % von einem großen
    # Ziel sind viele Euro"-Effekt (konstante Malusterme) und beendet auf
    # Instanzen mit kleinem Zielwert die teure Beweisphase früher.
    gap_abs = 0.0 if exact else float(
        getattr(cfg.optimization, "solver_mip_gap_abs_ct", 0.0) or 0.0)
    if exact or gap_abs > 0:
        kwargs["gapAbs"] = gap_abs

    solver_name = getattr(cfg.optimization, "solver", "cbc").lower()
    if solver_name == "highs":
        # Fester Zufallsseed + threads=1: HiGHS' Branch-and-Bound-Suche
        # (parallele Baumsuche, Zweigwahl, Heuristik-Timing) ist sonst nicht
        # deterministisch - bei mehreren innerhalb der Gap-Toleranz gleich-
        # wertigen Lösungen kann derselbe Input je Lauf eine ANDERE davon
        # liefern (beobachtet: identischer Plan lieferte einmal ein sinn-
        # loses Netzladen, im Neulauf nicht). threads>1 macht die parallele
        # Baumsuche zusätzlich lauf-timing-abhängig (welcher Zweig zuerst
        # einen Incumbent findet, ist nicht allein vom Seed bestimmt) - nur
        # threads=1 + Seed ergibt "derselbe Input -> IMMER dieselbe Lösung".
        # Kostet auf dieser Instanz kaum Zeit (Solve bleibt << Zeitlimit).
        highs_kwargs = dict(kwargs, random_seed=42, threads=1)
        try:
            highs = _WarmHiGHS(warm_values=warm_values, **highs_kwargs)
            if highs.available():
                log.info("Solver: HiGHS (highspy).")
                return highs
            log.warning("HiGHS-Solver ist nicht verfügbar (ist 'highspy' installiert?). Fallback auf CBC.")
        except Exception as exc:
            log.warning("Fehler beim Laden des HiGHS-Solvers: %s. Fallback auf CBC.", exc)

    coin = pulp.COIN_CMD(**kwargs)
    if coin.available():
        return coin
    return pulp.PULP_CBC_CMD(**kwargs)


class Optimizer:
    def __init__(self, config: Config):
        self.cfg = config

    def _departure_slot_indices(self, index: pd.DatetimeIndex) -> List[int]:
        """Slot-Indizes der Abfahrtzeiten im Horizont (je Wochentag; Tage ohne
        Abfahrt - z.B. Wochenende - liefern keinen Slot)."""
        if not (self.cfg.vehicle.enabled):
            return []
        veh = self.cfg.vehicle
        local = index.tz_convert(self.cfg.general.timezone)
        out = []
        for i, ts in enumerate(local):
            dep = veh.departure_for_weekday(ts.weekday())
            if dep is not None and ts.hour == dep.hour and ts.minute == dep.minute:
                out.append(i)
        return out

    def _neutral_result(self, inp: OptimizerInputs, status: str) -> OptimizerResult:
        """Fallback ohne Eingriffe ("alles auto"), wenn keine Lösung vorliegt.

        Der E3DC regelt dann eigenständig im Eigenverbrauchsmodus. Für plausible
        Prognosewerte (SoC, Netz, Kosten) wird dieses natürliche Verhalten
        simuliert: PV-Überschuss lädt den Akku, Defizit entlädt ihn.
        """
        hb = self.cfg.house_battery
        dt = self.cfg.general.dt_hours
        kwh = dt / 1000.0
        soc = min(hb.max_soc_wh, max(hb.min_soc_wh, float(inp.initial_house_soc_wh)))
        rows = []
        for t in range(len(inp.index)):
            pv_t = float(np.nan_to_num(inp.pv_w[t]))
            load_t = float(np.nan_to_num(inp.house_load_w[t]))
            price_t = float(np.nan_to_num(inp.price_ct_kwh[t]))
            feedin_t = float(np.nan_to_num(inp.feedin_ct_kwh[t]))
            pv_t, load_t = max(0.0, pv_t), max(0.0, load_t)
            soc, charge, dis, imp, exp = natural_battery_step(
                soc, pv_t, load_t, hb, dt,
                max_export_w=self.cfg.inverter.max_export_w)
            row = {
                "house_load_w": load_t, "pv_w": pv_t,
                "price_ct_kwh": price_t, "feedin_ct_kwh": feedin_t,
                "batt_dc_charge_w": round(charge, 1), "batt_ac_charge_w": 0.0,
                "batt_discharge_w": round(dis, 1),
                "batt_charge_limit_w": hb.max_dc_charge_w,
                "batt_discharge_limit_w": hb.max_discharge_w,
                "batt_grid_charge_w": 0.0, "batt_grid_discharge_w": 0.0,
                "charge_limited": 0.0, "discharge_limited": 0.0,
                "mode": "auto", "car_charge_w": 0.0,
                "grid_import_w": round(imp, 1), "grid_export_w": round(exp, 1),
                "house_soc_wh": round(soc, 1),
                "house_soc_percent": round(100.0 * soc / hb.capacity_wh, 2),
                "export_line_w": np.nan,
                "slot_cost_ct": round((imp * price_t - exp * feedin_t) * kwh, 4),
            }
            if inp.car_present and inp.initial_car_soc_wh is not None:
                row["car_soc_wh"] = round(float(inp.initial_car_soc_wh), 1)
                row["car_soc_percent"] = round(
                    100.0 * float(inp.initial_car_soc_wh) / self.cfg.vehicle.capacity_wh, 2)
            rows.append(row)
        table = pd.DataFrame(rows, index=inp.index)
        return OptimizerResult(
            table=table, total_cost_ct=float(table["slot_cost_ct"].sum()),
            status=status, infeasible=True, export_line_w=None,
        )

    def solve(self, inp: OptimizerInputs) -> OptimizerResult:
        cfg = self.cfg
        hb = cfg.house_battery
        veh = cfg.vehicle
        dt = cfg.general.dt_hours
        N = len(inp.index)

        # Eingaben validieren: NaN/Inf (z.B. komplett fehlende PV-Vorhersage)
        # würden das LP unbemerkt unbrauchbar machen.
        bad = [name for name, arr in (
            ("house_load_w", inp.house_load_w), ("pv_w", inp.pv_w),
            ("price_ct_kwh", inp.price_ct_kwh), ("feedin_ct_kwh", inp.feedin_ct_kwh),
        ) if not np.all(np.isfinite(np.asarray(arr, dtype=float)))]
        if bad or not np.isfinite(inp.initial_house_soc_wh):
            log.error("Ungültige Optimierer-Eingaben (NaN/Inf) in %s – "
                      "Fallback 'auto' ohne Eingriffe.", bad or ["initial_house_soc_wh"])
            return self._neutral_result(inp, "InvalidInput")

        # p10 (optional): nur nutzen, wenn vollständig; sonst Erwartungswert.
        pv10 = None
        if inp.pv10_w is not None:
            a = np.asarray(inp.pv10_w, dtype=float)
            if np.all(np.isfinite(a)):
                pv10 = np.maximum(a, 0.0)
            else:
                log.warning("PV-p10 enthält NaN – Einspeise-Linie nutzt den "
                            "Erwartungswert.")

        use_car = bool(veh.enabled and inp.car_present and inp.initial_car_soc_wh is not None)

        prob = pulp.LpProblem("EMS", pulp.LpMinimize)

        # ---- Leistungs-Reserve (#2): Nennleistungen fürs Planen deraten, damit
        # dem Echtzeit-Regler Reserve für Sub-Slot-Lastspitzen bleibt (Default
        # hedge=1.0 -> unverändert). Betrifft Akku-Lade/-Entlade + WR-Durchsatz.
        hedge = max(0.0, min(1.0, 1.0 - cfg.optimization.power_headroom_percent / 100.0))
        max_dc = hb.max_dc_charge_w * hedge
        max_ac = hb.max_ac_charge_w * hedge
        max_dis = hb.max_discharge_w * hedge
        max_tot_ch = hb.max_total_charge_w * hedge
        max_inv = cfg.inverter.max_ac_power_w * hedge
        # Mindest-Entladung (#1): 0 oder >= min_dis (auf die Max-Entladung gekappt).
        min_dis = min(cfg.optimization.min_discharge_w, max_dis)
        standby_w = cfg.optimization.standby_discharge_w   # WR-Sockellast (#1)

        # ---- Entscheidungsvariablen ------------------------------------- #
        dc = [pulp.LpVariable(f"dc_{t}", 0, max_dc) for t in range(N)]
        ac = [pulp.LpVariable(f"ac_{t}", 0, max_ac) for t in range(N)]
        dis = [pulp.LpVariable(f"dis_{t}", 0, max_dis) for t in range(N)]
        # PV-Abregelung: nötig, wenn Akku voll UND Export begrenzt/wertlos ist
        # (max_export_w, Negativpreis ohne Vergütung). Sonst via Mini-Malus 0.
        curt = [pulp.LpVariable(f"curt_{t}", 0) for t in range(N)]
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
        car_short: List = []   # Slack je Abfahrt: Fehlmenge zum Ziel-SoC (Wh)

        # ---- Anfangszustände -------------------------------------------- #
        prob += soc[0] == max(hb.min_soc_wh, min(hb.max_soc_wh, inp.initial_house_soc_wh))
        if use_car:
            prob += soc_car[0] == max(
                veh.min_soc_wh, min(veh.capacity_wh, inp.initial_car_soc_wh)
            )

        dep_slots = set(self._departure_slot_indices(inp.index)) if use_car else set()

        M_ch = max_dc + max_ac

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
        # Big-M aus den Anlagenwerten ableiten statt fester 60 kW: knappe obere
        # Schranken für Import (Last + Auto + AC-Laden) und Export (PV + Ent-
        # laden). Engere Big-Ms beschleunigen CBC und skalieren mit der Anlage.
        load_peak = float(np.max(np.maximum(inp.house_load_w, 0.0))) if N else 0.0
        pv_peak = float(np.max(np.maximum(inp.pv_w, 0.0))) if N else 0.0
        car_max = veh.max_charge_w if use_car else 0.0
        # max. gleichzeitige Leistung der steuerbaren Lasten (fürs Import-Big-M)
        cl_peak = 0.0
        for _ld in getattr(cfg, "controllable_loads", []):
            if not _ld.enabled:
                continue
            if _ld.type == "thermal":
                cl_peak += sum(st.power_w for st in _ld.stages)
            elif _ld.power_profile_w:
                cl_peak += max(_ld.power_profile_w)
            else:
                cl_peak += _ld.power_w
        BIGG = 1.05 * max(load_peak + car_max + hb.max_ac_charge_w + cl_peak,
                          pv_peak + hb.max_discharge_w, 1000.0)

        # Eigenverbrauchs-Priorität (harte Regel): Einspeisen nur erlaubt, wenn der
        # Akku VOLL ist (is_full) ODER mit maximaler Leistung lädt (at_max). Dadurch
        # lädt jeder PV-Überschuss zuerst in den Akku – früh und deterministisch,
        # erst der echte Überlauf wird eingespeist. In Arbitrage-Slots (gd_allowed)
        # entfällt die Regel (dann darf der Akku ins Netz entladen).
        is_full = [pulp.LpVariable(f"full_{t}", cat="Binary") for t in range(N)]
        at_max = [pulp.LpVariable(f"atmax_{t}", cat="Binary") for t in range(N)]
        EPS_SOC = 20.0   # Wh
        EPS_P = 20.0     # W
        strategy = getattr(cfg.optimization, "charge_strategy", "asap")
        pv_max = float(max(1.0, np.max(inp.pv_w)))
        # Peak-Modus: Einspeise-Linie PRO TAG. Einspeisung wird je Kalendertag auf
        # L_tag gedeckelt (alles darunter eingespeist), die PV-Spitze DARÜBER lädt
        # den Akku. Jedes L_tag wird minimiert -> so tief wie möglich, sodass der
        # Akku an JEDEM Tag seine Spitze abschöpft und voll wird.
        _local = inp.index.tz_convert(cfg.general.timezone)
        _daykey = [ts.date() for ts in _local]
        _uniq = sorted(set(_daykey))
        _dayidx = {d: i for i, d in enumerate(_uniq)}
        slot_day = [_dayidx[k] for k in _daykey]
        export_line = [pulp.LpVariable(f"L_day_{i}", 0) for i in range(len(_uniq))]
        # Linie nur auf Tage anwenden, deren Nachmittags-/Erzeugungsspitze im
        # Horizont liegt. Reine Vormittags-Teiltage am Rand (letzter Tag) bekommen
        # KEINE Linie -> keine sinnlose 0-Linie/Zwangsladung am Horizontende.
        _day_last_hour = {}
        for _k, _ts in zip(_daykey, _local):
            _day_last_hour[_k] = max(_day_last_hour.get(_k, -1), _ts.hour)
        line_day = [bool(_day_last_hour[_uniq[i]] >= 15) for i in range(len(_uniq))]

        # Ladestrategie PRO TAG. "auto": Tage mit deutlich mehr PV-Überschuss als
        # nutzbarer Akkukapazität -> peak (Spitze abschöpfen); sonst asap (verfüg-
        # bare PV früh einsammeln). "peak"/"asap" = alle Tage gleich.
        # Für die Entscheidung zählt das pessimistische p10 (falls vorhanden):
        # ein nur auf dem Erwartungswert "sichere" Peak-Tag wird konservativ
        # als asap behandelt (früh einsammeln statt auf die Spitze wetten).
        usable_wh = hb.max_soc_wh - hb.min_soc_wh
        pv_for_mode = pv10 if pv10 is not None else inp.pv_w
        _day_surplus = [0.0] * len(_uniq)
        for i in range(N):
            _day_surplus[slot_day[i]] += max(0.0, float(pv_for_mode[i]) - float(inp.house_load_w[i])) * dt
        if strategy == "auto":
            day_mode = ["peak" if _day_surplus[i] >= usable_wh else "asap"
                        for i in range(len(_uniq))]
        elif strategy == "peak":
            day_mode = ["peak"] * len(_uniq)
        else:
            day_mode = ["asap"] * len(_uniq)
        log.info("Ladestrategie '%s'%s -> Tage: %s", strategy,
                 " (p10-basiert)" if pv10 is not None else "",
                 {str(_uniq[i]): day_mode[i] for i in range(len(_uniq))})

        # Steuerbare/verschiebbare Lasten (Pool-WP etc.) – leere Liste = No-op.
        from .loads import add_controllable_loads
        cl_power, cl_cost, cl_outputs, cl_mqtt = add_controllable_loads(
            prob, cfg, inp, N, dt, g_imp=g_imp)

        for t in range(N):
            pv_t = float(max(0.0, inp.pv_w[t]))
            load_t = float(max(0.0, inp.house_load_w[t]))

            # DC-Laden und Abregelung nur aus PV
            prob += dc[t] + curt[t] <= pv_t
            pv_to_ac = pv_t - dc[t] - curt[t]   # >= 0 durch obige Schranke
            # DC-Laden zusätzlich auf den PV-ÜBERSCHUSS begrenzen: sonst
            # "tarnt" das LP Netzladen als DC-Laden (ganze PV in den Akku,
            # Hauslast aus dem Netz kaufen) - das kann der E3DC im Automatik-
            # modus nicht ausführen und es umginge den AC-Wirkungsgrad.
            # Echtes Netzladen läuft explizit über ac (Befehl batt_grid_charge_w).
            prob += dc[t] <= max(0.0, pv_t - load_t)

            # Wechselrichter-Durchsatz (mit Leistungs-Reserve #2)
            prob += pv_to_ac + dis[t] + ac[t] <= max_inv

            # AC-Knotenbilanz: Import - Export = Last + Auto + steuerbare Lasten
            #                  + AC-Laden - PV_AC - Entladung
            prob += (
                g_imp[t] - g_exp[t]
                == load_t + car[t] + cl_power[t] + ac[t] - pv_to_ac - dis[t]
            )

            # kein gleichzeitiges Laden/Entladen
            prob += dc[t] + ac[t] <= M_ch * is_ch[t]
            prob += dis[t] <= max_dis * is_di[t]
            prob += is_ch[t] + is_di[t] <= 1
            # Mindest-Entladeleistung (#1): 0 oder >= min_dis
            if min_dis > 0.0:
                prob += dis[t] >= min_dis * is_di[t]

            # Entladen nur zur Deckung der Restlast (+ steuerbare Lasten + Auto).
            # Bei PV-Überschuss kann der E3DC im Automatikmodus nicht "für den
            # Export" entladen - das wäre Akku->Netz und braucht den expliziten
            # grid_discharge-Pfad (gd_allowed). Verhindert zudem, dass die Linien-
            # Minimierung des Folgetags den Akku abends zur Einspeisevergütung
            # leerverkauft. WICHTIG: steuerbare Lasten (Pool-WP) gehören zur
            # lokalen Last - ohne cl_power dürfte der Akku sie nicht decken und
            # ihr Verbrauch (z.B. nachts) käme unnötig teuer aus dem Netz.
            if not gd_allowed[t]:
                prob += dis[t] <= max(0.0, load_t - pv_t) + cl_power[t] + car[t]
                # Da cl_power eine VARIABLE ist, ist die Obergrenze allein nicht
                # dicht: der Akku dürfte "für den Pool" entladen, obwohl die PV
                # den Pool schon deckt - die frei werdende PV ginge ins Netz
                # (getarntes Akku->Netz zur Einspeisevergütung). Dicht wird es
                # mit dem Ausschluss: NIE gleichzeitig entladen UND einspeisen
                # (im Ein-Knoten-Modell ist jede Entladung bei laufendem Export
                # ökonomisch ein Akku->Netz-Dump; weniger exportieren wäre immer
                # möglich). Arbitrage-Slots (gd_allowed) bleiben ausgenommen.
                prob += g_exp[t] <= BIGG * (1 - is_di[t])

            # physikalische Gesamt-Ladeleistung des Akkus (DC + AC zusammen)
            prob += dc[t] + ac[t] <= max_tot_ch

            # Eigenverbrauchs-Priorität / kein Akku->Netz (außer Arbitrage):
            prob += soc[t + 1] >= (hb.max_soc_wh - EPS_SOC) * is_full[t]
            prob += dc[t] + ac[t] >= (max_tot_ch - EPS_P) * at_max[t]
            # NETZ-Laden heißt Netz: die AC-Ladung darf den Netzbezug des Slots
            # nicht übersteigen. Ohne diese Kopplung durfte der Solver "ac" aus
            # PV-Überschuss speisen (imp=0) - physikalisch sinnlose Doppel-
            # wandlung (dafür gibt es den effizienteren DC-Pfad), die aber als
            # batt_grid_charge_w-Befehl real ECHTES Netzladen kommandiert hätte
            # (z.B. 87 W "Netzladen" bei 38 ct, gespeist aus PV).
            prob += ac[t] <= g_imp[t]
            # Strategie des jeweiligen Tages (peak/asap)
            dm = day_mode[slot_day[t]]
            # Peak-Tag: kein Netzladen (reines PV-Peak-Shaving; verhindert auch
            # das Terminal-Nachlade-Artefakt).
            if dm == "peak":
                prob += ac[t] == 0
            if not gd_allowed[t]:
                if dm == "peak":
                    # Peak-Tag: kein Akku->Netz-Dump; Einspeisung auf die Tages-
                    # Linie deckeln – aber nur an Tagen mit Spitze im Horizont.
                    # Abregelung zählt für die Linie wie Einspeisung, sonst
                    # könnte der Optimierer "abregeln statt Linie anheben".
                    prob += g_exp[t] <= pv_to_ac
                    if line_day[slot_day[t]]:
                        prob += g_exp[t] + curt[t] <= export_line[slot_day[t]]
                else:
                    # asap: Einspeisen nur, wenn Akku voll ODER mit Max-Leistung lädt.
                    prob += g_exp[t] <= BIGG * (is_full[t] + at_max[t])
            # Abregeln nur, wenn der Akku voll ist ODER mit Max-Leistung lädt -
            # sonst MUSS der Überschuss geladen/exportiert werden. Ohne diese
            # Regel darf der Solver innerhalb der MIP-Gap-Toleranz PV "aus
            # Faulheit" wegwerfen (z.B. 350 W abregeln statt laden, wenn das
            # Export-Gate zu ist und der Ladegewinn nur wenige ct beträgt) -
            # sichtbar als sinnlose Abregel-/Ladesperren-Slots im Plan. Immer
            # zulässig bleibt Abregeln bei vollem Akku (Export-Limit/Peak-Linie/
            # Negativpreis) - genau dann ist es real nötig.
            prob += curt[t] <= BIGG * (is_full[t] + at_max[t])
            # Kein gleichzeitiges Netzladen (Import) und Einspeisen (Export)
            prob += g_imp[t] <= BIGG * b_grid[t]
            prob += g_exp[t] <= BIGG * (1 - b_grid[t])

            # Einspeisebegrenzung am Netzanschluss (60/70%-Regel / §9 EEG):
            # keine Erlöse einplanen, die real abgeregelt würden.
            if cfg.inverter.max_export_w is not None:
                prob += g_exp[t] <= cfg.inverter.max_export_w

            # SoC-Dynamik Haus. AC-Laden (Netz) hat einen eigenen (schlechteren)
            # Wirkungsgrad als DC-Laden aus PV - sonst rechnet sich Netzladen
            # systematisch zu günstig.
            prob += soc[t + 1] == (
                soc[t]
                + hb.charge_efficiency * dc[t] * dt
                + hb.eff_ac_charge * ac[t] * dt
                - (1.0 / hb.discharge_efficiency) * dis[t] * dt
                # WR-Sockellast (#1): fixer Entnahmeverlust je aktivem Entlade-Slot
                - standby_w * is_di[t] * dt
            )

            if use_car:
                # semikontinuierlich: 0 oder [min,max]
                prob += car[t] <= veh.max_charge_w * is_car[t]
                prob += car[t] >= veh.min_charge_w * is_car[t]
                # Ladekurve (Taper): oberhalb taper_start sinkt die maximale
                # Leistung linear bis min_charge_w bei 100 %. Unterhalb ist die
                # Schranke lockerer als max_charge_w -> nicht bindend.
                if veh.taper_start_soc_percent < 100.0:
                    taper_wh = veh.capacity_wh * veh.taper_start_soc_percent / 100.0
                    denom = max(veh.capacity_wh - taper_wh, 1.0)
                    prob += car[t] <= veh.min_charge_w + \
                        (veh.max_charge_w - veh.min_charge_w) * \
                        (veh.capacity_wh - soc_car[t]) / denom
                prob += soc_car[t + 1] == soc_car[t] + veh.charge_efficiency * car[t] * dt
                # Ziel-SoC zur Abfahrt: WEICH (Slack mit hoher Strafe). Ein
                # unerreichbares Ziel (spät angesteckt, Taper) darf nicht die
                # gesamte Optimierung unlösbar machen - stattdessen wird so
                # viel wie möglich geladen und die Fehlmenge gemeldet.
                if t in dep_slots:
                    s = pulp.LpVariable(f"carshort_{t}", 0)
                    prob += soc_car[t] + s >= veh.target_soc_wh
                    car_short.append(s)

        # Ziel-SoC am letzten Slot ebenfalls sichern, falls keine Abfahrt im
        # Horizont liegt, aber an ANDEREN Wochentagen eine kommt (Vorbereitung
        # auf die nächste Abfahrt). Gibt es gar keine Abfahrten, entfällt das.
        if use_car and not dep_slots and veh.has_any_departure:
            s = pulp.LpVariable("carshort_end", 0)
            prob += soc_car[N] + s >= veh.target_soc_wh
            car_short.append(s)

        # ---- Zielfunktion ----------------------------------------------- #
        kwh = dt / 1000.0
        cost_terms = []
        for t in range(N):
            cost_terms.append(g_imp[t] * inp.price_ct_kwh[t] * kwh)
            cost_terms.append(-g_exp[t] * inp.feedin_ct_kwh[t] * kwh)

        # Zyklus-Malus: pen ist als ct je voll ZYKLIERTER kWh gemeint (einmal
        # rein + einmal raus). Auf Lade- UND Entladeleistung angewandt daher
        # 0.5 je Richtung, sonst würde ein Zyklus doppelt bestraft.
        pen = cfg.optimization.cycle_penalty_ct_kwh
        if pen:
            for t in range(N):
                cost_terms.append(0.5 * pen * (dc[t] + ac[t] + dis[t]) * kwh)

        # Auto-Ziel-Verfehlung bestrafen (weiche Nebenbedingung, s.o.)
        if car_short:
            cost_terms.append(cfg.optimization.car_target_penalty_ct_kwh *
                              pulp.lpSum(car_short) / 1000.0)

        # Wallbox-Schalt-Malus: jeder Einschaltvorgang (0 -> laden) kostet
        # car_switch_penalty_ct. Verhindert Dauer-Takten bei zappeligen
        # Preisen (Schützverschleiß); der erste Slot bleibt frei (Vorzustand
        # unbekannt).
        pen_sw = cfg.optimization.car_switch_penalty_ct
        if use_car and pen_sw:
            car_start = [pulp.LpVariable(f"carstart_{t}", 0, 1) for t in range(1, N)]
            for t in range(1, N):
                prob += car_start[t - 1] >= is_car[t] - is_car[t - 1]
            cost_terms.append(pen_sw * pulp.lpSum(car_start))

        # Batterie-Taktung: Ein einzelner Halte-Slot zwischen zwei
        # Entlade-Slots wird als 0-W-Limit an E3DC/MQTT ausgegeben und ist
        # damit ein realer, unnötiger Eingriff. Ein kleiner Malus je Wechsel
        # bevorzugt einen durchgehenden Eigenverbrauch, sofern der Preisvorteil
        # die Unterbrechung nicht klar rechtfertigt. Der erste Slot bleibt
        # frei, weil sein Vorzustand unbekannt ist.
        bat_pen = float(getattr(cfg.optimization, "battery_switch_penalty_ct", 0.0) or 0.0)
        if bat_pen and N > 1:
            bat_switch = [pulp.LpVariable(f"batswitch_{t}", 0, 1)
                          for t in range(1, N)]
            for t in range(1, N):
                prob += bat_switch[t - 1] >= is_di[t] - is_di[t - 1]
                prob += bat_switch[t - 1] >= is_di[t - 1] - is_di[t]
            cost_terms.append(bat_pen * pulp.lpSum(bat_switch))

        # Kleiner Tie-Breaker: DC-Laden (PV) gegenüber AC-Laden (Netz) bevorzugen,
        # wenn kostengleich. So wird AC-Laden nur genutzt, wenn es echten Vorteil
        # bringt (günstiger Netzbezug), nicht zum Wegrouten von PV-Überschuss.
        # Abregelung minimal bestrafen: nur als letzter Ausweg (Akku voll und
        # Export begrenzt/wertlos), nie statt Einspeisung mit Vergütung > 0.
        for t in range(N):
            cost_terms.append(0.02 * ac[t] * kwh)
            # Abregelung spürbar (0,5 ct/kWh) bestrafen - weit unter der
            # Einspeisevergütung (verzerrt keine echte Entscheidung), aber
            # genug, dass die MIP-Gap-Toleranz (gapAbs) nie "Abregeln statt
            # Einspeisen" als gleichwertig durchwinkt.
            cost_terms.append(0.5 * curt[t] * kwh)

        # Peak-Tage: Einspeise-Linie L minimieren -> so tief wie möglich, dass der
        # Akku gerade voll wird (Spitze über L lädt den Akku). asap-Tage: über die
        # harte Nebenbedingung oben.
        pw = cfg.optimization.peak_charge_weight
        _peak_line_days = [i for i in range(len(export_line))
                           if line_day[i] and day_mode[i] == "peak"]
        if pw and _peak_line_days:
            cost_terms.append(pw * pulp.lpSum(
                [export_line[i] for i in _peak_line_days]) / 1000.0)

        # p10-Absicherung an Peak-Tagen: Laden darf nur so weit aufgeschoben
        # werden, dass selbst der RESTLICHE p10-Überschuss des Tages den Akku
        # noch füllt. SoC-Untergrenze je Slot:
        #   soc[t] >= max_soc - eff * (künftiger p10-Überschuss des Tages).
        # Weich (15 ct/kWh Slack): deutlich teurer als entgangene Einspeisung
        # -> es wird früh geladen, wann immer physikalisch möglich; aber kein
        # hartes Veto (Anfangs-SoC kann die Grenze anfangs unterschreiten).
        if pv10 is not None and _peak_line_days:
            P10_PEN_CT_KWH = 15.0
            sd_arr = np.asarray(slot_day)
            loads = np.maximum(np.asarray(inp.house_load_w, dtype=float), 0.0)
            surplus10 = np.maximum(pv10 - loads, 0.0)
            for d in _peak_line_days:
                idxs = np.where(sd_arr == d)[0]
                s10 = surplus10[idxs]
                # künftiger Tages-Überschuss NACH Slot j (exklusiv)
                suffix = np.concatenate([np.cumsum(s10[::-1])[::-1][1:], [0.0]])
                for j, t in enumerate(idxs):
                    # Nur solange noch p10-Überschuss aussteht: die Grenze soll
                    # das AUFSCHIEBEN des Ladens begrenzen. Nach PV-Ende wäre
                    # floor = max_soc und würde das normale (teure!) Abend-
                    # entladen blockieren - genau dann muss sie entfallen.
                    if suffix[j] <= 0.0:
                        continue
                    floor = hb.max_soc_wh - hb.charge_efficiency * suffix[j] * dt
                    if floor <= hb.min_soc_wh:
                        continue
                    slack = pulp.LpVariable(f"p10s_{t}", 0)
                    prob += soc[t + 1] + slack >= min(floor, hb.max_soc_wh)
                    cost_terms.append(P10_PEN_CT_KWH * slack / 1000.0)

        # Terminalwert des gespeicherten Akku-Inhalts (Nutzen -> negativ),
        # mit Entlade-Wirkungsgrad diskontiert. Bei "auto" als FALLENDE
        # Grenzwert-Kurve in drei Segmenten: erstes Drittel über min_soc = Median-
        # preis, mittleres = unteres Quartil, letztes = nur Einspeisung (morgen
        # füllt PV ohnehin nach). Konkav fallend -> das LP füllt das wertvollste
        # Segment zuerst, keine Binärvariablen nötig. Fester Zahlenwert = flach.
        #
        # BEWUSST konservativ (früher p75/Mittel/p25): mit dem höheren Mittel-
        # segment bewertete der Optimierer die "letzte" gespeicherte kWh nahe am
        # Horizont-Ende teurer als den Netzbezug und HORTETE dann Akku-Energie -
        # er importierte/regelte PV ab, statt zu entladen (sinnlos aussehende
        # Abend-Eingriffe im Sommer). Median/p25/Einspeisung behebt das (die
        # Grenz-kWh liegt jetzt unter dem Bezugspreis) und ist zugleich billiger;
        # der Akku füllt sich weiterhin aus PV, da alle Segmente >= Einspeisung.
        tv = cfg.optimization.terminal_soc_value
        if tv == "auto":
            p = np.asarray(inp.price_ct_kwh, dtype=float)
            fin = float(np.mean(inp.feedin_ct_kwh))
            seg_values = sorted([
                max(float(np.percentile(p, 50)), fin),
                max(float(np.percentile(p, 25)), fin),
                fin,
            ], reverse=True)
        else:
            seg_values = [float(tv)] * 3
        usable_cap = hb.max_soc_wh - hb.min_soc_wh
        term_seg = [pulp.LpVariable(f"termseg_{i}", 0, usable_cap / 3.0)
                    for i in range(3)]
        prob += pulp.lpSum(term_seg) <= soc[N] - hb.min_soc_wh
        for i, v in enumerate(seg_values):
            cost_terms.append(-v * hb.discharge_efficiency * term_seg[i] / 1000.0)

        cost_terms.extend(cl_cost)          # Schalt-Malus + Komfort der Lasten
        prob += pulp.lpSum(cost_terms)

        # ---- Lösen ------------------------------------------------------- #
        _t0 = time.monotonic()
        warm = _shifted_warm_values(inp.index[0], int(round(dt * 60)))
        prob.solve(make_solver(cfg, warm_values=warm))
        solve_s = time.monotonic() - _t0
        hit_limit = solve_s >= cfg.optimization.solver_time_limit_s - 2.0
        if hit_limit:
            log.warning("Solver-Zeitlimit erreicht (%.0fs von %ds) – Lösung "
                        "kann deutlich suboptimal sein.", solve_s,
                        cfg.optimization.solver_time_limit_s)
        else:
            log.info("Solver fertig in %.1f s.", solve_s)
        status = pulp.LpStatus[prob.status]
        if prob.status != pulp.LpStatusOptimal:
            # Keine (verlässliche) Lösung: pulp.value() liefert dann None und
            # die Extraktion würde abstürzen. Stattdessen neutralen Fahrplan
            # liefern, damit weiterhin publiziert wird (setzt frühere Eingriffe
            # zurück) und Dashboard/InfluxDB konsistent bleiben.
            log.error("Optimierung nicht optimal gelöst (%s) – "
                      "Fallback 'auto' ohne Eingriffe.", status)
            return self._neutral_result(inp, status)
        infeasible = False
        # Politur: Gap-Toleranz-Reste exakt wegoptimieren (Binäre fixiert ->
        # LP), DANN erst die Lösung fürs Warmstarten merken - so startet der
        # nächste Zyklus vom polierten Stand statt vom Incumbent mit Resten.
        # Verdächtige Binäre bleiben dabei frei (Mini-MIP), damit die Politur
        # auch Einzel-Blasen auflösen kann:
        #  * Entladesperren-Blase: Import trotz Entladung 0 bei nutzbarem SoC
        #    (real: Sperre GENAU in der teuersten Reststunde) -> is_di frei.
        #  * Abregel-Blase: curt > 0, obwohl Export möglich wäre (real: 6,7 kW
        #    abgeregelt statt eingespeist, weil b_grid im Slot auf der Import-
        #    Seite festhing und den Export sperrte) -> b_grid/is_full/at_max
        #    des Slots frei. Real NÖTIGE Abregelung (Export-Limit, Peak-Linie,
        #    Negativpreis) bleibt: constraint-erzwungen, nicht Binärwahl.
        #  * is_full-Zwang (koppelt beide): fixierte is_full=1-Folge-Slots
        #    erzwingen den vollen Akku zu einem festen Zeitpunkt. Das zwang
        #    real (a) ein 6,8-kW-Netzladen in den Plan (Vollladung vorziehen,
        #    damit Export früher darf) und (b) hielt eine Entladesperre am
        #    Leben (Entladen um 04:45 hätte die Mittags-Vollladung um einen
        #    Slot verzögert - das LP DURFTE nicht, obwohl is_di frei war).
        #    Daher je Verdachts-Slot die is_full/at_max-Kette bis zum ENDE
        #    DER NÄCHSTEN VOLLPHASE freigeben - die Politur darf die
        #    Vollladung dann verschieben.
        _t1 = time.monotonic()

        def _full_chain(t0):
            """is_full/at_max-Namen ab t0 bis zum Ende der nächsten Vollphase
            (erste zusammenhängende is_full=1-Periode im Incumbent)."""
            names, seen_full = set(), False
            for k in range(t0, N):
                f = (is_full[k].varValue or 0.0) > 0.5
                if seen_full and not f:
                    break
                names.update({is_full[k].name, at_max[k].name})
                seen_full = seen_full or f
            return names

        def _suspect_free_names():
            """Verdachts-Binäre der AKTUELLEN Lösung (varValues)."""
            core, chain = set(), set()
            for t in range(N):
                hold_susp = ((g_imp[t].varValue or 0.0) > 5.0
                             and (dis[t].varValue or 0.0) < 1.0
                             and (soc[t + 1].varValue or 0.0)
                             > hb.min_soc_wh + 100.0)
                ac_susp = (ac[t].varValue or 0.0) > 5.0
                curt_susp = (curt[t].varValue or 0.0) > 5.0
                if hold_susp:
                    core.add(is_di[t].name)
                if curt_susp:
                    core.update({b_grid[t].name, is_full[t].name,
                                 at_max[t].name})
                if ac_susp:
                    core.add(b_grid[t].name)
                if hold_susp or ac_susp:
                    chain |= _full_chain(t)
            # Sicherheitsdeckel: Kern-Verdachte immer behalten, Ketten trimmen.
            if len(core) + len(chain) > 256:
                chain = set(sorted(chain)[:max(0, 256 - len(core))])
            return core | chain

        # ITERIEREN: die Politur kann beim exakten Umverteilen selbst neue
        # Blasen derselben Klassen erzeugen (real beobachtet: um eine fixierte
        # is_full-Vollladung zu erfüllen, kaufte das Politur-LP im billigsten
        # Slot 5,8 kW aus dem Netz - der Verdacht existierte im Incumbent noch
        # nicht, also war die Kette nicht freigegeben). Daher nach jeder Runde
        # die Verdachts-Erkennung auf dem POLIERTEN Stand wiederholen, bis
        # keine neuen Verdachte mehr auftauchen (max. 3 Runden, je ~0,3 s).
        _free = _suspect_free_names()
        _rounds = 0
        while _rounds < 3 and _polish_continuous(prob, cfg, free_names=_free):
            _rounds += 1
            _new = _suspect_free_names() - _free
            if not _new:
                break
            _free |= _new
        if _rounds:
            log.info("Politur in %.1f s (%d Runden, %d freie Binäre).",
                     time.monotonic() - _t1, _rounds, len(_free))
        # Lösung für den Warmstart des nächsten Zyklus merken.
        _store_warm_solution(prob, inp.index[0], int(round(dt * 60)))

        # ---- Ergebnis extrahieren --------------------------------------- #
        def val(v):
            return float(pulp.value(v)) if not isinstance(v, (int, float)) else float(v)

        shortfall = max((val(s) for s in car_short), default=0.0)
        if shortfall > 100.0:
            log.warning("Auto-Ziel-SoC im Plan nicht erreichbar: es fehlen "
                        "%.1f kWh zur Abfahrt.", shortfall / 1000.0)

        # Tages-Linien-Werte (Peak-Modus) je Slot. Ungenutzte L (Tage ohne Linie)
        # bleiben None -> als NaN behandeln.
        def _lv(L):
            v = pulp.value(L)
            return float(v) if v is not None else float("nan")
        # Für Peak-Tage gesetzt, sonst NaN (asap-Tage haben keine Linie).
        line_vals = [_lv(L) for L in export_line]

        rows = []
        for t in range(N):
            dc_v, ac_v, dis_v = val(dc[t]), val(ac[t]), val(dis[t])
            curt_v = val(curt[t])
            car_v = val(car[t]) if use_car else 0.0
            imp_v, exp_v = val(g_imp[t]), val(g_exp[t])
            soc_v = val(soc[t + 1])
            # Mikro-Netzladen (< 100 W, MIP-Gap-Rauschen) NIE als Befehl ausgeben:
            # ein "Netzladen 87 W"-Kommando ist für ein 12-kW-System sinnlos und
            # würde real Mode-4-Netzladen erzwingen. ac <= g_imp (Modell) macht
            # das Streichen exakt bilanz-konsistent (Import sinkt um denselben
            # Betrag); die SoC-Abweichung (< 25 Wh) liegt in der Toleranz.
            if 0.0 < ac_v < 100.0:
                imp_v = max(0.0, imp_v - ac_v)
                ac_v = 0.0
            # Abregel-Krümel (MIP-Gap-Rauschen) deterministisch bereinigen: wird
            # im selben Slot bereits EXPORTIERT (Export-Gate offen, kein Import),
            # ist "abregeln statt mehr einspeisen" beweisbar dominiert -> Rest in
            # Export umbuchen. Exakt bilanz-konsistent (pv_to_ac und g_exp steigen
            # um denselben Betrag; Peak-Linie deckelt g_exp+curt, Summe konstant).
            # Schranken: Netzanschluss-Limit und WR-Durchsatz; nur bei positiver
            # Vergütung (sonst wäre Abregeln ggf. gewollt).
            if curt_v > 0.0 and exp_v > 1.0 and float(inp.feedin_ct_kwh[t]) > 0.0:
                cap = (cfg.inverter.max_export_w if cfg.inverter.max_export_w
                       is not None else float("inf"))
                pv_to_ac_v = max(0.0, float(inp.pv_w[t])) - dc_v - curt_v
                room_inv = max_inv - (pv_to_ac_v + dis_v + ac_v)
                shift = max(0.0, min(curt_v, cap - exp_v, room_inv))
                curt_v -= shift
                exp_v += shift
            slot_cost = (imp_v * inp.price_ct_kwh[t] - exp_v * inp.feedin_ct_kwh[t]) * kwh

            # --- Übersetzung in E3DC-Steuerbefehle -----------------------
            # Der E3DC regelt selbst (Eigenverbrauch): lädt aus PV-Überschuss,
            # entlädt zur Lastdeckung. Es wird nur ein LIMIT gesetzt, wenn der
            # Optimierer vom natürlichen Verhalten abweichen will:
            #   * Lade-/Entladelimit = Hardware-Maximum  -> "frei laufen" (kein Eingriff)
            #   * Lade-/Entladelimit < Maximum           -> gezielte Begrenzung/Sperre
            #   * grid_charge_w > 0                      -> Netzladen erzwingen
            pv_t = float(max(0.0, inp.pv_w[t]))
            # Steuerbare Lasten (Pool-WP etc.) sind für den E3DC ganz normale
            # Hausverbraucher: der Eigenverbrauch bedient sie aus PV, BEVOR der Akku
            # geladen wird. Für die "natürliche" Lade-/Entladeleistung daher zur
            # Hauslast zählen - sonst sieht die Klassifikation die vom Pool
            # verbrauchte PV als fehlende Akku-Ladung und meldet fälschlich
            # "Laden gesperrt/gedrosselt", obwohl gar kein Akku-Eingriff vorliegt.
            cl_v = float(max(0.0, val(cl_power[t]))) if cl_power else 0.0
            load_t = float(max(0.0, inp.house_load_w[t])) + cl_v
            nat_charge = min(max(0.0, pv_t - load_t), hb.max_dc_charge_w)
            nat_dis = min(max(0.0, load_t - pv_t), hb.max_discharge_w)
            tol = 5.0
            # Ein Eingriff (Lade-/Entladesperre/-drossel) wird erst ab einer
            # SPÜRBAREN Abweichung vom Eigenverbrauch gemeldet. Bei PV≈Last ist
            # die natürliche Lade-/Entladeleistung nur ein paar Watt Rauschen -
            # daraus keine sinnlose Sperre ableiten (die real nur Sub-Slot-
            # Netzbezug erzwänge). Peak-Shaping ist davon unberührt (dort ist die
            # Abweichung deutlich größer).
            act_floor = 100.0   # W
            full_tol = 0.02 * hb.capacity_wh   # 2 % Toleranz für "praktisch voll/leer"
            battery_full = soc_v >= hb.max_soc_wh - full_tol
            battery_empty = soc_v <= hb.min_soc_wh + full_tol
            # PV-Laden nur dann als "begrenzt" markieren, wenn der Akku noch Platz
            # hätte (sonst lädt er ohnehin nicht weiter -> kein Eingriff, "auto").
            if dc_v < nat_charge - act_floor and not battery_full:
                charge_limit, charge_limited = round(dc_v, 1), 1
            else:
                charge_limit, charge_limited = hb.max_dc_charge_w, 0
            # Entladen nur dann als "gesperrt/gedrosselt" markieren, wenn noch
            # Ladung vorhanden ist (sonst kann er ohnehin nicht entladen).
            if dis_v < nat_dis - act_floor and not battery_empty:
                dis_limit, dis_limited = round(dis_v, 1), 1
            else:
                dis_limit, dis_limited = hb.max_discharge_w, 0
            # Modus. Im Peak-Modus ist das geformte Laden entlang der Linie
            # NORMAL (kein "Eingriff"), bekommt aber den eigenen Modus "peak",
            # damit es in der Zeitleiste sichtbar ist. Gedrosseltes Laden und
            # Entladen sind getrennte Modi.
            peak_shaped = charge_limited and day_mode[slot_day[t]] == "peak"
            charge_flag = charge_limited and not peak_shaped
            if ac_v > tol:
                mode = "grid_charge"
            elif charge_flag and charge_limit < tol:
                mode = "block_charge"
            elif dis_limited and dis_limit < tol:
                mode = "hold"                         # Entladen gesperrt (Akku halten)
            elif dis_limited:
                mode = "limit_discharge"              # Entladen gedrosselt
            elif charge_flag:
                mode = "limit_charge"                 # Laden gedrosselt
            elif peak_shaped:
                mode = "peak"                         # geformtes Laden (Linie)
            else:
                mode = "auto"
            # Netz-Entladen (Akku -> Netz): der Teil der Einspeisung, der nicht aus
            # PV stammt. Bei allow_grid_discharge=False ist das 0.
            grid_discharge_v = max(0.0, exp_v - max(0.0, pv_t - dc_v - curt_v))
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
                "pv_curtail_w": round(curt_v, 1),
                "house_soc_wh": round(soc_v, 1),
                "house_soc_percent": round(100.0 * soc_v / hb.capacity_wh, 2),
                "export_line_w": (round(line_vals[slot_day[t]], 1)
                                  if (line_day[slot_day[t]] and day_mode[slot_day[t]] == "peak")
                                  else np.nan),
                "slot_cost_ct": round(slot_cost, 4),
            }
            if use_car:
                soc_car_v = val(soc_car[t + 1])
                row["car_soc_wh"] = round(soc_car_v, 1)
                row["car_soc_percent"] = round(100.0 * soc_car_v / veh.capacity_wh, 2)
            for _col, _exprs in cl_outputs.items():   # steuerbare Lasten (W / °C)
                row[_col] = round(val(_exprs[t]), 2)
            rows.append(row)

        table = pd.DataFrame(rows, index=inp.index)
        total = float(table["slot_cost_ct"].sum())
        line_w = (float(line_vals[slot_day[0]])
                  if (line_day[slot_day[0]] and day_mode[slot_day[0]] == "peak") else None)
        return OptimizerResult(
            table=table, total_cost_ct=total, status=status, infeasible=infeasible,
            export_line_w=line_w, car_target_shortfall_wh=round(shortfall, 1),
            solver_hit_limit=hit_limit, load_mqtt_map=cl_mqtt,
        )
