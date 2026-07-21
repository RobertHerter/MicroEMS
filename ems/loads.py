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

# Komfort-Malus je K·Slot Bandverletzung. GRÖSSENORDNUNG MIT BEDACHT: 1 K eine
# Stunde lang verfehlt = 4*50 = 200 ct - mehr, als das Nachheizen von 1 K je
# kostet (~2-3 kWh_el ~ 60-90 ct) -> Komfort dominiert die Energie-Ökonomie und
# das Band wird gehalten. Aber NICHT mehr um Größenordnungen darüber (früher
# 1000 ct/K·Slot): sonst sind Milli-Kelvin dem Solver Cent-Beträge "wert" und
# er tauscht innerhalb der MIP-Toleranz echtes Geld gegen unfühlbare
# Temperatur-Differenzen (Cash-Verschwendung bei Status "Optimal").
_COMFORT_PEN = 50.0
_RUNTIME_PEN = 1000.0     # ct je fehlendem Laufzeit-Slot


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "load"


def _md(s: str):
    m, d = s.split("-")
    return int(m), int(d)


def in_season(load, ts) -> bool:
    """Ist der Zeitstempel in der Saison der Last? (season_from/to MM-DD, Wrap ok)"""
    if not (load.season_from and load.season_to):
        return True
    lo, hi = _md(load.season_from), _md(load.season_to)
    cur = (ts.month, ts.day)
    return (lo <= cur <= hi) if lo <= hi else (cur >= lo or cur <= hi)


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


