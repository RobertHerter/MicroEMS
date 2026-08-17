#!/usr/bin/env python3
"""Was hat der Pool in der letzten Nacht wirklich getan?

Prueft die Horizont-Regel (no_grid_import, seit 81d7c79) am echten Betrieb statt
am Plan: heizt der Pool nur aus Ueberschuss, wie tief geht die Temperatur, und
entsteht dabei Netzbezug?

    python tools/nacht_pool_check.py --config config.yaml [--nacht 2026-08-18]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import pandas as pd

from ems.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--nacht", default=None,
                    help="Morgen-Datum der Nacht (YYYY-MM-DD), Standard heute")
    args = ap.parse_args()
    cfg = load_config(args.config)
    tz = cfg.general.timezone
    tag = pd.Timestamp(args.nacht, tz=tz) if args.nacht else \
        pd.Timestamp.now(tz=tz).normalize()
    von, bis = tag - pd.Timedelta(hours=4), tag + pd.Timedelta(hours=9)

    con = sqlite3.connect(cfg.e3dc_rscp.history_db_path)
    live = pd.read_sql(
        "SELECT ts, pv_w, house_w, grid_w, battery_w FROM live_samples "
        "WHERE ts >= ? AND ts < ? ORDER BY ts", con,
        params=[von.tz_convert("UTC").isoformat(), bis.tz_convert("UTC").isoformat()])
    temp = pd.read_sql(
        "SELECT ts, temp_c FROM load_temp WHERE name='Pool' AND ts >= ? AND ts < ? "
        "ORDER BY ts", con,
        params=[von.tz_convert("UTC").isoformat(), bis.tz_convert("UTC").isoformat()])
    stufen = pd.read_sql(
        "SELECT ts, stage, commanded_on FROM load_stage_cmd WHERE name='Pool' "
        "AND ts >= ? AND ts < ? ORDER BY ts", con,
        params=[von.tz_convert("UTC").isoformat(), bis.tz_convert("UTC").isoformat()])
    con.close()

    for frame in (live, temp, stufen):
        if not frame.empty:
            frame["ts"] = pd.to_datetime(
                frame.ts, format="ISO8601", utc=True).dt.tz_convert(tz)

    print(f"Nacht auf {tag:%d.%m.%Y} ({von:%H:%M} bis {bis:%H:%M})\n")
    if stufen.empty:
        print("  keine Pool-Schaltbefehle protokolliert")
    else:
        an = stufen[stufen.commanded_on == 1]
        slots = sorted(set(an.ts))
        print(f"  Pool war in {len(slots)} Slots freigegeben")
        if slots:
            print(f"    von {min(slots):%d.%m %H:%M} bis {max(slots):%d.%m %H:%M}")
    if not temp.empty:
        print(f"  Temperatur: {temp.temp_c.min():.1f} bis {temp.temp_c.max():.1f} C "
              f"(Heizgrenze {cfg.controllable_loads[0].min_c if cfg.controllable_loads else '?'})")
    if live.empty:
        print("  keine Livewerte")
        return 0
    live = live.set_index("ts")
    nacht = live.loc[f"{(tag - pd.Timedelta(days=1)).date()} 21:00":f"{tag.date()} 07:00"]
    # Kernfrage: lief der Pool, waehrend das Netz geliefert hat?
    bezug = nacht[nacht.grid_w > 50.0]
    print(f"\n  Nacht 21-07: Netzbezug in {len(bezug)} von {len(nacht)} Proben")
    if len(nacht):
        print(f"    Bezug im Mittel {nacht.grid_w.clip(lower=0).mean():6.0f} W, "
              f"Maximum {nacht.grid_w.max():6.0f} W")
        print(f"    Akku von {nacht.battery_w.iloc[0]:+.0f} W bis "
              f"{nacht.battery_w.iloc[-1]:+.0f} W")
    morgen = live.loc[f"{tag.date()} 04:00":f"{tag.date()} 09:00"]
    if len(morgen):
        print(f"  Morgen 04-09: Netzbezug im Mittel "
              f"{morgen.grid_w.clip(lower=0).mean():6.0f} W")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
