"""Steuerbare/verschiebbare Lasten für den MILP-Optimierer.

Zwei Typen:
  * "deferrable": muss `runtime_minutes` im Zeitfenster laufen (Leistung konstant
    oder als 15-min-Kurve als Startzyklus). Interruptierbar bei konstanter Leistung.
  * "thermal": thermischer Speicher (z.B. Pool) mit Temperatur als MILP-Zustand,
    geheizt über `stages` (on/off, ggf. gekoppelt), gehalten im Band [min_c, max_c];
    Wärmeverlust ~ k·(T − T_außen). Der Optimierer legt die Laufzeit in die
    günstigsten/PV-reichsten Slots.

`add_controllable_loads` erzeugt die Variablen/Nebenbedingungen und liefert:
  cl_power[t]  – elektrische Gesamtlast (W) der steuerbaren Lasten je Slot (in die
                 AC-Bilanz einzuhängen),
  cost_terms   – Zusatz-Zielterme (Schalt-Malus, Komfort-Slack),
  outputs      – {Spaltenname: [Ausdruck je Slot]} für die Ergebnistabelle,
  mqtt_map     – [(topic, Spaltenname)] für die Sollwert-Ausgabe.
Bei leerer/deaktivierter Liste: alles leer -> Optimierer unverändert.
"""
from __future__ import annotations

import re

import numpy as np
import pulp

_COMFORT_PEN = 1000.0     # ct je K·Slot Bandverletzung (Komfort dominiert)
_RUNTIME_PEN = 1000.0     # ct je fehlendem Laufzeit-Slot


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "load"


def _md(s: str):
    m, d = s.split("-")
    return int(m), int(d)


def _season_mask(load, md_list) -> np.ndarray:
    """Bool je Slot: ist die Last in Saison? (season_from/to als MM-DD, Wrap ok)."""
    n = len(md_list)
    if not (load.season_from and load.season_to):
        return np.ones(n, dtype=bool)
    lo, hi = _md(load.season_from), _md(load.season_to)
    out = np.empty(n, dtype=bool)
    for i, cur in enumerate(md_list):
        out[i] = (lo <= cur <= hi) if lo <= hi else (cur >= lo or cur <= hi)
    return out


def _switch_penalty(prob, on, N, pen_ct, cost_terms, tag):
    if pen_ct <= 0:
        return
    for t in range(1, N):
        sw = pulp.LpVariable(f"{tag}_sw_{t}", 0)
        prob += sw >= on[t] - on[t - 1]
        cost_terms.append(pen_ct * sw)


def add_controllable_loads(prob, config, inp, N, dt):
    cl_power = [pulp.LpAffineExpression() for _ in range(N)]
    cost_terms: list = []
    outputs: dict = {}
    mqtt_map: list = []
    loads = [ld for ld in getattr(config, "controllable_loads", []) if ld.enabled]
    if not loads or N == 0:
        return cl_power, cost_terms, outputs, mqtt_map

    tz = config.general.timezone
    local = inp.index.tz_convert(tz)
    hours = np.array([t.hour for t in local])
    md_list = [(t.month, t.day) for t in local]
    amb = (np.asarray(inp.ambient_temp_c, dtype=float)
           if inp.ambient_temp_c is not None else None)
    state = inp.load_state or {}
    on_by_key: dict = {}          # für `requires`-Kopplung

    for ld in loads:
        active = _season_mask(ld, md_list)
        if ld.type == "thermal":
            _add_thermal(prob, ld, inp, N, dt, cl_power, cost_terms, outputs,
                         mqtt_map, amb, state, active, on_by_key)
        else:
            _add_deferrable(prob, ld, inp, N, dt, cl_power, cost_terms, outputs,
                            mqtt_map, hours, active, on_by_key)
    return cl_power, cost_terms, outputs, mqtt_map


