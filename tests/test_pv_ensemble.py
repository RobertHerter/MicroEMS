"""Tests für Gewichtung und Unsicherheitsband des PV-lib-Ensembles."""
from __future__ import annotations

import pandas as pd

from ems import local_history, pv_ensemble


def _seed_actual(db, target, actual):
    con = local_history._con(db)
    con.execute(
        "INSERT OR REPLACE INTO actuals(ts, pv_w) VALUES(?,?)",
        (target.tz_convert("UTC").isoformat(), float(actual)))
    con.commit()
    con.close()


def test_learns_horizon_weight_from_rolling_origin_errors(tmp_path):
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    sources = {"good": ["member-good"], "bad": ["member-bad"]}
    for day in range(1, 21):
        target = now - pd.Timedelta(days=day) + pd.Timedelta(hours=12)
        issue = target - pd.Timedelta(hours=3)
        _seed_actual(db, target, 1000.0)
        local_history.write_pv_forecast_archive(
            db, "member-good", issue,
            {target.isoformat(): (1050.0, 900.0, 1200.0)})
        local_history.write_pv_forecast_archive(
            db, "member-bad", issue,
            {target.isoformat(): (1800.0, 1200.0, 2200.0)})

    learned = pv_ensemble.learn_weights(
        db, sources, now, lookback_days=30, min_samples=8,
        horizon_hours=[6, 24, 48], min_weight=0.05)
    short = learned["0-6h"]
    assert short["learned"] and short["n"] == 20
    assert short["weights"]["good"] > short["weights"]["bad"]
    assert short["weights"]["bad"] >= 0.05
    # Kein Training im 6-24-h-Bucket -> neutrale Gewichte.
    assert learned["6-24h"]["weights"] == {"good": 0.5, "bad": 0.5}


def test_residual_quantiles_are_lead_specific(tmp_path):
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    for day in range(1, 21):
        target = now - pd.Timedelta(days=day) + pd.Timedelta(hours=12)
        issue = target - pd.Timedelta(hours=3)
        actual = 800.0 if day <= 10 else 1200.0
        _seed_actual(db, target, actual)
        local_history.write_pv_forecast_archive(
            db, "pvmodel:A", issue,
            {target.isoformat(): (1000.0, 700.0, 1300.0)})

    bands = pv_ensemble.residual_quantiles(
        db, ["pvmodel:A"], now, lookback_days=30, min_samples=8,
        horizon_hours=[6, 24], fallback_low=0.65, fallback_high=1.15)
    assert bands["0-6h"]["learned"] and bands["0-6h"]["n"] == 20
    assert 0.79 <= bands["0-6h"]["q10_ratio"] <= 0.81
    assert 1.19 <= bands["0-6h"]["q90_ratio"] <= 1.21
    assert not bands["6-24h"]["learned"]


def test_combines_model_spread_and_empirical_residuals():
    issue = pd.Timestamp("2026-07-20 09:00", tz="UTC")
    target = (issue + pd.Timedelta(hours=3)).isoformat()
    outputs = {
        "good": {"pvmodel:A": {target: (1000.0, 0.0, 0.0)}},
        "high": {"pvmodel:A": {target: (2000.0, 0.0, 0.0)}},
    }
    weights = {
        "0-6h": {"weights": {"good": 0.75, "high": 0.25},
                 "n": 100, "learned": True},
        "6h+": {"weights": {"good": 0.5, "high": 0.5},
                "n": 0, "learned": False},
    }
    residuals = {
        "0-6h": {"q10_ratio": 0.8, "q90_ratio": 1.2,
                 "n": 100, "learned": True},
        "6h+": {"q10_ratio": 0.65, "q90_ratio": 1.15,
                "n": 0, "learned": False},
    }
    combined, diagnostics = pv_ensemble.combine(
        outputs, weights, residuals, issue, [6])
    point, p10, p90 = combined["pvmodel:A"][target]
    assert point == 1250.0
    assert p10 == 1000.0       # Modellstreuung konservativer als Residuum
    assert p90 == 2000.0
    assert p10 <= point <= p90
    assert diagnostics["slots_by_bucket"]["0-6h"] == 1


def test_member_source_keeps_aggregate_ids_compatible():
    assert pv_ensemble.member_source(
        "dwd_icon", "pvmodel:Ost") == "pvmodel-member:dwd_icon:Ost"
