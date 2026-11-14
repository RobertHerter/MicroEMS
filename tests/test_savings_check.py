"""Tests für das tageweise Ablegen der validierten Ersparnis (savings_check)."""
from __future__ import annotations

import pandas as pd
import pytest

import savings_check
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _data(start, end):
    idx = pd.date_range(start, end, freq="15min", tz=TZ, inclusive="left")
    meter = pd.DataFrame({
        "pv_wh": 0.0, "load_wh": 100.0, "bat_in_wh": 0.0, "bat_out_wh": 0.0,
        "grid_import_wh": 100.0, "grid_export_wh": 0.0}, index=idx)
    return {"repo": None, "meter": meter,
            "price": pd.Series(30.0, index=idx),
            "spot": pd.Series(dtype=float),
            "feedin": pd.Series(8.0, index=idx), "grid_w": None}


@pytest.fixture
def written(monkeypatch):
    rows = []
    monkeypatch.setattr(savings_check, "write_savings_validated",
                        lambda db, day, r: rows.append((day, r["n_slots"])))
    monkeypatch.setattr(savings_check, "read_savings_baseline_soc",
                        lambda db, day: 5000.0)
    return rows


def test_teiltag_ab_0145_wird_nicht_als_voller_tag_abgelegt(written):
    """89 von 96 Slots lagen über der alten 90-%-Schwelle und überschrieben den
    korrekten Wert der Vornacht."""
    cfg = make_config()
    data = _data("2026-11-03 01:45", "2026-11-05 01:45")
    now = pd.Timestamp("2026-11-05 01:45", tz=TZ)
    n = savings_check._persist_days(cfg, data, now)
    assert written == [("2026-11-04", 96)]
    assert n == 1


def test_zeitumstellung_jeder_kalendertag_genau_einmal(written):
    """25.10.2026 hat 25 h: 00:00 + 24 h wäre 23:00 desselben Tages."""
    cfg = make_config()
    data = _data("2026-10-24 00:00", "2026-10-28 00:00")
    now = pd.Timestamp("2026-10-28 00:00", tz=TZ)
    savings_check._persist_days(cfg, data, now)
    assert written == [("2026-10-24", 96), ("2026-10-25", 100),
                       ("2026-10-26", 96), ("2026-10-27", 96)]


def test_fruehjahrsumstellung_92_slots_gelten_als_voll(written):
    cfg = make_config()
    data = _data("2026-03-29 00:00", "2026-03-31 00:00")
    now = pd.Timestamp("2026-03-31 00:00", tz=TZ)
    savings_check._persist_days(cfg, data, now)
    assert written == [("2026-03-29", 92), ("2026-03-30", 96)]
