"""Tests für die Plausibilitäts-Grenzen externer Eingaben (ems/sanity.py)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems import sanity
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _idx(n):
    return pd.date_range("2026-01-20 00:00", periods=n, freq="15min", tz=TZ)


def test_sanitize_price_replaces_spike_and_out_of_range():
    idx = _idx(6)
    price = pd.Series([30.0, 32.0, 5000.0, 31.0, -500.0, 29.0], index=idx)
    reports = []
    out = sanity.sanitize_price(price, -100.0, 200.0, 10.0, reports)
    med = float(np.median(price))
    assert out.iloc[2] == pytest.approx(med)   # 5000 -> Median (Spike + über max)
    assert out.iloc[4] == pytest.approx(med)   # -500 -> Median (unter min)
    assert out.iloc[0] == 30.0 and out.iloc[3] == 31.0   # normale Werte bleiben
    assert reports and reports[0]["signal"] == "Strompreis"


def test_sanitize_price_allows_normal_negative_prices():
    idx = _idx(4)
    price = pd.Series([-5.0, 10.0, 20.0, 30.0], index=idx)
    out = sanity.sanitize_price(price, -100.0, 200.0, 10.0, [])
    assert list(out) == [-5.0, 10.0, 20.0, 30.0]   # moderat negativ bleibt


def test_sanitize_pv_clamps_negative_and_cap():
    idx = _idx(4)
    pv = pd.Series([-50.0, 1000.0, 9000.0, 500.0], index=idx)
    reports = []
    out = sanity.sanitize_pv(pv, "PV", cap_w=8000.0, reports=reports)
    assert out.iloc[0] == 0.0 and out.iloc[2] == 8000.0
    assert out.iloc[1] == 1000.0
    assert len(reports) == 2   # negativ + über Cap


def test_sanitize_inputs_uses_pv_cap_from_kwp_and_reports():
    from ems.config import PvArray
    cfg = make_config()
    cfg.pv_model.arrays = [PvArray("Ost", kwp=5.0, tilt=20, azimuth=90),
                           PvArray("West", kwp=5.0, tilt=20, azimuth=270)]
    idx = _idx(4)
    price = pd.Series(30.0, index=idx)
    pv = pd.Series([0.0, 20000.0, 3000.0, -10.0], index=idx)  # 20 kW > 10 kWp*1.15
    load = pd.Series([-100.0, 500.0, 800.0, 600.0], index=idx)
    p, out_pv, out_pv10, out_load = sanity.sanitize_inputs(
        cfg, price=price, pv=pv, pv10=None, load=load)
    assert out_pv.iloc[1] == pytest.approx(10000.0 * 1.15)   # gekappt auf kWp*1.15
    assert out_pv.iloc[3] == 0.0
    assert out_load.iloc[0] == 0.0
    assert out_pv10 is None
    assert cfg._sanity_reports   # Reports für Alarm/Dashboard hinterlegt


def test_sanitize_inputs_noop_on_clean_data():
    cfg = make_config()
    idx = _idx(4)
    price = pd.Series([25.0, 30.0, 28.0, 26.0], index=idx)
    pv = pd.Series([0.0, 1000.0, 2000.0, 500.0], index=idx)
    load = pd.Series([300.0, 400.0, 500.0, 350.0], index=idx)
    p, out_pv, _, out_load = sanity.sanitize_inputs(
        cfg, price=price, pv=pv, pv10=None, load=load)
    assert list(p) == list(price) and list(out_pv) == list(pv)
    assert list(out_load) == list(load)
    assert cfg._sanity_reports == []