def add_controllable_loads(prob, config, inp, N, dt, g_imp=None):
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
    solar = (np.asarray(inp.solar_w_m2, dtype=float)
            if inp.solar_w_m2 is not None else None)
    # Wetter ist optional. Rest-NaN dürfen niemals als PuLP-Konstanten enden:
    # ohne vollständige Temperatur neutral am Zielwert rechnen, fehlende
    # Strahlung konservativ als 0 (keinen solaren Wärmeeintrag erfinden).
    if amb is not None and (len(amb) != N or not np.all(np.isfinite(amb))):
        amb = None
    if solar is not None:
        if len(solar) != N:
            solar = None
        else:
            solar = np.nan_to_num(solar, nan=0.0, posinf=0.0, neginf=0.0)
    state = inp.load_state or {}
    on_by_key: dict = {}          # für `requires`-Kopplung

    for ld in loads:
        active = _season_mask(ld, md_list)
        if ld.type == "thermal":
            _add_thermal(prob, ld, inp, N, dt, cl_power, cost_terms, outputs,
                         mqtt_map, amb, solar, state, active, on_by_key, g_imp,
                         config)
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

    # Deadline: die Laufzeit muss innerhalb von deadline_hours ab JETZT (Slot 0)
    # abgeschlossen sein - sonst schiebt der Optimierer den Lauf für Cent-
    # Bruchteile ans Horizontende ("Waschmaschine erst übermorgen"). Slots
    # jenseits der Deadline sind gesperrt; wäre dadurch GAR KEIN Start mehr
    # möglich (z.B. sehr enge Fenster), wird die Deadline ignoriert statt das
    # Problem unlösbar zu machen.
    ddl = float(ld.deadline_hours or 0.0)

    if prof is not None:
        # Startzyklus: 'on' markiert Startslot; Profil läuft ab da (nicht unterbr.).
        L = len(prof)
        ok = [bool(active[t] and ld.window_from_hour <= hours[t] < ld.window_to_hour
                   and t + L <= N) for t in range(N)]
        if ddl > 0:
            within = [ok[t] and (t + L) * dt <= ddl for t in range(N)]
            if any(within):
                ok = within
        for t in range(N):
            if not ok[t]:
                prob += on[t] == 0
        runs = max(1, int(round(ld.runtime_minutes / (dt * 60 * L)))) if ld.runtime_minutes else 1
        # Nie mehr Starts fordern, als Start-Slots existieren (sonst wird das
        # GANZE MILP unlösbar, z.B. außerhalb der Saison: alle on==0 erzwungen,
        # aber sum(on)==1 gefordert -> Infeasible).
        prob += pulp.lpSum(on) == min(runs, sum(ok))
        for t in range(N):
            # Leistung im Slot t = Summe der Starts, deren Profil hier hinreicht
            expr = pulp.lpSum(prof[t - s] * on[s]
                              for s in range(max(0, t - L + 1), t + 1))
            cl_power[t] = cl_power[t] + expr
            outputs.setdefault(f"load_{sg}_w", [None] * N)[t] = expr
    else:
        ok = [bool(active[t] and ld.window_from_hour <= hours[t] < ld.window_to_hour)
              for t in range(N)]
        if ddl > 0:
            req_slots_ddl = int(np.ceil(ld.runtime_minutes / (dt * 60.0)))
            within = [ok[t] and (t + 1) * dt <= ddl for t in range(N)]
            if sum(within) >= req_slots_ddl > 0 or (ld.runtime_minutes <= 0 and any(within)):
                ok = within
        for t in range(N):
            if not ok[t]:
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
                 amb, solar, state, active, on_by_key, g_imp=None, config=None):
    sg = _slug(ld.name)
    C = ld.capacity_wh_per_k
    if C <= 0 or not ld.stages:
        return
    T0 = float(state.get(ld.name, ld.target_c))
    T0 = min(max(T0, ld.min_c - 5.0), ld.max_c + 5.0)
    Tamb = amb if amb is not None else np.full(N, ld.target_c)
    # Solar-Einstrahlung (W/m²) -> Wärmeeintrag = Fläche * Wirkungsgrad * Strahlung.
    # surface_m2=0 (Default) -> Term entfällt, unverändertes Verhalten wie bisher.
    Solar = solar if solar is not None else np.zeros(N)
    solar_gain = ld.surface_m2 * ld.solar_absorption

    # Freilauf-Trajektorien (DETERMINISTISCH, da die Stufen nur heizen können):
    # T_off = minimal erreichbare Temperatur (alle Stufen AUS),
    # T_on  = maximal erreichbare Temperatur (alle Stufen AN, wo aktiv).
    # Daraus folgt die UNVERMEIDBARE Bandverletzung: liegt schon T_off über
    # max_c (heißes Wasser, Solar) bzw. T_on unter min_c (tiefer Winter), kann
    # KEINE Entscheidung sie verhindern.
    def _free_run(heat_on_w: np.ndarray) -> np.ndarray:
        traj = np.empty(N + 1)
        traj[0] = T0
        for t in range(N):
            heat = float(heat_on_w[t]) + solar_gain * float(Solar[t])
            loss = ld.loss_w_per_k * (traj[t] - float(Tamb[t]))
            traj[t + 1] = traj[t] + (heat - loss) * dt / C
        return traj
    all_heat = float(sum(st.heat_w for st in ld.stages))
    T_off = _free_run(np.zeros(N))
    # Maximal erreichbar: alle Stufen an, wo aktiv. (Bei no_grid_import kann
    # Heizen in seltenen Fällen zusätzlich unmöglich sein - trübe Tage MIT
    # leerem Akku; das hängt aber von Akku-Entscheidungen ab und ist nicht
    # deterministisch vorhersagbar. T_on überschätzt dann das Erreichbare
    # leicht -> unavoid_lo wird UNTERschätzt, der Slack-Offset bleibt >= 0,
    # nur ein kleiner konstanter Rest verbleibt im Ziel. Sichere Richtung.)
    heat_ok = np.asarray(active, dtype=bool)
    T_on = _free_run(np.where(heat_ok, all_heat, 0.0))
    unavoid_hi = float(np.clip(T_off - ld.max_c, 0.0, None).sum())
    unavoid_lo = float(np.clip(ld.min_c - T_on, 0.0, None).sum())

    # Temperatur darf das Band nach OBEN verlassen: die Stufen können nur HEIZEN,
    # nicht kühlen. An heißen Tagen (Tamb > T) gewinnt der Pool auch mit allen WP
    # aus passiv Wärme -> ein hartes max_c wäre dann UNLÖSBAR. Deshalb weiches Band
    # in beide Richtungen (Über-/Unterschreitung mit Komfort-Malus), Var-Grenzen nur
    # als weiter Sicherheitsrahmen (an die physikalisch erreichbaren Trajektorien
    # angepasst, sonst würde z.B. starker Solar-Eintrag die Obergrenze sprengen).
    t_lb = min(ld.min_c - 10.0, float(T_off.min()) - 1.0)
    t_ub = max(ld.max_c + 10.0, float(T_on.max()) + 1.0)
    T = [pulp.LpVariable(f"clT_{sg}_{t}", t_lb, t_ub) for t in range(N + 1)]
    prob += T[0] == T0
    slack = [pulp.LpVariable(f"clSlo_{sg}_{t}", 0) for t in range(N + 1)]    # unter min_c
    slack_hi = [pulp.LpVariable(f"clShi_{sg}_{t}", 0) for t in range(N + 1)]  # über max_c

    # Entscheidungsraster: ein träger thermischer Speicher braucht keine 15-min-
    # Schaltentscheidungen. Eine Binärvariable je BLOCK (Default 60 min) statt je
    # Slot viertelt die Binärvariablen - der Unterschied zwischen "Solver findet
    # das Optimum in Sekunden" und "läuft ins Zeitlimit und liefert grobe
    # Zwischenstände (seltsame Sperren)". Schont zudem die WP-Kompressoren.
    dm = ld.decision_minutes or 60
    blk = max(1, int(round(dm / (dt * 60.0))))
    n_blocks = (N + blk - 1) // blk
    binary_h = max(0.0, float(getattr(ld, "binary_horizon_hours", 12.0)))
    binary_blocks = (n_blocks if binary_h <= 0.0 else min(
        n_blocks, int(np.ceil(binary_h / (blk * dt)))))
    stage_on: dict = {}
    for st in ld.stages:
        ssg = _slug(st.name)
        # Rolling Horizon: Nur zeitnah auszuführende Blöcke müssen diskret sein.
        # Fernere Blöcke als Duty-Cycle halten die thermische Energiebilanz und
        # Kostenwirkung, ohne den Branch-and-Bound-Baum über mehrere Tage zu
        # vervielfachen. Bevor sie real geschaltet werden, rücken sie in einem
        # Folgelauf automatisch in den binären Bereich.
        blk_var = [pulp.LpVariable(
            f"cl_{sg}_{ssg}_b{b}", 0, 1,
            cat="Binary" if b < binary_blocks else "Continuous")
            for b in range(n_blocks)]
        on = [blk_var[t // blk] for t in range(N)]     # je Slot -> Block-Variable
        stage_on[st.name] = on
        on_by_key[f"{ld.name}/{st.name}"] = on
        for t in range(N):
            if not active[t]:
                prob += on[t] == 0

    for st in ld.stages:                       # Kopplung (z.B. groß nur mit klein)
        if st.requires and st.requires in stage_on:
            for t in range(N):
                prob += stage_on[st.name][t] <= stage_on[st.requires][t]

    # Heizen ohne Netzbezug: läuft eine Stufe im Slot, muss der Netzbezug dort
    # 0 sein - PV-Überschuss UND Akku dürfen die WP decken, Netzstrom nie.
    # Big-M-Kopplung je Stufe an g_imp; mit dem Stunden-Entscheidungsraster
    # gilt das für JEDEN Slot des Blocks (Block sonst aus).
    if ld.no_grid_import and g_imp is not None and config is not None:
        M_imp = (float(np.max(np.maximum(inp.house_load_w, 0.0)) if N else 0.0)
                 + config.house_battery.max_ac_charge_w
                 + (config.vehicle.max_charge_w if config.vehicle.enabled else 0.0)
                 + sum(st.power_w for st in ld.stages) + 1000.0)
        for t in range(N):
            for st in ld.stages:
                prob += g_imp[t] <= M_imp * (1 - stage_on[st.name][t])

    for t in range(N):
        heat = (pulp.lpSum(st.heat_w * stage_on[st.name][t] for st in ld.stages)
                + solar_gain * float(Solar[t]))
        elec = pulp.lpSum(st.power_w * stage_on[st.name][t] for st in ld.stages)
        cl_power[t] = cl_power[t] + elec
        loss = ld.loss_w_per_k * (T[t] - float(Tamb[t]))
        prob += T[t + 1] == T[t] + (heat - loss) * dt / C
        prob += T[t] + slack[t] >= ld.min_c
        prob += T[t] - slack_hi[t] <= ld.max_c
    prob += T[N] + slack[N] >= ld.min_c
    prob += T[N] - slack_hi[N] <= ld.max_c
    # Komfort-Malus NUR auf den VERMEIDBAREN Teil der Bandverletzung: die
    # unvermeidbare Verletzung (Wasser wärmer als max_c und nicht kühlbar, bzw.
    # selbst Volllast-Heizen erreicht min_c nicht) wird als Konstante abgezogen.
    # Ohne den Abzug bläht z.B. 1,5 K Dauer-Überschreitung das Solver-Ziel um
    # tausende Euro auf - und die RELATIVE MIP-Gap (1 %) erlaubt dann real um
    # zig Euro schlechtere Pläne ("Optimal" mit Entladesperren/econ-Lücke).
    # Der Abzug ist eine Konstante -> ändert KEINE Entscheidung, nur die Skala.
    cost_terms.append(_COMFORT_PEN * (pulp.lpSum(slack) - unavoid_lo))
    cost_terms.append(_COMFORT_PEN * (pulp.lpSum(slack_hi) - unavoid_hi))

    for st in ld.stages:
        ssg = _slug(st.name)
        _switch_penalty(prob, stage_on[st.name], N, ld.switch_penalty_ct,
                        cost_terms, f"cl_{sg}_{ssg}")
        col = f"load_{sg}_{ssg}_w"
        outputs[col] = [st.power_w * stage_on[st.name][t] for t in range(N)]
        mqtt_map.append({"label": f"{ld.name}/{st.name}", "column": col,
                         "topic": st.mqtt_topic})
    outputs[f"load_{sg}_temp_c"] = [T[t] for t in range(N)]
