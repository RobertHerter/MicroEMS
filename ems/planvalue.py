"""Entscheidungsqualität statt Prognosefehler: was hat die Unsicherheit gekostet?

Die Delta-Anzeigen im Dashboard messen Prognose-FEHLER. Das beantwortet nicht,
ob der Optimierer die besten Zeitpunkte getroffen hat: ein grosser PV-Fehler
mittags kostet nichts, wenn der Akku ohnehin voll ist, ein kleiner Fehler in der
Abendspitze kann teuer sein. Ausserdem wird der Plan alle 15 min neu gerechnet -
die Guete des Morgenplans wird dabei nie bewertet.

Dieses Modul liefert zwei Auswertungen, beide rein lesend:

``timing_quality``  Lag die real entladene Energie in den teuersten Stunden und
    das Netzladen in den guenstigsten? Die Bestmarke wird als kleines LP
    bestimmt - begrenzt durch die Restlast (Akku->Netz ist gesperrt), die
    Leistungsgrenzen und die zum jeweiligen Zeitpunkt real verfuegbare Energie.
    Ohne diese Schranken waere die Marke unerreichbar und der Wert wertlos.

``regret_analysis``  Vier Kosten desselben Tages, alle auf den ECHTEN Ist-Daten
    bewertet, und daraus die Zerlegung:

        Prognose-Regret       = fixierter 00:00-Plan  - Hellsicht-Optimum
        Nutzen der Neuplanung = fixierter 00:00-Plan  - rollierender Plan
        Ausfuehrungsverlust   = real abgerechnet      - rollierender Plan
        Gesamtluecke          = real abgerechnet      - Hellsicht-Optimum

    Alle Betraege in Euro pro Tag - direkt interpretierbar, anders als ein WAPE.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
import pulp

log = logging.getLogger("ems.planvalue")


# --------------------------------------------------------------------------- #
# Ist-Daten und Preise
# --------------------------------------------------------------------------- #
def _day_bounds(config, day) -> tuple[pd.Timestamp, pd.Timestamp]:
    tz = config.general.timezone
    start = pd.Timestamp(day).tz_localize(tz) if pd.Timestamp(day).tzinfo is None \
        else pd.Timestamp(day).tz_convert(tz)
    start = start.normalize()
    return start, start + pd.Timedelta(days=1)


def load_day(config, day) -> Optional[dict]:
    """Ist-Werte und Preise eines Tages einlesen (Kurzform von ``load_range``)."""
    start, end = _day_bounds(config, day)
    return load_range(config, start, end)


def load_range(config, start, end) -> Optional[dict]:
    """Ist-Werte und Preise eines Zeitraums auf dem Slot-Raster einlesen.

    None, wenn der Zeitraum zu luecken- oder preisarm fuer eine Bewertung ist."""
    from .local_history import read_actual, read_spot
    from .tariff import apply_tariff

    tz = config.general.timezone
    step = pd.Timedelta(minutes=config.general.slot_minutes)
    db = config.e3dc_rscp.history_db_path
    index = pd.date_range(start, end, freq=step, tz=tz, inclusive="left")

    def actual(field):
        try:
            series = read_actual(db, field, start, end, tz)
        except Exception:
            return pd.Series(dtype="float64").reindex(index)
        if series is None or series.empty:
            return pd.Series(np.nan, index=index)
        return series.reindex(index).astype("float64")

    pv = actual("pv_w")
    house = actual("house_w")
    battery = actual("battery_w")
    grid = actual("grid_w")
    soc = actual("soc")
    try:
        spot = read_spot(db, start, end, tz, config.general.slot_minutes)
        price = apply_tariff(spot.reindex(index), config)
    except Exception:
        price = pd.Series(np.nan, index=index)
    feedin = pd.Series(float(config.feed_in.fixed_ct_kwh), index=index)

    # Ohne Preise oder ohne den Grossteil der Ist-Werte ist keine Aussage moeglich.
    covered = int(min(pv.notna().sum(), house.notna().sum(), price.notna().sum()))
    if covered < 0.8 * len(index):
        return None
    return {"index": index, "pv_w": pv.ffill().bfill(),
            "house_w": house.ffill().bfill(),
            "battery_w": battery.ffill().bfill(),
            "grid_w": grid.ffill().bfill(),
            "soc_percent": soc.ffill().bfill(),
            "price_ct_kwh": price.ffill().bfill(), "feedin_ct_kwh": feedin,
            "start": start, "end": end, "dt_hours": config.general.dt_hours}


# --------------------------------------------------------------------------- #
# Timing-Guete
# --------------------------------------------------------------------------- #
def _best_allocation(price, cap_kwh, total_kwh, headroom_kwh, cheapest: bool):
    """Beste Verteilung einer festen Energiemenge als LP.

    price        Preis je Slot (ct/kWh)
    cap_kwh      obere Schranke je Slot (Leistung UND Restlast bzw. Ladeplatz)
    total_kwh    zu verteilende Menge (die real bewegte)
    headroom_kwh kumulative Schranke je Slot (real verfuegbare Energie bzw. Platz)
    cheapest     True = Kosten minimieren (Laden), False = Erloes maximieren
    """
    n = len(price)
    if total_kwh <= 1e-9 or n == 0:
        return None
    prob = pulp.LpProblem("timing", pulp.LpMinimize)
    x = [pulp.LpVariable(f"x_{t}", 0, float(max(0.0, cap_kwh[t]))) for t in range(n)]
    sign = 1.0 if cheapest else -1.0
    prob += pulp.lpSum(sign * float(price[t]) * x[t] for t in range(n))
    prob += pulp.lpSum(x) == float(total_kwh)
    # Kumulative Verfuegbarkeit: bis Slot t darf nicht mehr bewegt werden, als
    # real zur Verfuegung stand - sonst ist die Marke physikalisch unerreichbar.
    if headroom_kwh is not None:
        running = []
        for t in range(n):
            running.append(x[t])
            prob += pulp.lpSum(running) <= float(max(0.0, headroom_kwh[t]))
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if prob.status != pulp.LpStatusOptimal:
        return None
    values = np.array([float(v.value() or 0.0) for v in x])
    moved = values.sum()
    if moved <= 1e-9:
        return None
    return float((np.asarray(price) * values).sum() / moved)


def timing_quality(config, day, data=None) -> Optional[dict]:
    """Wie gut lagen Entladen und Netzladen preislich?

    100 % = die Energie lag genau dort, wo sie unter den realen Schranken am
    meisten wert war. Der Wert ist damit belastbar und nicht - wie eine Marke
    ohne Lastschranke - kuenstlich unerreichbar.

    `data` wird nur zum Testen vorgegeben; normal kommen die Ist-Daten aus
    load_day().
    """
    if data is None:
        data = load_day(config, day)
    if data is None:
        return None
    hb = config.house_battery
    dt = data["dt_hours"]
    price = data["price_ct_kwh"].to_numpy(float)
    pv = data["pv_w"].to_numpy(float)
    house = data["house_w"].to_numpy(float)
    battery = data["battery_w"].to_numpy(float)
    soc = data["soc_percent"].to_numpy(float)

    dis_kwh = np.maximum(0.0, -battery) * dt / 1000.0
    chg_w = np.maximum(0.0, battery)
    charge_kwh = chg_w * dt / 1000.0
    # Netzladen = gleichzeitig laden UND Netz beziehen.
    grid_import_w = np.maximum(0.0, data["grid_w"].to_numpy(float))
    gridcharge_kwh = np.minimum(chg_w, grid_import_w) * dt / 1000.0

    # Schranken je Slot. Die Marke muss das real Geschehene mindestens abbilden
    # koennen - sonst waere sie unloesbar bzw. kleiner als die Realitaet. Die
    # Ist-Signale sind 15-min-Mittel aus verschiedenen Quellen und ueberschreiten
    # die Restlast gelegentlich um wenige Watt (Messrauschen, WR-Sockellast).
    residual_w = np.maximum(0.0, house - pv)            # ohne Akku->Netz
    cap_dis = np.maximum(
        np.minimum(hb.max_discharge_w, residual_w) * dt / 1000.0, dis_kwh)
    cap_chg = np.maximum(
        np.full(len(price), hb.max_total_charge_w * dt / 1000.0), gridcharge_kwh)

    # Kumulative Verfuegbarkeit aus dem realen SoC-Verlauf
    usable_wh = max(1.0, hb.max_soc_wh - hb.min_soc_wh)
    avail0 = max(0.0, (soc[0] / 100.0 * hb.capacity_wh) - hb.min_soc_wh)
    avail = np.maximum((avail0 + np.cumsum(charge_kwh * 1000.0)) / 1000.0,
                       np.cumsum(dis_kwh))
    room0 = max(0.0, hb.max_soc_wh - soc[0] / 100.0 * hb.capacity_wh)
    room = np.maximum((room0 + np.cumsum(dis_kwh * 1000.0)) / 1000.0,
                      np.cumsum(gridcharge_kwh))

    out = {"day": str(pd.Timestamp(data["start"]).date()),
           "slots": int(len(price)),
           "price_min_ct": round(float(np.nanmin(price)), 2),
           "price_max_ct": round(float(np.nanmax(price)), 2)}

    e_dis = float(dis_kwh.sum())
    real_dis = (float((price * dis_kwh).sum() / e_dis) if e_dis > 1e-9 else None)
    best_dis = _best_allocation(price, cap_dis, e_dis, avail, cheapest=False)
    # WICHTIG fuers Lesen der Note: Entladen darf nur die Restlast decken. Deckte
    # der Akku ohnehin fast die ganze Restlast des Tages, gab es beim Zeitpunkt
    # gar keine Wahl - dann ist die Note zwangslaeufig ~100 % und sagt nichts.
    # Erst wenn der Akku die knappe Ressource ist (Winter, nach Tiefentladung),
    # wird sie aussagekraeftig. Der Spielraum wird deshalb mit ausgegeben.
    slack_dis = float(cap_dis.sum() - e_dis)
    out["discharge"] = {
        "energy_kwh": round(e_dis, 2),
        "avg_price_ct": None if real_dis is None else round(real_dis, 2),
        "best_price_ct": None if best_dis is None else round(best_dis, 2),
        "score_percent": (round(100.0 * real_dis / best_dis, 1)
                          if real_dis and best_dis and best_dis > 1e-9 else None),
        "slack_kwh": round(slack_dis, 2),
        "meaningful": bool(slack_dis >= 1.0 and e_dis >= 1.0),
    }

    e_gc = float(gridcharge_kwh.sum())
    real_gc = (float((price * gridcharge_kwh).sum() / e_gc) if e_gc > 1e-9 else None)
    best_gc = _best_allocation(price, cap_chg, e_gc, room, cheapest=True)
    out["grid_charge"] = {
        "energy_kwh": round(e_gc, 3),
        "avg_price_ct": None if real_gc is None else round(real_gc, 2),
        "best_price_ct": None if best_gc is None else round(best_gc, 2),
        # Beim Laden ist guenstiger besser -> Marke/real.
        "score_percent": (round(100.0 * best_gc / real_gc, 1)
                          if real_gc and best_gc and real_gc > 1e-9 else None),
        # Beim Netzladen begrenzt nicht die Ladeleistung, sondern der freie
        # Platz im Akku, wann geladen werden koennte.
        "slack_kwh": round(max(0.0, float(room[-1]) - e_gc), 2),
        # Unter 0.2 kWh ist der Wert Rauschen und wird nicht bewertet.
        "meaningful": bool(e_gc >= 0.2),
    }
    out["usable_capacity_kwh"] = round(usable_wh / 1000.0, 2)
    return out


# --------------------------------------------------------------------------- #
# Fixplan-Simulator
# --------------------------------------------------------------------------- #
def simulate_schedule(config, data, battery_w, soc0_wh=None) -> dict:
    """Einen fixierten Akku-Fahrplan auf den Ist-Daten abspielen.

    battery_w: gewuenschte Akkuleistung je Slot (+ laden, - entladen). Die Werte
    werden physikalisch begrenzt (Leistung, SoC-Grenzen, Wechselrichter) - ein
    Plan, der auf den Ist-Daten nicht durchfuehrbar ist, wird also nicht
    schoengerechnet. Rueckgabe: Kosten und Energiebilanz.
    """
    hb = config.house_battery
    dt = data["dt_hours"]
    pv = data["pv_w"].to_numpy(float)
    house = data["house_w"].to_numpy(float)
    price = data["price_ct_kwh"].to_numpy(float)
    feedin = data["feedin_ct_kwh"].to_numpy(float)
    want = np.asarray(battery_w, dtype=float)
    n = len(pv)
    if len(want) < n:
        want = np.concatenate([want, np.zeros(n - len(want))])
    want = np.nan_to_num(want[:n])

    soc = (float(soc0_wh) if soc0_wh is not None
           else float(data["soc_percent"].iloc[0]) / 100.0 * hb.capacity_wh)
    soc = min(hb.max_soc_wh, max(hb.min_soc_wh, soc))
    max_export = config.inverter.max_export_w
    cost_ct = 0.0
    imp_kwh = exp_kwh = chg_kwh = dis_kwh = 0.0
    for t in range(n):
        target = want[t]
        if target >= 0.0:                              # laden
            room_w = (hb.max_soc_wh - soc) / max(1e-9, hb.charge_efficiency * dt)
            charge = min(target, hb.max_total_charge_w, max(0.0, room_w))
            dis = 0.0
            soc += hb.charge_efficiency * charge * dt
        else:                                          # entladen
            avail_w = (soc - hb.min_soc_wh) * hb.discharge_efficiency / max(1e-9, dt)
            dis = min(-target, hb.max_discharge_w, max(0.0, avail_w))
            charge = 0.0
            soc -= dis * dt / max(1e-9, hb.discharge_efficiency)
        # Netzbilanz des Slots
        net = house[t] + charge - pv[t] - dis
        imp = max(0.0, net)
        exp = max(0.0, -net)
        if max_export is not None:
            exp = min(exp, float(max_export))           # Rest wird abgeregelt
        cost_ct += (imp * price[t] - exp * feedin[t]) * dt / 1000.0
        imp_kwh += imp * dt / 1000.0
        exp_kwh += exp * dt / 1000.0
        chg_kwh += charge * dt / 1000.0
        dis_kwh += dis * dt / 1000.0
    return {"cost_eur": round(cost_ct / 100.0, 3),
            "grid_import_kwh": round(imp_kwh, 2),
            "grid_export_kwh": round(exp_kwh, 2),
            "battery_charge_kwh": round(chg_kwh, 2),
            "battery_discharge_kwh": round(dis_kwh, 2),
            "end_soc_wh": round(soc, 1),
            "end_soc_percent": round(100.0 * soc / hb.capacity_wh, 1)}


def _metered_cost(data) -> Optional[dict]:
    """Real abgerechnete Netzkosten des Tages aus den Ist-Werten."""
    grid = data.get("grid_w")
    if grid is None:
        return None
    dt = data["dt_hours"]
    g = np.asarray(grid, dtype=float)
    price = data["price_ct_kwh"].to_numpy(float)
    feedin = data["feedin_ct_kwh"].to_numpy(float)
    imp = np.maximum(0.0, g)
    exp = np.maximum(0.0, -g)
    cost_ct = float(((imp * price - exp * feedin) * dt / 1000.0).sum())
    return {"cost_eur": round(cost_ct / 100.0, 3),
            "grid_import_kwh": round(float((imp * dt / 1000.0).sum()), 2),
            "grid_export_kwh": round(float((exp * dt / 1000.0).sum()), 2)}


# --------------------------------------------------------------------------- #
# Regret-Analyse
# --------------------------------------------------------------------------- #
def _solve_battery_plan(config, index, pv_w, house_w, price_ct, soc0_wh,
                        pv10_w=None):
    """Optimierer auf gegebenen Eingaengen laufen lassen und den Akku-Fahrplan
    (+ laden, - entladen) je Slot zurueckgeben."""
    import copy

    from .optimizer import Optimizer, OptimizerInputs

    cfg = copy.deepcopy(config)
    cfg.controllable_loads = []      # Lasten stecken schon in der Ist-Hauslast
    n = len(index)
    inp = OptimizerInputs(
        index=index,
        house_load_w=np.asarray(house_w, dtype=float),
        pv_w=np.asarray(pv_w, dtype=float),
        price_ct_kwh=np.asarray(price_ct, dtype=float),
        feedin_ct_kwh=np.full(n, float(cfg.feed_in.fixed_ct_kwh)),
        initial_house_soc_wh=float(soc0_wh),
        pv10_w=(np.asarray(pv10_w, dtype=float) if pv10_w is not None else None),
        spot_price_ct_kwh=np.asarray(price_ct, dtype=float))
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False,
                    diagnose_infeasible=False).solve(inp)
    if res.infeasible:
        return None
    table = res.table
    charge = (table["batt_dc_charge_w"].to_numpy(float)
              + table["batt_ac_charge_w"].to_numpy(float))
    return charge - table["batt_discharge_w"].to_numpy(float)


def regret_analysis(config, day) -> Optional[dict]:
    """Was hat die Prognoseunsicherheit an diesem Tag gekostet?

    Vier Kosten, ALLE mit demselben Simulator auf den echten Ist-Daten bewertet -
    nur so sind sie vergleichbar:

      hellsicht   Optimierer mit den tatsaechlich eingetretenen Werten
      fix         Plan aus der 00:00-Prognose, danach nicht mehr angepasst
      rollierend  die je Slot wirklich publizierten Sollwerte
      abgerechnet aus den Ist-Zaehlerfluessen (Quervergleich)

    Der Tag muss abgeschlossen sein und ein Folgetag vorliegen: der Optimierer
    bekommt 48 h Ist-Daten, damit der Terminalwert des Akkus nicht in den
    Bewertungstag hineinwirkt. Bewertet wird nur der Tag selbst.
    """
    from .local_history import (read_execution_plan_range,
                                read_optimizer_forecast_asof)

    tz = config.general.timezone
    hb = config.house_battery
    start, end = _day_bounds(config, day)
    # Der Folgetag muss abgeschlossen sein: an einem laufenden Tag wuerde
    # load_range die fehlenden Stunden per ffill fortschreiben und der Optimierer
    # bekaeme erfundene Werte als "Ist-Daten".
    if end + pd.Timedelta(days=1) > pd.Timestamp.now(tz=tz):
        return {"day": str(start.date()),
                "reason": "Folgetag noch nicht abgeschlossen"}
    day_data = load_range(config, start, end)
    long_data = load_range(config, start, end + pd.Timedelta(days=1))
    if day_data is None:
        return {"day": str(start.date()),
                "reason": "keine ausreichenden Ist-Daten für diesen Tag"}
    if long_data is None:
        # Der Optimierer braucht den Folgetag, sonst wuerde er den Akku am
        # Tagesende leerfahren und die Hellsicht-Kosten schoenrechnen.
        return {"day": str(start.date()),
                "reason": "Folgetag fehlt (Tag muss abgeschlossen sein)"}
    n_day = len(day_data["index"])
    soc0_wh = float(day_data["soc_percent"].iloc[0]) / 100.0 * hb.capacity_wh
    soc0_wh = min(hb.max_soc_wh, max(hb.min_soc_wh, soc0_wh))

    out = {"day": str(start.date()), "slots": n_day,
           "initial_soc_percent": round(float(day_data["soc_percent"].iloc[0]), 1)}

    def sim(plan):
        if plan is None:
            return None
        return simulate_schedule(config, day_data, plan[:n_day], soc0_wh)

    # 1) Hellsicht: Optimierer auf den eingetretenen Werten (48 h Horizont)
    oracle_plan = _solve_battery_plan(
        config, long_data["index"], long_data["pv_w"], long_data["house_w"],
        long_data["price_ct_kwh"], soc0_wh)
    out["oracle"] = sim(oracle_plan)

    # 2) Fixierter Plan aus der 00:00-Prognose
    frozen = None
    try:
        issue, frame = read_optimizer_forecast_asof(
            config.e3dc_rscp.history_db_path, start + pd.Timedelta(minutes=20),
            start, end + pd.Timedelta(days=1), tz)
        if frame is not None and not frame.empty:
            frame = frame.sort_index()
            need = ("house_load_w", "pv_w", "price_ct_kwh")
            if all(c in frame.columns for c in need) and len(frame) >= n_day:
                frozen = _solve_battery_plan(
                    config, frame.index, frame["pv_w"], frame["house_load_w"],
                    frame["price_ct_kwh"], soc0_wh,
                    pv10_w=(frame["pv10_w"] if "pv10_w" in frame else None))
                out["forecast_issued_at"] = str(issue)
    except Exception as exc:                                # pragma: no cover
        log.debug("00:00-Prognose nicht rekonstruierbar (%s).", exc)
    out["frozen"] = sim(frozen)

    # 3) Rollierend: die je Slot publizierten Sollwerte
    rolling = None
    try:
        plan = read_execution_plan_range(
            config.e3dc_rscp.history_db_path, start, end, tz)
        if plan is not None and not plan.empty and "planned_battery_w" in plan:
            series = plan["planned_battery_w"].reindex(day_data["index"])
            if series.notna().sum() >= 0.8 * n_day:
                rolling = series.ffill().bfill().to_numpy(float)
    except Exception as exc:                                # pragma: no cover
        log.debug("Rollierender Plan nicht lesbar (%s).", exc)
    out["rolling"] = sim(rolling)
    out["metered"] = _metered_cost(day_data)

    # Terminalwert des Akkus: die vier Varianten enden mit unterschiedlichem
    # Ladestand. Unbewertet waere ein Plan, der den Akku leerfaehrt, scheinbar
    # guenstiger - und "hellsicht" damit keine untere Schranke mehr (der Wert
    # kann sonst negativ werden). Bewertet wird die Differenz zum Ist-Endstand
    # mit dem Mittelpreis des Folgetages, an dem die Energie eingesetzt wird,
    # abzueglich Entladeverlust.
    next_price = long_data["price_ct_kwh"].to_numpy(float)[n_day:]
    ref_price_ct = float(np.mean(next_price)) if len(next_price) else float(
        np.mean(day_data["price_ct_kwh"].to_numpy(float)))
    end_soc_percent = float(day_data["soc_percent"].iloc[-1])
    soc_ref_wh = end_soc_percent / 100.0 * hb.capacity_wh
    out["soc_value_ct_kwh"] = round(ref_price_ct, 2)
    out["reference_end_soc_percent"] = round(end_soc_percent, 1)

    def cost(key):
        """Kosten der Variante, auf den Ist-Endladestand normiert."""
        item = out.get(key)
        if not item:
            return None
        # "abgerechnet" endet per Definition beim Ist-Stand -> Korrektur 0.
        end_wh = float(item.get("end_soc_wh", soc_ref_wh))
        adj = ((soc_ref_wh - end_wh) / 1000.0 * ref_price_ct / 100.0
               * hb.discharge_efficiency)
        item["soc_adjustment_eur"] = round(adj, 3)
        item["cost_adjusted_eur"] = round(float(item["cost_eur"]) + adj, 3)
        return float(item["cost_adjusted_eur"])

    c_or, c_fr, c_ro, c_me = (cost("oracle"), cost("frozen"),
                              cost("rolling"), cost("metered"))
    delta = {}
    if c_fr is not None and c_or is not None:
        delta["forecast_regret_eur"] = round(c_fr - c_or, 3)
    if c_fr is not None and c_ro is not None:
        delta["replanning_gain_eur"] = round(c_fr - c_ro, 3)
    if c_me is not None and c_ro is not None:
        # Enthaelt neben der Ausfuehrung auch Messinkonsistenzen der Ist-Werte.
        delta["execution_and_metering_eur"] = round(c_me - c_ro, 3)
    if c_me is not None and c_or is not None:
        delta["total_gap_eur"] = round(c_me - c_or, 3)
    out["delta"] = delta
    return out


# --------------------------------------------------------------------------- #
# Zusammenfassung fuer Dashboard/CLI
# --------------------------------------------------------------------------- #
_SUMMARY_CACHE: dict = {}
_SUMMARY_TTL_S = 6 * 3600


def _completed_days(config, count: int) -> list:
    """Die letzten `count` abgeschlossenen Tage (der Vortag ist der jüngste:
    der Regret braucht den Folgetag als Solver-Horizont)."""
    today = pd.Timestamp.now(tz=config.general.timezone).normalize()
    return [str((today - pd.Timedelta(days=k)).date())
            for k in range(count, 0, -1)]


def plan_value_summary(config, timing_days: int = 7, regret_days: int = 3,
                       use_cache: bool = True) -> dict:
    """Kennzahlen für das Analyse-Panel: Timing-Güte und Prognose-Regret.

    Die Timing-Güte ist reines Nachrechnen der Ist-Daten (Millisekunden), der
    Regret kostet je Tag zwei Solverläufe (~2-3 s) - deshalb nur wenige Tage
    und ein Prozess-Cache mit 6 h Haltbarkeit.
    """
    import time

    # Der Schlüssel enthält die Datenquelle: sonst würde eine zweite Anlage
    # (oder ein Test) das Ergebnis der ersten sehen.
    key = (config.e3dc_rscp.history_db_path, timing_days, regret_days)
    now = time.monotonic()
    if use_cache:
        hit = _SUMMARY_CACHE.get(key)
        if hit and now - hit[0] < _SUMMARY_TTL_S:
            return hit[1]

    out: dict = {"generated": str(pd.Timestamp.now(tz=config.general.timezone)),
                 "timing": [], "regret": []}
    for day in _completed_days(config, timing_days):
        try:
            item = timing_quality(config, day)
        except Exception as exc:                            # pragma: no cover
            log.debug("Timing-Güte %s fehlgeschlagen (%s).", day, exc)
            item = None
        if item:
            out["timing"].append(item)
    for day in _completed_days(config, regret_days):
        try:
            item = regret_analysis(config, day)
        except Exception as exc:                            # pragma: no cover
            log.debug("Regret %s fehlgeschlagen (%s).", day, exc)
            item = None
        if item and not item.get("reason"):
            out["regret"].append(item)

    # Mittelwerte über die bewertbaren Tage. Beim Timing zählen nur Tage mit
    # echtem Spielraum - sonst wäre die Note trivial ~100 % (Sommer: der Akku
    # deckt jede Restlast sofort, es gab gar keine Wahl).
    scored = [t["discharge"]["score_percent"] for t in out["timing"]
              if t["discharge"].get("meaningful")
              and t["discharge"].get("score_percent") is not None]
    out["discharge_score_percent"] = (round(sum(scored) / len(scored), 1)
                                      if scored else None)
    out["discharge_scored_days"] = len(scored)
    out["timing_days"] = len(out["timing"])
    gc = [t["grid_charge"]["score_percent"] for t in out["timing"]
          if t["grid_charge"].get("meaningful")
          and t["grid_charge"].get("score_percent") is not None]
    out["grid_charge_score_percent"] = (round(sum(gc) / len(gc), 1)
                                        if gc else None)
    out["grid_charge_scored_days"] = len(gc)

    keys = ("forecast_regret_eur", "replanning_gain_eur",
            "execution_and_metering_eur", "total_gap_eur")
    full = [r for r in out["regret"]
            if all(r.get("delta", {}).get(k) is not None for k in keys)]
    out["regret_days"] = len(full)
    if full:
        for k in keys:
            out[f"avg_{k}"] = round(
                sum(float(r["delta"][k]) for r in full) / len(full), 3)
    if use_cache:
        _SUMMARY_CACHE[key] = (now, out)
    return out
