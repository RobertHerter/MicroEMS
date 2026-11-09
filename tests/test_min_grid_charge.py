"""Mindestmenge je Netzladung (optimization.min_grid_charge_kwh).

Am 09.11.2026 plante der Optimierer zweimal 155-165 W Netzladen für einen
Slot (37 Wh), um 0,4 ct/kWh Preisunterschied mitzunehmen. Ein Netzladeblock
bringt jetzt 0 oder mindestens die Mindestmenge in den Akku.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.local_history import read_grid_charge_run_wh, write_execution_plan
from ems.optimizer import GRID_CHARGE_MIN_WINDOW_H, Optimizer, grid_charge_min_slots
from ems.validate import _grid_charge_blocks
from tests.test_optimizer import _day_index, _inputs
from tests.test_synthetic import make_config


def _cfg(min_kwh):
    cfg = make_config()
    cfg.optimization.min_grid_charge_kwh = min_kwh
    cfg.optimization.terminal_soc_value = 0.0   # kein Restwert: nur der Abend zählt
    cfg.optimization.solver_mip_gap = 0.0
    cfg.optimization.solver_mip_gap_abs_ct = 0.0
    return cfg


def _kleiner_bedarf(idx):
    """Billig in Slot 0, danach eine Stunde teuer mit wenig Last (~0,2 kWh)."""
    n = len(idx)
    preis = np.full(n, 30.0)
    preis[0] = 10.0
    preis[1:5] = 50.0
    last = np.zeros(n)
    last[1:5] = 200.0
    return preis, last


def _bloecke_wh(table, eta):
    werte = table["batt_grid_charge_w"].to_numpy(dtype=float)
    out, t = [], 0
    while t < len(werte):
        if werte[t] > 5.0:
            start = t
            while t < len(werte) and werte[t] > 5.0:
                t += 1
            out.append((start, t, eta * werte[start:t].sum() * 0.25))
        else:
            t += 1
    return out


def test_ohne_mindestmenge_plant_der_optimierer_eine_kleinstladung():
    """Gegenprobe: das Szenario erzeugt ohne Untergrenze wirklich eine Kleinstladung."""
    idx = _day_index("2026-01-15")[:8]
    preis, last = _kleiner_bedarf(idx)
    cfg = _cfg(0.0)
    res = Optimizer(cfg).solve(_inputs(idx, load=last, price=preis,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    bloecke = _bloecke_wh(res.table, cfg.house_battery.eff_ac_charge)
    assert bloecke and 0.0 < bloecke[0][2] < 500.0


def test_kleinstladung_entfaellt_mit_mindestmenge():
    idx = _day_index("2026-01-15")[:8]
    preis, last = _kleiner_bedarf(idx)
    cfg = _cfg(1.0)
    res = Optimizer(cfg).solve(_inputs(idx, load=last, price=preis,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    assert res.table["batt_grid_charge_w"].max() < 5.0


def test_lohnende_netzladung_bringt_mindestens_die_mindestmenge():
    """Großer Bedarf: geladen wird weiter, jeder Block hat mindestens 1 kWh."""
    idx = _day_index("2026-01-15")[:16]
    n = len(idx)
    preis = np.full(n, 50.0)
    preis[:2] = 10.0
    last = np.zeros(n)
    last[2:] = 600.0
    cfg = _cfg(1.0)
    res = Optimizer(cfg).solve(_inputs(idx, load=last, price=preis,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    bloecke = _bloecke_wh(res.table, cfg.house_battery.eff_ac_charge)
    assert bloecke, "billiger Netzstrom fuer 3 h teure Last sollte geladen werden"
    for start, ende, wh in bloecke:
        if ende < n:
            assert wh >= 1000.0 - 1.0, (start, ende, wh)
    assert _grid_charge_blocks(cfg, res.table["batt_grid_charge_w"], None) is None


def test_laufende_netzladung_zaehlt_auf_die_mindestmenge():
    """Lief schon genug Netzladen, darf Slot 0 mit einem kleinen Rest weiterladen."""
    idx = _day_index("2026-01-15")[:8]
    preis, last = _kleiner_bedarf(idx)
    cfg = _cfg(1.0)
    inp = _inputs(idx, load=last, price=preis, soc=cfg.house_battery.min_soc_wh,
                  grid_charge_run_wh=1500.0)
    res = Optimizer(cfg).solve(inp)
    assert not res.infeasible
    erster = float(res.table["batt_grid_charge_w"].iloc[0])
    assert 50.0 <= erster < 2000.0
    assert _grid_charge_blocks(cfg, res.table["batt_grid_charge_w"], inp) is None


def test_planpruefung_meldet_eine_zu_kleine_netzladung():
    cfg = make_config()
    idx = _day_index("2026-01-15")[:6]
    werte = pd.Series([0.0, 165.0, 0.0, 0.0, 0.0, 0.0], index=idx)
    verstoss = _grid_charge_blocks(cfg, werte, None)
    assert verstoss is not None and verstoss.rule == "power.grid_charge_min"
    assert verstoss.slots == [idx[1]]
    # Am Horizontende ist der Block offen, kein Verstoß.
    offen = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, 165.0], index=idx)
    assert _grid_charge_blocks(cfg, offen, None) is None
    cfg.optimization.min_grid_charge_kwh = 0.0
    assert _grid_charge_blocks(cfg, werte, None) is None


def test_mindestmenge_gilt_im_fenster_am_horizontbeginn():
    """Nur Blöcke in den ersten GRID_CHARGE_MIN_WINDOW_H Stunden zählen."""
    cfg = make_config()
    idx = _day_index("2026-01-15")[:48]          # 12 h
    assert grid_charge_min_slots(len(idx), 0.25) == int(GRID_CHARGE_MIN_WINDOW_H * 4)
    spaet = pd.Series(0.0, index=idx)
    spaet.iloc[30] = 165.0                        # 7,5 h voraus: offen
    assert _grid_charge_blocks(cfg, spaet, None) is None
    frueh = pd.Series(0.0, index=idx)
    frueh.iloc[10] = 165.0
    assert _grid_charge_blocks(cfg, frueh, None) is not None


def test_optimierer_haelt_die_mindestmenge_ueber_einen_tag():
    """Kleinbedarf jeden Abend: im Fenster keine Kleinstladung, Plan gültig."""
    idx = _day_index("2026-01-15")
    n = len(idx)
    preis = np.full(n, 30.0)
    preis[::8] = 10.0                             # alle 2 h ein billiger Slot
    last = np.full(n, 150.0)
    cfg = make_config()
    cfg.optimization.terminal_soc_value = 0.0
    inp = _inputs(idx, load=last, price=preis, soc=cfg.house_battery.min_soc_wh)
    res = Optimizer(cfg).solve(inp)
    assert not res.infeasible
    assert _grid_charge_blocks(cfg, res.table["batt_grid_charge_w"], inp) is None


def test_laufende_netzladung_aus_dem_fahrplan(tmp_path):
    cfg = make_config()
    pfad = str(tmp_path / "hist.sqlite")
    idx = pd.date_range("2026-11-09 05:00", periods=6, freq="15min",
                        tz="Europe/Berlin")
    tabelle = pd.DataFrame({
        "batt_grid_charge_w": [4000.0, 0.0, 2000.0, 3000.0, 0.0, 0.0],
        "mode": ["grid_charge", "auto", "grid_charge", "grid_charge", "auto", "auto"],
    }, index=idx)
    write_execution_plan(pfad, idx[0] - pd.Timedelta(minutes=5), tabelle)
    slot = cfg.general.slot_minutes
    # Slot 05:45 beginnt nach zwei Netzladeslots: 2000 + 3000 W je 15 min.
    assert read_grid_charge_run_wh(pfad, idx[4] + pd.Timedelta(minutes=2),
                                   slot) == 1250.0
    # Slot 06:00: der Vorslot lud nicht.
    assert read_grid_charge_run_wh(pfad, idx[5], slot) == 0.0
    # Slot 05:15: davor lief 4 kW.
    assert read_grid_charge_run_wh(pfad, idx[1], slot) == 1000.0
    assert read_grid_charge_run_wh(str(tmp_path / "fehlt.sqlite"), idx[1], slot) == 0.0