def _add_deferrable(prob, ld, inp, N, dt, cl_power, cost_terms, outputs, mqtt_map,
                    hours, active, on_by_key):
    sg = _slug(ld.name)
    prof = np.asarray(ld.power_profile_w, dtype=float) if ld.power_profile_w else None
    on = [pulp.LpVariable(f"cl_{sg}_{t}", cat="Binary") for t in range(N)]
    on_by_key[ld.name] = on

    if prof is not None:
        # Startzyklus: 'on' markiert Startslot; Profil läuft ab da (nicht unterbr.).
        L = len(prof)
        for t in range(N):
            if not (active[t] and ld.window_from_hour <= hours[t] < ld.window_to_hour
                    and t + L <= N):
                prob += on[t] == 0
        runs = max(1, int(round(ld.runtime_minutes / (dt * 60 * L)))) if ld.runtime_minutes else 1
        prob += pulp.lpSum(on) == runs
        for t in range(N):
            # Leistung im Slot t = Summe der Starts, deren Profil hier hinreicht
            expr = pulp.lpSum(prof[t - s] * on[s]
                              for s in range(max(0, t - L + 1), t + 1))
            cl_power[t] = cl_power[t] + expr
            outputs.setdefault(f"load_{sg}_w", [None] * N)[t] = expr
    else:
        for t in range(N):
            if not (active[t] and ld.window_from_hour <= hours[t] < ld.window_to_hour):
                prob += on[t] == 0
            cl_power[t] = cl_power[t] + ld.power_w * on[t]
            outputs.setdefault(f"load_{sg}_w", [None] * N)[t] = ld.power_w * on[t]
        req_slots = ld.runtime_minutes / (dt * 60.0)
        if req_slots > 0:
            short = pulp.LpVariable(f"cl_{sg}_short", 0)
            prob += pulp.lpSum(on) + short >= req_slots
            cost_terms.append(_RUNTIME_PEN * short)

    if ld.requires and ld.requires in on_by_key:
        req_on = on_by_key[ld.requires]
        for t in range(N):
            prob += on[t] <= req_on[t]
    _switch_penalty(prob, on, N, ld.switch_penalty_ct, cost_terms, f"cl_{sg}")
    mqtt_map.append({"label": ld.name, "column": f"load_{sg}_w",
                     "topic": ld.mqtt_topic})


def _add_thermal(prob, ld, inp, N, dt, cl_power, cost_terms, outputs, mqtt_map,
                 amb, state, active, on_by_key):
    sg = _slug(ld.name)
    C = ld.capacity_wh_per_k
    if C <= 0 or not ld.stages:
        return
    T0 = float(state.get(ld.name, ld.target_c))
    T0 = min(max(T0, ld.min_c - 5.0), ld.max_c)
    Tamb = amb if amb is not None else np.full(N, ld.target_c)

    T = [pulp.LpVariable(f"clT_{sg}_{t}", ld.min_c - 10.0, ld.max_c) for t in range(N + 1)]
    prob += T[0] == T0
    slack = [pulp.LpVariable(f"clSlo_{sg}_{t}", 0) for t in range(N + 1)]

    stage_on: dict = {}
    for st in ld.stages:
        ssg = _slug(st.name)
        on = [pulp.LpVariable(f"cl_{sg}_{ssg}_{t}", cat="Binary") for t in range(N)]
        stage_on[st.name] = on
        on_by_key[f"{ld.name}/{st.name}"] = on
        for t in range(N):
            if not active[t]:
                prob += on[t] == 0

    for st in ld.stages:                       # Kopplung (z.B. groß nur mit klein)
        if st.requires and st.requires in stage_on:
            for t in range(N):
                prob += stage_on[st.name][t] <= stage_on[st.requires][t]

    for t in range(N):
        heat = pulp.lpSum(st.heat_w * stage_on[st.name][t] for st in ld.stages)
        elec = pulp.lpSum(st.power_w * stage_on[st.name][t] for st in ld.stages)
        cl_power[t] = cl_power[t] + elec
        loss = ld.loss_w_per_k * (T[t] - float(Tamb[t]))
        prob += T[t + 1] == T[t] + (heat - loss) * dt / C
        prob += T[t] + slack[t] >= ld.min_c
    prob += T[N] + slack[N] >= ld.min_c
    cost_terms.append(_COMFORT_PEN * pulp.lpSum(slack))

    for st in ld.stages:
        ssg = _slug(st.name)
        _switch_penalty(prob, stage_on[st.name], N, ld.switch_penalty_ct,
                        cost_terms, f"cl_{sg}_{ssg}")
        col = f"load_{sg}_{ssg}_w"
        outputs[col] = [st.power_w * stage_on[st.name][t] for t in range(N)]
        mqtt_map.append({"label": f"{ld.name}/{st.name}", "column": col,
                         "topic": st.mqtt_topic})
    outputs[f"load_{sg}_temp_c"] = [T[t] for t in range(N)]
