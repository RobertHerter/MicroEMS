#!/usr/bin/env python3
"""Lastprognose-Bias je Tagesblock, getrennt nach Prognosestand.

Zwei Staende sagen Verschiedenes:

* **operativ** - der Stand, der im jeweiligen Slot wirklich galt (mit
  Nowcast-Korrektur). Das ist die Prognose, die den Akku gefuehrt hat.
* **Tagesstart** - der 00:00-Stand. Er treibt die naechtliche SoC-Planung,
  korrigiert sich aber durch die Neuplanung alle 15 min weitgehend weg.

Gemessen am 31.08.2026 ueber 14 Tage lagen sie weit auseinander (00:00 +129 W
und 34,0 % WAPE, operativ -21 W und 28,2 %). Der Waechter hat wochenlang den
falschen Stand gemeldet; seit 4421c36 loest er auf dem operativen aus.

    python tools/prognose_bias_stand.py --tage 14
    python tools/prognose_bias_stand.py --tage 3 --nur-block 06-10

Der operative Stand kostet einen Archivzugriff je Slot (gemessen 4,9 ms).
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ems.config import load_config                              # noqa: E402
from ems.local_history import (read_house_load,                 # noqa: E402
                               read_optimizer_forecast_asof)

BLOECKE = ((0, 6, "00-06"), (6, 10, "06-10"), (10, 14, "10-14"),
           (14, 18, "14-18"), (18, 22, "18-22"), (22, 24, "22-24"))


def _wape(ist, prog) -> float:
    ist, prog = np.asarray(ist, float), np.asarray(prog, float)
    return 100.0 * np.abs(prog - ist).sum() / max(1e-9, np.abs(ist).sum())


def sammle(cfg, tage: float) -> pd.DataFrame:
    tz = cfg.general.timezone
    db = cfg.e3dc_rscp.history_db_path
    schritt = pd.Timedelta(minutes=cfg.general.slot_minutes)
    jetzt = pd.Timestamp.now(tz=tz)
    zeilen = []
    tag = jetzt.normalize() - pd.Timedelta(days=float(tage))
    while tag < jetzt.normalize():
        nxt = tag + pd.Timedelta(days=1)
        ist = read_house_load(db, tag, nxt, tz)
        try:
            _issue, start_frame = read_optimizer_forecast_asof(db, tag, tag,
                                                              nxt, tz)
        except Exception:
            start_frame = None
        start = (pd.to_numeric(start_frame["house_load_w"], errors="coerce")
                 if start_frame is not None and not start_frame.empty
                 and "house_load_w" in start_frame else None)
        for ts in ist.index:
            a = float(ist.loc[ts])
            if not np.isfinite(a):
                continue
            p_start = (float(start.loc[ts])
                       if start is not None and ts in start.index else np.nan)
            try:
                _i, nah = read_optimizer_forecast_asof(db, ts, ts,
                                                       ts + schritt, tz)
                p_op = (float(pd.to_numeric(nah["house_load_w"],
                                            errors="coerce").iloc[0])
                        if nah is not None and not nah.empty
                        and "house_load_w" in nah else np.nan)
            except Exception:
                p_op = np.nan
            zeilen.append((ts, a, p_start, p_op))
        tag = nxt
    return pd.DataFrame(zeilen,
                        columns=["ts", "ist", "start", "operativ"]).set_index("ts")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--tage", type=float, default=14.0)
    p.add_argument("--nur-block", dest="nur_block", default=None,
                   help="z.B. 06-10; ohne Angabe alle Bloecke")
    args = p.parse_args()

    cfg = load_config(args.config)
    d = sammle(cfg, args.tage)
    if d.empty:
        print("keine auswertbaren Slots")
        return 1
    print(f"{len(d)} Slots, {args.tage:.0f} Tage "
          f"({d.index[0]:%d.%m} bis {d.index[-1]:%d.%m})")
    print("Bias = Prognose - Ist, positiv = Modell sagt zu viel "
          "(ems/quality.bias_w)\n")
    print(f"{'Block':8s}{'n':>6s}{'Bias 00:00':>12s}{'Bias operativ':>15s}"
          f"{'WAPE 00:00':>12s}{'WAPE operativ':>15s}{'Gewinn':>9s}")
    for von, bis, name in BLOECKE:
        if args.nur_block and args.nur_block != name:
            continue
        sub = d[(d.index.hour >= von) & (d.index.hour < bis)]
        m_s, m_o = sub["start"].notna(), sub["operativ"].notna()
        if m_o.sum() < 8:
            continue
        b_s = (float((sub.loc[m_s, "start"] - sub.loc[m_s, "ist"]).median())
               if m_s.sum() else float("nan"))
        b_o = float((sub.loc[m_o, "operativ"] - sub.loc[m_o, "ist"]).median())
        w_s = (_wape(sub.loc[m_s, "ist"], sub.loc[m_s, "start"])
               if m_s.sum() else float("nan"))
        w_o = _wape(sub.loc[m_o, "ist"], sub.loc[m_o, "operativ"])
        print(f"  {name:6s}{int(m_o.sum()):6d}{b_s:+12.0f}{b_o:+15.0f}"
              f"{w_s:12.1f}{w_o:15.1f}{w_s - w_o:+9.1f}")
    for name, spalte in (("00:00-Stand", "start"), ("operativ", "operativ")):
        m = d[spalte].notna()
        if not m.sum():
            continue
        print(f"\n  {name:12s} n={int(m.sum()):5d}  "
              f"Bias {float((d.loc[m, spalte] - d.loc[m, 'ist']).median()):+5.0f} W  "
              f"WAPE {_wape(d.loc[m, 'ist'], d.loc[m, spalte]):.1f} %")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
