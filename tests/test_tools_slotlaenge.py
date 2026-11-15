"""Auswertewerkzeuge rechnen W-Summen mit der Slotlaenge der Config in kWh um.

Frueher stand dort fest ``* 0.25``: auf einer Anlage mit 30-min-Raster zeigten
die Werkzeuge die halbe Energie.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from ems.local_history import write_load_feedback

ROOT = Path(__file__).resolve().parents[1]


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"tools_{name}", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pool_energie_scales_with_the_slot_length(tmp_path):
    tool = _tool("soc_bei_produktionsbeginn")
    db = str(tmp_path / "history.sqlite")
    von = pd.Timestamp("2026-08-20 20:00", tz="Europe/Berlin")
    # Vier Slots mit 3000 W (je Slot zwei Stufen-Zeilen: der Maximalwert
    # zaehlt), dazu ein ausgeschalteter Slot und einer ausserhalb des Fensters.
    for k in range(4):
        ts = von + pd.Timedelta(minutes=30 * k)
        write_load_feedback(db, ts, "Pool", "1", {"on": True, "power_w": 1500.0})
        write_load_feedback(db, ts, "Pool", "2", {"on": True, "power_w": 3000.0})
    write_load_feedback(db, von + pd.Timedelta(hours=2), "Pool", "1",
                        {"on": False, "power_w": 3000.0})
    bis = von + pd.Timedelta(hours=3)
    write_load_feedback(db, bis, "Pool", "1", {"on": True, "power_w": 3000.0})

    assert tool._pool_energie(db, von, bis, 0.25) == 3.0     # 4 x 3 kW x 15 min
    assert tool._pool_energie(db, von, bis, 0.5) == 6.0      # 4 x 3 kW x 30 min


def test_tools_have_no_hardwired_quarter_hour_left():
    """Die Umrechnung W -> kWh darf nirgends mehr fest 0,25 h annehmen."""
    for name in ("pool_komfort_sweep", "soc_bei_produktionsbeginn",
                 "pv_nowcast_sweep"):
        text = (ROOT / "tools" / f"{name}.py").read_text(encoding="utf-8")
        assert "* 0.25 / 1000" not in text, name
        assert "slot_minutes=15" not in text, name
    assert "* 0.25 / 1000" not in (ROOT / "pool_horizon_bench.py").read_text(
        encoding="utf-8")
