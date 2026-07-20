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

import numpy as np
import pandas as pd
import pytest

from kalibrierung import validate_forecast_series
from tests.test_synthetic import make_config, synthetic_history

pytestmark = pytest.mark.slow


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
    assert res["exogenous_mode"] == "historical_bootstrap"
    assert res["correction_profile_compatible"] is False
    assert res["correction_profile_source"] == "historical_bootstrap"
    assert res["archive_weight"] == 0.0
    assert len(res["hourly_correction"]) == 24
    assert res["global_correction"] > 0.0
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


def test_validation_does_not_leak_future_exogenous_features(monkeypatch):
    """Ohne issue_time-Archiv dürfen Ist-Temperatur und final revidierte
    PV-Prognose nicht als am historischen Origin bekannte Zukunft gelten."""
    cfg, hist, now = _setup()
    temp = pd.Series(20.0, index=hist.index)
    pv = pd.Series(1000.0, index=hist.index)
    from ems.forecast import LoadForecaster
    real = LoadForecaster.forecast
    seen = []

    def wrapped(self, *args, **kwargs):
        seen.append((kwargs.get("hist_temp"), kwargs.get("fut_temp"),
                     kwargs.get("hist_pv"), kwargs.get("fut_pv")))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(LoadForecaster, "forecast", wrapped)
    res = validate_forecast_series(cfg, hist, temp, pv, now, folds=2,
                                   horizon_hours=24, min_train_days=30)
    assert res is not None and seen
    assert all(all(v is None for v in call) for call in seen)


def test_validation_uses_complete_issue_time_archive(monkeypatch):
    cfg, hist, now = _setup()
    temp = pd.Series(20.0, index=hist.index)
    pv = pd.Series(1000.0, index=hist.index)
    seen = []
    from ems.forecast import LoadForecaster
    real = LoadForecaster.forecast

    def archive_reader(origin, end):
        idx = pd.date_range(origin, end,
                            freq=f"{cfg.general.slot_minutes}min",
                            inclusive="left")
        return {"temp": pd.Series(18.0, index=idx),
                "pv": pd.Series(800.0, index=idx), "complete": True}

    def wrapped(self, *args, **kwargs):
        seen.append((kwargs.get("hist_temp"), kwargs.get("fut_temp"),
                     kwargs.get("hist_pv"), kwargs.get("fut_pv")))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(LoadForecaster, "forecast", wrapped)
    res = validate_forecast_series(
        cfg, hist, temp, pv, now, folds=2, horizon_hours=24,
        min_train_days=30, archive_reader=archive_reader)
    assert res["archive_folds"] == res["folds"] == 2
    assert res["exogenous_mode"] == "issue_time_archive"
    assert res["correction_profile_compatible"] is True
    assert res["archive_weight"] == 1.0
    assert res["correction_profile_source"] == "issue_time_archive"
    # Langzeit-Bootstrap bleibt bewusst ohne Zukunftsmerkmale; die getrennten
    # Archiv-Folds müssen dagegen die damaligen Wetter-/PV-Reihen erhalten.
    assert seen and any(all(v is None for v in call) for call in seen)
    assert any(all(v is not None for v in call) for call in seen)


def test_validation_blends_partial_weekly_archive_into_bootstrap():
    cfg, hist, now = _setup()
    temp = pd.Series(20.0, index=hist.index)
    pv = pd.Series(1000.0, index=hist.index)
    calls = 0

    def archive_reader(origin, end):
        nonlocal calls
        calls += 1
        idx = pd.date_range(origin, end,
                            freq=f"{cfg.general.slot_minutes}min",
                            inclusive="left")
        return {"temp": pd.Series(18.0, index=idx),
                "pv": pd.Series(800.0, index=idx),
                "complete": calls <= 3}

    res = validate_forecast_series(
        cfg, hist, temp, pv, now, folds=6, horizon_hours=24,
        min_train_days=30, archive_reader=archive_reader)

    assert res["archive_folds"] == 3
    assert res["archive_min_folds"] == 6
    assert res["archive_weight"] == 0.5
    assert res["exogenous_mode"] == "hybrid_issue_time_archive"
    assert res["correction_profile_compatible"] is False
    assert res["correction_profile_source"] == "hybrid"
    assert len(res["hourly_correction"]) == 24


def test_validation_supports_hourly_slots_and_36h_horizon():
    """P2: Raster und Horizont sind frei wählbar; jeder Fold ist vollständig."""
    cfg = make_config()
    cfg.general.slot_minutes = 60
    now = pd.Timestamp("2026-06-10 12:00", tz=cfg.general.timezone)
    idx = pd.date_range(now - pd.Timedelta(days=100), now,
                        freq="1h", inclusive="left")
    hour = idx.hour.to_numpy()
    hist = pd.Series(500.0 + 200.0 * ((hour >= 7) & (hour < 22)), index=idx)
    res = validate_forecast_series(cfg, hist, None, None, now, folds=3,
                                   horizon_hours=36, min_train_days=30)
    assert res is not None and res["folds"] == 3
    assert all(m["gesamt"]["n"] == 3 * 36 for m in res["methods"].values())
