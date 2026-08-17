#!/usr/bin/env python3
"""Komfort-Malus der thermischen Last durchmessen.

Der Malus (``comfort_penalty_ct_per_k_slot``) entscheidet, ob Komfort oder
Strompreis gewinnt. Die Groessenordnung, auf die es ankommt, ist die
Einspeiseverguetung: Heizen aus PV kostet immer den entgangenen Erlös (hier
~7,8 ct/kWh). Unter diesem Wert heizt der Speicher NIE, darueber genau dann,
wenn Ueberschuss da ist - nachts stehen 33-47 ct Bezugspreis dagegen.

    python tools/pool_komfort_sweep.py --config config.yaml --werte 8 15 22 30

Jeder Lauf wird mit grosszuegigem Zeitlimit gerechnet; Laeufe, die es reissen,
sind als LIMIT markiert und NICHT vergleichbar (Plaene aus abgebrochenen Laeufen
unterscheiden sich durch den Suchpfad, nicht durch das Optimum).
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

from ems.config import load_config          # noqa: E402
from ems.optimizer import Optimizer, OptimizerInputs   # noqa: E402

FELDER = ("house_load_w", "pv_w", "price_ct_kwh", "feedin_ct_kwh", "pv10_w",
          "ambient_temp_c", "solar_w_m2")


def lade_eingaben(db: str, snapshot_id=None):
    con = sqlite3.connect(db)
    if snapshot_id:
        row = con.execute("SELECT payload, generated FROM debug_snapshot "
                          "WHERE id=?", (snapshot_id,)).fetchone()
    else:
        row = con.execute("SELECT payload, generated FROM debug_snapshot "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    if not row:
        raise SystemExit("kein Schnappschuss vorhanden")
    try:
        d = json.loads(zlib.decompress(row[0]))
    except Exception:
        d = json.loads(row[0])
    i = d["inputs"]
    idx = pd.DatetimeIndex(pd.to_datetime(i["index"], format="ISO8601"))
    kw = {"index": idx,
          "initial_house_soc_wh": float(i["initial_house_soc_wh"])}
    for f in FELDER:
        if i.get(f) is not None:
            kw[f] = np.asarray(i[f], float)
    for f in ("load_state", "load_feedback", "car_present", "initial_car_soc_wh"):
        if i.get(f) is not None:
            kw[f] = i[f]
    return OptimizerInputs(**kw), row[1], i.get("load_state")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--werte", nargs="+", type=float,
                    default=[0.0, 8.0, 15.0, 22.0, 30.0])
    ap.add_argument("--snapshot", type=int, default=None)
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    basis = load_config(args.config)
    inp, gen, state = lade_eingaben(basis.e3dc_rscp.history_db_path, args.snapshot)
    tz = basis.general.timezone
    lokal = inp.index.tz_convert(tz)
    verg = float(np.mean(inp.feedin_ct_kwh))
    print(f"Grundlage: Lauf {pd.Timestamp(gen).tz_convert(tz):%d.%m %H:%M}, "
          f"{len(lokal)} Slots, Start {state}")
    print(f"Einspeiseverguetung im Mittel {verg:.2f} ct/kWh - darunter lohnt "
          f"Heizen aus PV nie.\n")
    print(f"{'Malus':>7s} {'Kosten':>9s} {'Pool ges':>10s} {'Nacht':>9s} "
          f"{'Tag':>9s} {'Temp-Min':>9s} {'Bezug':>9s} {'Dauer':>7s}")
    nacht_von, nacht_bis = 21, 7
    for pen in args.werte:
        cfg = copy.deepcopy(basis)
        cfg.optimization.solver_time_limit_s = args.limit
        for ld in cfg.controllable_loads:
            if ld.type == "thermal":
                ld.comfort_penalty_ct_per_k_slot = pen
        t0 = time.time()
        res = Optimizer(cfg).solve(inp)
        dauer = time.time() - t0
        if res.infeasible:
            print(f"  {pen:5.1f}   unloesbar: {res.infeasible_reason}")
            continue
        t = res.table.copy()
        t.index = lokal
        spalten = [c for c in t.columns
                   if c.startswith("load_") and c.endswith("_w")
                   and not c.endswith("_grid_w") and "temp" not in c]
        pool = t[spalten].sum(axis=1) if spalten else pd.Series(0.0, index=t.index)
        ist_nacht = (t.index.hour >= nacht_von) | (t.index.hour < nacht_bis)
        temp = [c for c in t.columns if c.endswith("_temp_c")]
        tmin = float(t[temp[0]].min()) if temp else float("nan")
        print(f"  {pen:5.1f} {res.total_cost_ct / 100:8.3f} "
              f"{float(pool.sum()) * 0.25 / 1000:7.2f} kWh "
              f"{float(pool[ist_nacht].sum()) * 0.25 / 1000:6.2f} kWh "
              f"{float(pool[~ist_nacht].sum()) * 0.25 / 1000:6.2f} kWh "
              f"{tmin:8.2f} C "
              f"{float(t['grid_import_w'].sum()) * 0.25 / 1000:6.2f} kWh "
              f"{dauer:6.0f} s{'  LIMIT' if res.solver_hit_limit else ''}",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
