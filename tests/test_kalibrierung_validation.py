"""Rolling-Origin-Prognose-Validierung (kalibrierung.validate_forecast_series).

Prüft auf synthetischer Historie:
  * beide Methoden (ml + similar_days) werden auf denselben Folds bewertet,
  * Metriken (MAE/WAPE) gesamt und je Tageszeit/Werktag-WE/Saison vorhanden
    und plausibel,
  * Korrekturen (correction_factor) sind neutralisiert - das Ergebnis misst
    das rohe Modell,
  * zu wenig Daten -> None statt Absturz.
"""
from __future__ import annotations

import pandas as pd
import pytest

from kalibrierung import validate_forecast_series
from tests.test_synthetic import make_config, synthetic_history


def _setup(days=120):
    cfg = make_config()
    cfg.forecast.lookback_days = days
    now = pd.Timestamp("2026-06-10 00:00", tz=cfg.general.timezone)
    hist = synthetic_history(cfg, now)
    return cfg, hist, now


def test_validation_compares_methods_on_same_folds():
    pytest.importorskip("sklearn")
    cfg, hist, now = _setup()
    res = validate_forecast_series(cfg, hist, None, None, now,
                                   folds=3, horizon_hours=24,
                                   min_train_days=30)
    assert res is not None and res["folds"] == 3
    assert set(res["methods"]) == {"ml", "similar_days"}
    for m, seg in res["methods"].items():
        g = seg["gesamt"]
        assert g["n"] > 0 and 0.0 < g["wape_pct"] < 100.0, (m, g)
        assert g["mae_W"] > 0.0
        # Segment-Aufschlüsselung vorhanden (Sommer-Folds -> Saison Sommer)
        assert "Nacht 00-06" in seg and "Werktag" in seg and "Sommer" in seg
    assert res["empfehlung"] in res["methods"]
    best = res["empfehlung"]
    assert all(res["methods"][best]["gesamt"]["wape_pct"]
               <= res["methods"][m]["gesamt"]["wape_pct"]
               for m in res["methods"])


def test_validation_neutralizes_correction_factor():
    """Ein absurder globaler Korrekturfaktor darf das Validierungsergebnis
    nicht beeinflussen (gemessen wird das rohe Modell) - und er wird nach
    dem Lauf wiederhergestellt."""
    cfg, hist, now = _setup()
    cfg.forecast.method = "similar_days"
    cfg.forecast.correction_factor = 5.0
    res = validate_forecast_series(cfg, hist, None, None, now,
                                   folds=2, horizon_hours=24,
                                   min_train_days=30)
    assert res is not None
    wape = res["methods"]["similar_days"]["gesamt"]["wape_pct"]
    assert wape < 100.0, "Faktor 5.0 hat durchgeschlagen - nicht neutralisiert"
    assert cfg.forecast.correction_factor == 5.0     # wiederhergestellt
    assert cfg.forecast.method == "similar_days"


def test_validation_returns_none_on_insufficient_history():
    cfg, hist, now = _setup(days=10)
    assert validate_forecast_series(cfg, hist, None, None, now,
                                    folds=5, horizon_hours=48,
                                    min_train_days=60) is None
