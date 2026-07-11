"""Tests der optionalen ML-Lastprognose (forecast.method = 'ml')."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems import forecast as fc_mod
from ems.forecast import LoadForecaster
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
FREQ = "15min"
START = pd.Timestamp("2026-05-01 00:00", tz=TZ)


def _history(days: int) -> pd.Series:
    """Historie mit klarem Tagesprofil (nachts 200 W, tags 800 W)."""
    idx = pd.date_range(START - pd.Timedelta(days=days), START, freq=FREQ,
                        inclusive="left")
    hour = idx.tz_convert(TZ).hour
    val = np.where((hour >= 8) & (hour < 20), 800.0, 200.0).astype(float)
    return pd.Series(val, index=idx)


def _ml_config():
    cfg = make_config()
    cfg.forecast.method = "ml"
    cfg.forecast.weight_same_temp = 0.0
    return cfg


def test_ml_forecast_runs_and_is_sane():
    cfg = _ml_config()
    fc = LoadForecaster(cfg).forecast(_history(30), START, 96)
    assert len(fc) == 96
    assert fc.notna().all()
    assert (fc >= 0).all()
    assert 150.0 < float(fc.mean()) < 850.0        # im Bereich des Tagesprofils


def test_ml_lag_robust_to_gaps():
    """Unregelmäßiger Index (fehlende Zeilen) darf den 7-Tage-Lag nicht
    verschieben/abstürzen lassen (Lag per Zeitstempel, nicht per Position)."""
    hist = _history(30)
    # ganze Zeilen entfernen -> irregulärer Index (wie sparse read_house_load)
    hist = hist.drop(hist.index[500:1500])
    fc = LoadForecaster(_ml_config()).forecast(hist, START, 96)
    assert len(fc) == 96 and fc.notna().all() and (fc >= 0).all()


def test_ml_falls_back_when_training_fails(monkeypatch):
    """Schlägt das ML-Training fehl, wird transparent auf 'similar_days'
    zurückgefallen (kein Absturz, sinnvolle Prognose)."""
    import sklearn.ensemble

    class BoomRegressor:
        def __init__(self, *a, **k):
            pass

        def fit(self, *a, **k):
            raise RuntimeError("Training kaputt")

    monkeypatch.setattr(sklearn.ensemble, "HistGradientBoostingRegressor", BoomRegressor)
    fc_mod._ML_CACHE.clear()
    fc = LoadForecaster(_ml_config()).forecast(_history(30), START, 96)
    assert len(fc) == 96 and fc.notna().all()
    assert 150.0 < float(fc.mean()) < 850.0        # similar_days lieferte ein Ergebnis


def test_ml_model_cache_reused(monkeypatch):
    """Zweiter forecast()-Aufruf mit DERSELBEN Historie trainiert nicht erneut."""
    import sklearn.ensemble
    real = sklearn.ensemble.HistGradientBoostingRegressor
    fits = {"n": 0}

    class Counting(real):
        def fit(self, *a, **k):
            fits["n"] += 1
            return super().fit(*a, **k)

    monkeypatch.setattr(sklearn.ensemble, "HistGradientBoostingRegressor", Counting)
    fc_mod._ML_CACHE.clear()
    cfg, hist = _ml_config(), _history(30)
    f1 = LoadForecaster(cfg).forecast(hist, START, 96)
    f2 = LoadForecaster(cfg).forecast(hist, START, 96)     # gleiche Historie
    assert fits["n"] == 1, f"Modell wurde {fits['n']}x trainiert (Cache greift nicht)"
    # identische Vorhersage aus dem Cache
    assert np.allclose(f1.values, f2.values)


def test_similar_days_unaffected():
    """Default-Methode bleibt unverändert nutzbar."""
    cfg = make_config()          # method default 'similar_days'
    fc = LoadForecaster(cfg).forecast(_history(30), START, 96)
    assert len(fc) == 96 and fc.notna().all()


def test_ml_empty_horizon_returns_empty():
    """Leerer Horizont (horizon=0, z.B. Intraday ohne Ist-Daten) darf den ML-
    Pfad nicht crashen (sklearn predict auf 0 Zeilen)."""
    fc = LoadForecaster(_ml_config()).forecast(_history(30), START, 0)
    assert len(fc) == 0
