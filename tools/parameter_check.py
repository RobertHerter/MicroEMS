#!/usr/bin/env python3
"""Einen Optimierer-Parameter durchmessen - richtig verglichen.

Drei Fallen, in die ein Parametervergleich von Hand zuverlaessig laeuft; dieses
Skript umgeht alle drei:

1. **Terminalwert.** ``total_cost_ct`` ist die reine Energiekostensumme. Der
   Wert des Akkuinhalts am Horizontende steckt in der Zielfunktion, aber NICHT
   in dieser Zahl. Sobald zwei Varianten unterschiedlich voll enden, ist der
   Vergleich auf den Kosten allein ungueltig - bezahlte Energie im Akku sieht
   dort wie Ersparnis aus und kann die Ordnung umkehren (gemessen: 18,5 ct
   "Vorteil" fuer die teurere Variante). Darum wird ``netto = Kosten -
   Terminalwert`` berichtet, mit der Formel aus ``ems/optimizer.py``.
2. **Warmstart.** ``Optimizer(cfg)`` legt seine Loesung als Warmstart ab; die
   naechste Variante startet dann dort. Hier immer ``store_warm=False,
   stabilize_plan=False``.
3. **Abbruchtoleranz.** Mit ``gapRel``/``gapAbs`` haengt es am Suchpfad, an
   welcher zulaessigen Loesung abgebrochen wird. Standard ist deshalb exakt
   (beide 0); Laeufe, die ins Zeitlimit rennen, sind als LIMIT markiert und
   NICHT vergleichbar.

Zwei Betriebsarten:

``--modus modell``  bewertet die Plaene auf den Prognosedaten des Snapshots.
    Beantwortet "was haelt der Optimierer fuer besser".
``--modus ist``     rechnet den 00:00-Plan jedes Tages auf den ECHTEN Ist-Daten
    ab (``ems/planvalue.simulate_schedule``, physikalisch begrenzt). Das ist die
    entscheidende Messung fuer Parameter, die gegen Prognosefehler versichern -
    im Modell ist die Prognose wahr, dort kann eine Versicherung nur kosten.

    python tools/parameter_check.py \\
        --parameter optimization.battery_hold_penalty_ct_kwh --werte 5 2 0
    python tools/parameter_check.py --modus ist --tage 5 \\
        --parameter optimization.battery_hold_penalty_ct_kwh --werte 5 0
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
import time
import zlib

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ems.config import load_config                            # noqa: E402
from ems.optimizer import (Optimizer, OptimizerInputs,        # noqa: E402
                           terminal_credit_ct)

FELDER = ("house_load_w", "pv_w", "price_ct_kwh", "feedin_ct_kwh", "pv10_w",
          "ambient_temp_c", "solar_w_m2")
DURCHREICHEN = ("load_state", "load_feedback", "car_present",
                "initial_car_soc_wh")


def _payload(con, snapshot_id):
    row = con.execute("SELECT payload, generated FROM debug_snapshot "
                      "WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        return None, None
    rohdaten, erzeugt = row
    try:
        return json.loads(zlib.decompress(rohdaten)), erzeugt
    except Exception:
        return json.loads(rohdaten), erzeugt


def _eingaben(daten) -> OptimizerInputs:
    i = daten["inputs"]
    kw = {"index": pd.DatetimeIndex(pd.to_datetime(i["index"],
                                                   format="ISO8601")),
          "initial_house_soc_wh": float(i["initial_house_soc_wh"])}
    for feld in FELDER:
        if i.get(feld) is not None:
            kw[feld] = np.asarray(i[feld], dtype=float)
    for feld in DURCHREICHEN:
        if i.get(feld) is not None:
            kw[feld] = i[feld]
    return OptimizerInputs(**kw)


def _setze(cfg, pfad: str, wert) -> None:
    """``optimization.battery_hold_penalty_ct_kwh`` = 5.0 setzen."""
    ziel = cfg
    teile = pfad.split(".")
    for teil in teile[:-1]:
        ziel = getattr(ziel, teil)
    name = teile[-1]
    if not hasattr(ziel, name):
        raise SystemExit(f"unbekannter Parameter: {pfad}")
    alt = getattr(ziel, name)
    if isinstance(alt, bool):
        neu = str(wert).lower() in ("1", "true", "ja", "yes")
    elif isinstance(alt, int) and not isinstance(alt, bool):
        neu = int(float(wert))
    elif isinstance(alt, float) or alt is None:
        neu = float(wert)
    else:
        neu = wert                          # Strings wie terminal_soc_value
        try:
            neu = float(wert)
        except (TypeError, ValueError):
            pass
    setattr(ziel, name, neu)


def _variante(basis, pfad, wert, zeitlimit, exakt):
    cfg = copy.deepcopy(basis)
    cfg.optimization.solver_time_limit_s = zeitlimit
    if exakt:
        cfg.optimization.solver_mip_gap = 0.0
        cfg.optimization.solver_mip_gap_abs_ct = 0.0
    _setze(cfg, pfad, wert)
    return cfg


def _loese(cfg, inp):
    t0 = time.time()
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
    return res, time.time() - t0


def _snapshots(con, anzahl, muster):
    sql = "SELECT id, generated FROM debug_snapshot"
    args = ()
    if muster:
        sql += " WHERE generated LIKE ?"
        args = (muster,)
    sql += " ORDER BY id DESC LIMIT ?"
    return list(reversed(con.execute(sql, args + (anzahl,)).fetchall()))


def modus_modell(basis, con, args) -> int:
    laeufe = _snapshots(con, args.snapshots, args.muster)
    if not laeufe:
        print("keine Snapshots gefunden")
        return 1
    print(f"{args.parameter}, bewertet auf den PROGNOSEDATEN")
    print(f"{len(laeufe)} Laeufe, {'exakt' if args.exakt else 'mit Toleranz'}, "
          f"ohne Warmstart, Zeitlimit {args.zeitlimit} s\n")
    kopf = f"{'Lauf':13s}"
    for wert in args.werte:
        kopf += f"{str(wert):>25s}"
    print(kopf)
    print(f"{'':13s}" + "".join(f"{'Kosten':>8s}{'-TW':>8s}{'=netto':>9s}"
                                for _ in args.werte))
    summe = {w: 0.0 for w in args.werte}
    for sid, erzeugt in laeufe:
        daten, _ = _payload(con, sid)
        if daten is None or not daten.get("inputs"):
            continue
        inp = _eingaben(daten)
        ts = pd.Timestamp(erzeugt).tz_convert(basis.general.timezone)
        zeile, marke = f"  {ts:%d.%m %H:%M}", ""
        for wert in args.werte:
            cfg = _variante(basis, args.parameter, wert, args.zeitlimit,
                            args.exakt)
            res, _ = _loese(cfg, inp)
            if res.infeasible:
                zeile += f"{'unloesbar':>25s}"
                continue
            if res.solver_hit_limit:
                marke = "  LIMIT"
            soc_wh = float(res.table["house_soc_wh"].iloc[-1])
            tw = terminal_credit_ct(cfg, inp.price_ct_kwh, inp.feedin_ct_kwh,
                                    soc_wh)
            netto = res.total_cost_ct - tw
            summe[wert] += netto
            zeile += (f"{res.total_cost_ct / 100:8.3f}{tw / 100:8.3f}"
                      f"{netto / 100:9.3f}")
        print(zeile + marke, flush=True)
    print(f"\n  {'Summe netto':11s}" + "".join(
        f"{'':16s}{summe[w] / 100:9.3f}" for w in args.werte))
    _fazit(args, summe)
    return 0


def modus_ist(basis, con, args) -> int:
    from ems.planvalue import load_day, simulate_schedule
    heute = pd.Timestamp.now(tz=basis.general.timezone).normalize()
    tage = [(heute - pd.Timedelta(days=n)).date()
            for n in range(args.tage, 0, -1)]
    print(f"{args.parameter}, 00:00-Plan abgerechnet auf den IST-DATEN")
    print("der Fahrplan wird physikalisch begrenzt - ein auf den Ist-Daten "
          "undurchfuehrbarer\nPlan wird also nicht schoengerechnet\n")
    kopf = f"{'Tag':12s}"
    for wert in args.werte:
        kopf += f"{str(wert):>25s}"
    print(kopf)
    print(f"{'':12s}" + "".join(f"{'Kosten':>8s}{'EndSoC':>8s}{'netto':>9s}"
                                for _ in args.werte))
    summe = {w: 0.0 for w in args.werte}
    for tag in tage:
        sid = con.execute("SELECT id FROM debug_snapshot WHERE generated "
                          "LIKE ? ORDER BY id LIMIT 1",
                          (f"{tag.isoformat()}T00%",)).fetchone()
        ist = load_day(basis, tag)
        if not sid or ist is None:
            print(f"  {tag}  uebersprungen (Snapshot {bool(sid)}, "
                  f"Ist-Daten {ist is not None})")
            continue
        daten, _ = _payload(con, sid[0])
        if daten is None or not daten.get("inputs"):
            continue
        inp = _eingaben(daten)
        kap = basis.house_battery.capacity_wh
        soc0 = float(ist["soc_percent"].iloc[0]) / 100.0 * kap
        zeile, marke = f"  {tag}", ""
        for wert in args.werte:
            cfg = _variante(basis, args.parameter, wert, args.zeitlimit,
                            args.exakt)
            res, _ = _loese(cfg, inp)
            if res.infeasible:
                zeile += f"{'unloesbar':>25s}"
                continue
            if res.solver_hit_limit:
                marke = "  LIMIT"
            t = res.table.reindex(ist["index"])      # nur die Slots des Tages
            fahrplan = (t["batt_dc_charge_w"].fillna(0.0)
                        + t["batt_ac_charge_w"].fillna(0.0)
                        - t["batt_discharge_w"].fillna(0.0)).to_numpy(float)
            sim = simulate_schedule(basis, ist, fahrplan, soc0_wh=soc0)
            tw = terminal_credit_ct(cfg, ist["price_ct_kwh"],
                                    ist["feedin_ct_kwh"], sim["end_soc_wh"])
            netto = sim["cost_eur"] * 100.0 - tw
            summe[wert] += netto
            zeile += (f"{sim['cost_eur']:8.3f}{sim['end_soc_percent']:7.1f}%"
                      f"{netto / 100:9.3f}")
        print(zeile + marke, flush=True)
    print(f"\n  {'Summe netto':10s}" + "".join(
        f"{'':16s}{summe[w] / 100:9.3f}" for w in args.werte))
    _fazit(args, summe)
    return 0


def _fazit(args, summe) -> None:
    gemessen = {w: v for w, v in summe.items() if v}
    if len(gemessen) < 2:
        return
    besser = min(gemessen, key=gemessen.get)
    print(f"\n  guenstigster Wert: {args.parameter} = {besser}")
    for wert in args.werte:
        if wert in gemessen and wert != besser:
            print(f"    gegen {wert}: "
                  f"{(gemessen[wert] - gemessen[besser]) / 100:+.3f} EUR")
    print("  Kleine Differenzen sind kein Ergebnis: bindet der Parameter an "
          "den meisten\n  Tagen gar nicht, sind die Plaene identisch und die "
          "Stichprobe ist kleiner\n  als die Zahl der Zeilen.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--parameter", required=True,
                   help="Pfad wie optimization.battery_hold_penalty_ct_kwh")
    p.add_argument("--werte", nargs="+", required=True,
                   help="zu vergleichende Werte")
    p.add_argument("--modus", choices=("modell", "ist"), default="modell")
    p.add_argument("--snapshots", type=int, default=8,
                   help="Anzahl der jüngsten Snapshots (modus modell)")
    p.add_argument("--muster", default=None,
                   help="LIKE-Muster auf generated, z.B. '2026-08-1_T14%%'")
    p.add_argument("--tage", type=int, default=5,
                   help="Anzahl abgeschlossener Tage (modus ist)")
    p.add_argument("--zeitlimit", type=int, default=900)
    p.add_argument("--mit-toleranz", dest="exakt", action="store_false",
                   help="die konfigurierten MIP-Toleranzen behalten "
                        "(schneller, aber suchpfadabhängig)")
    args = p.parse_args()
    args.werte = [float(w) if w.replace(".", "", 1).lstrip("-").isdigit()
                  else w for w in args.werte]

    basis = load_config(args.config)
    con = sqlite3.connect(basis.e3dc_rscp.history_db_path)
    try:
        if args.modus == "ist":
            return modus_ist(basis, con, args)
        return modus_modell(basis, con, args)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
