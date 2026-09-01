#!/usr/bin/env python3
"""Steht der Akku bei Produktionsbeginn am Mindest-SoC?

Roberts Pruefkriterium (01.09.2026): laeuft es richtig, muss der Akku bei
Beginn der Solarproduktion am Mindest-SoC stehen, WENN der Tag genug PV bringt
und der Pool Waerme braucht. Energie, die er in einen sonnigen Tag hinein
mitschleppt, verdraengt nichts - der Tag fuellt ihn ohnehin und speist den Rest
ein.

Gemessen ueber 20.-31.08.2026 war das nie erfuellt: Pool 22-27,8 C (also unter
der Grenze) und Akku mit 6 bis 36 % am Morgen, an vier Tagen 1,7 bis 4,8 kWh
ungenutzt bei 50-81 kWh PV am selben Tag. Ursache war die Bewertung
gespeicherter Energie gegen einen Komfort-Malus von 1 ct/K/Slot; behoben mit
dem Rest-Budget samt Anfangsguthaben (ee0db68).

    python tools/soc_bei_produktionsbeginn.py --tage 12

Produktionsbeginn = erster Slot des Tages, in dem die PV die Hauslast um mehr
als 100 W uebersteigt.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ems.config import load_config                                # noqa: E402
from ems.local_history import read_actual, read_load_temp         # noqa: E402


def _pool_energie(db: str, von, bis) -> float:
    """Gemessene Pool-Energie (kWh) im Zeitraum, je Slot der Maximalwert."""
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT sum(power_w) FROM (SELECT ts, max(power_w) power_w "
            "FROM load_feedback WHERE name='Pool' AND actual_on=1 "
            "AND ts>=? AND ts<? GROUP BY ts)",
            (pd.Timestamp(von).tz_convert("UTC").isoformat(),
             pd.Timestamp(bis).tz_convert("UTC").isoformat())).fetchone()
    finally:
        con.close()
    return (float(row[0]) * 0.25 / 1000.0) if row and row[0] else 0.0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--tage", type=int, default=12)
    args = p.parse_args()

    cfg = load_config(args.config)
    tz = cfg.general.timezone
    db = cfg.e3dc_rscp.history_db_path
    hb = cfg.house_battery
    min_pct = 100.0 * hb.min_soc_wh / max(1.0, hb.capacity_wh)
    thermisch = [ld for ld in cfg.controllable_loads if ld.type == "thermal"]
    soll_c = thermisch[0].target_c if thermisch else float("nan")
    jetzt = pd.Timestamp.now(tz=tz)

    print(f"Kriterium: SoC bei Produktionsbeginn nahe {min_pct:.0f} % "
          f"(Mindest-SoC), Nachtbezug 0")
    print(f"Pool-Solltemperatur {soll_c} C\n")
    print(f"{'Tag':11s}{'Beginn':>7s}{'SoC':>6s}{'Bezug':>8s}{'Pool kWh':>10s}"
          f"{'PoolTemp':>10s}{'Bedarf':>8s}{'PV Tag':>8s}{'ungenutzt':>11s}")
    for n in range(args.tage, 0, -1):
        tag = jetzt.normalize() - pd.Timedelta(days=n)
        nacht_von = tag - pd.Timedelta(hours=4)      # ab 20:00 des Vortags
        ende = tag + pd.Timedelta(days=1)
        pv = read_actual(db, "pv_w", nacht_von, ende, tz)
        haus = read_actual(db, "house_w", nacht_von, ende, tz)
        soc = read_actual(db, "soc", nacht_von, ende, tz)
        netz = read_actual(db, "grid_w", nacht_von, ende, tz)
        if pv is None or soc is None or not len(soc):
            continue
        tags = pv[(pv.index >= tag) & (pv.index < ende)]
        ueber = tags - haus.reindex(tags.index)
        beginn = next((ts for ts in tags.index
                       if np.isfinite(ueber.get(ts, np.nan))
                       and ueber[ts] > 100.0), None)
        if beginn is None:
            continue
        nacht = (soc.index >= nacht_von) & (soc.index < beginn)
        g = (np.asarray(netz.reindex(soc.index[nacht]), dtype=float)
             if netz is not None else np.zeros(1))
        bezug = float(np.nansum(np.maximum(0.0, g))) * 0.25 / 1000.0
        temp = read_load_temp(db, "Pool", beginn - pd.Timedelta(minutes=30),
                              beginn + pd.Timedelta(minutes=30), tz)
        pt = (float(temp.dropna().iloc[0])
              if temp is not None and len(temp.dropna()) else float("nan"))
        s_beg = float(soc.reindex([beginn]).iloc[0])
        ungenutzt = max(0.0, (s_beg - min_pct) / 100.0 * hb.capacity_wh) / 1000.0
        bedarf = ("ja" if np.isfinite(pt) and pt < soll_c - 0.05
                  else "nein" if np.isfinite(pt) else "?")
        print(f"  {tag:%d.%m}   {beginn:%H:%M}{s_beg:5.0f}%{bezug:7.2f}k"
              f"{_pool_energie(db, nacht_von, beginn):9.2f}k{pt:9.2f}C"
              f"{bedarf:>8s}{float(np.nansum(tags)) * 0.25 / 1000:7.1f}k"
              f"{ungenutzt:10.2f}k")
    print("\n  ungenutzt = Energie über dem Mindest-SoC, die in den Tag "
          "mitgenommen wurde")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
