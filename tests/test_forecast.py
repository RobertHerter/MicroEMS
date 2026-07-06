"""Prognose-Tests (pytest): Rezenz-Gewichtung und Umgang mit Datenlücken."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.forecast import LoadForecaster
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
FREQ = "15min"
START = pd.Timestamp("2026-05-01 00:00", tz=TZ)


def _history(days: int, old_w: float, new_w: float, split_days: int) -> pd.Series:
    """Historie: ältere Tage = old_w, die letzten split_days Tage = new_w."""
    idx = pd.date_range(START - pd.Timedelta(days=days), START,
                        freq=FREQ, inclusive="left")
    age_days = (START - idx).total_seconds() / 86400.0
    values = np.where(age_days <= split_days, new_w, old_w).astype(float)
    return pd.Series(values, index=idx)


def test_recency_weighting_follows_recent_level():
    """Mit kurzer Halbwertszeit dominiert das neue Verbrauchsniveau."""
    cfg = make_config()
    cfg.forecast.half_life_days = 7.0
    cfg.forecast.weight_same_temp = 0.0
    hist = _history(days=60, old_w=200.0, new_w=600.0, split_days=30)
    fc = LoadForecaster(cfg).forecast(hist, START, 96)
    assert float(fc.mean()) > 500.0, \
        f"Rezenz-Gewichtung wirkt nicht (Mittel {fc.mean():.0f} W)"


def test_recency_disabled_averages_all_history():
    """half_life_days=0: alte und neue Tage zählen (fast) gleich."""
    cfg = make_config()
    cfg.forecast.half_life_days = 0.0
    cfg.forecast.weight_same_temp = 0.0
    hist = _history(days=60, old_w=200.0, new_w=600.0, split_days=30)
    fc = LoadForecaster(cfg).forecast(hist, START, 96)
    assert 300.0 < float(fc.mean()) < 500.0, \
        f"Ohne Rezenz sollte das Mittel dazwischen liegen ({fc.mean():.0f} W)"


def test_gaps_in_history_are_skipped():
    """NaN-Lücken (Sensorausfall) führen weder zu NaN noch zu Absturz."""
    cfg = make_config()
    hist = _history(days=30, old_w=400.0, new_w=400.0, split_days=0)
    hist.iloc[200:1200] = np.nan          # mehrtägige Lücke
    hist.iloc[-50:] = np.nan              # Lücke direkt vor 'jetzt'
    fc = LoadForecaster(cfg).forecast(hist, START, 96)
    assert len(fc) == 96
    assert fc.notna().all(), "Prognose enthält NaN"
    assert (fc >= 0).all()
    assert abs(float(fc.mean()) - 400.0) < 50.0


def test_empty_history_returns_zeros():
    cfg = make_config()
    empty = pd.Series(dtype="float64",
                      index=pd.DatetimeIndex([], tz=TZ))
    fc = LoadForecaster(cfg).forecast(empty, START, 96)
    assert len(fc) == 96 and (fc == 0).all()
