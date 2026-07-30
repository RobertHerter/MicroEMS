"""Messwerkzeug für den binären Entscheidungshorizont (pool_horizon_bench.py).

Der Horizont wurde seinerzeit gekürzt, weil der Solver ins Zeitlimit lief. Ob
das noch nötig ist, ist eine Messfrage - und die Messung ist nur so viel wert
wie ihre Rekonstruktion des Laufs. Deshalb prüft dieser Test genau das: dass
aus dem Schnappschuss dieselben Eingaben entstehen, insbesondere die
Pool-Eingänge, die sonst still auf None fallen und den Fall entschärfen würden.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from pool_horizon_bench import _inputs_from_snapshot, _thermal_loads
from tests.test_synthetic import make_config


def _snapshot(n: int = 8) -> dict:
    index = pd.date_range("2026-01-15 00:00", periods=n, freq="15min",
                          tz="Europe/Berlin")
    return {
        "generated": index[0].isoformat(),
        "inputs": {
            "index": [t.isoformat() for t in index],
            "house_load_w": [800.0] * n,
            "pv_w": [0.0] * n,
            "pv10_w": None,
            "price_ct_kwh": [25.0] * n,
            "spot_price_ct_kwh": [20.0] * n,
            "feedin_ct_kwh": [8.0] * n,
            "initial_house_soc_wh": 5000.0,
            "initial_car_soc_wh": None,
            "car_present": False,
            "ambient_temp_c": [4.0] * n,
            "solar_w_m2": [0.0] * n,
            "load_state": {"Pool": 24.5},
            "load_feedback": {},
        },
    }


def test_snapshot_rebuild_keeps_the_thermal_inputs():
    """Ohne Außentemperatur und Einstrahlung rechnet das Thermomodell einen
    anderen Fall - die Laufzeitmessung wäre dann nicht die des echten Laufs."""
    inp = _inputs_from_snapshot(_snapshot())
    assert len(inp.index) == 8
    assert inp.ambient_temp_c is not None and float(inp.ambient_temp_c[0]) == 4.0
    assert inp.solar_w_m2 is not None
    assert inp.load_state == {"Pool": 24.5}
    assert float(inp.initial_house_soc_wh) == 5000.0
    assert inp.spot_price_ct_kwh is not None


def test_missing_optional_series_stay_none_instead_of_zero():
    """Ein fehlendes Band darf nicht als Nullreihe erscheinen - das wäre eine
    stille Falschaussage über die Eingaben."""
    snap = _snapshot()
    snap["inputs"]["pv10_w"] = None
    snap["inputs"]["solar_w_m2"] = None
    inp = _inputs_from_snapshot(snap)
    assert inp.pv10_w is None
    assert inp.solar_w_m2 is None


def test_none_entries_become_nan_not_zero():
    """None in einer Reihe heißt 'unbekannt', nicht 'null Watt'."""
    snap = _snapshot()
    snap["inputs"]["pv_w"] = [None] + [0.0] * 7
    werte = _inputs_from_snapshot(snap).pv_w
    assert np.isnan(werte[0])


def test_thermal_loads_are_selected_by_type_and_enabled_flag():
    from ems.config import ControllableLoad, LoadStage

    cfg = make_config()
    cfg.controllable_loads = [
        ControllableLoad(name="Pool", type="thermal", enabled=True,
                         target_c=28.0, min_c=26.0, max_c=32.0,
                         stages=[LoadStage("klein", 400, 1000)]),
        ControllableLoad(name="Pool alt", type="thermal", enabled=False,
                         target_c=28.0, min_c=26.0, max_c=32.0,
                         stages=[LoadStage("klein", 400, 1000)]),
        ControllableLoad(name="Trockner", type="deferrable", enabled=True,
                         power_w=2000.0, runtime_minutes=90.0),
    ]
    assert [ld.name for ld in _thermal_loads(cfg)] == ["Pool"]


def test_snapshot_from_disk_parses(tmp_path):
    pfad = tmp_path / "snap.json"
    pfad.write_text(json.dumps(_snapshot()), encoding="utf-8")
    with open(pfad, encoding="utf-8") as fh:
        assert _inputs_from_snapshot(json.load(fh)) is not None
