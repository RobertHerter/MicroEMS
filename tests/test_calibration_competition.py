from __future__ import annotations

import numpy as np
import pandas as pd

from ems.calibration_competition import (
    apply_load_profile, compare_intervals, compare_point_forecasts)


TZ = "Europe/Berlin"


def _series(days=7):
    idx = pd.date_range(
        "2026-07-01", periods=days * 96, freq="15min", tz=TZ)
    hour = idx.hour + idx.minute / 60.0
    actual = pd.Series(
        600.0 + 500.0 * ((hour >= 17) & (hour < 22)), index=idx)
    return idx, actual


def test_better_challenger_is_promoted():
    _, actual = _series()
    champion = actual * 1.20
    challenger = actual * 1.03
    result = compare_point_forecasts(
        actual, champion, challenger, TZ, min_samples=96,
        min_improvement_percent=1.0)
    assert result["promote"] is True
    assert result["status"] == "promoted"
    assert result["improvement_percent"] > 50.0


def test_segment_regression_keeps_champion():
    idx, actual = _series()
    champion = actual * 1.10
    challenger = actual.copy()
    morning = (idx.hour >= 6) & (idx.hour < 12)
    challenger.loc[morning] = actual.loc[morning] * 1.30
    result = compare_point_forecasts(
        actual, champion, challenger, TZ, min_samples=96,
        min_improvement_percent=1.0,
        max_segment_degradation_pct=5.0)
    assert result["promote"] is False
    assert result["status"] == "held"
    assert "Teilzeitraum" in result["reason"]


def test_insufficient_holdout_never_replaces_champion():
    idx = pd.date_range("2026-07-01", periods=12, freq="15min", tz=TZ)
    actual = pd.Series(1000.0, index=idx)
    result = compare_point_forecasts(
        actual, actual * 2.0, actual, TZ, min_samples=96)
    assert result["promote"] is False
    assert result["status"] == "insufficient"


def test_first_valid_profile_becomes_initial_champion():
    _, actual = _series()
    result = compare_point_forecasts(
        actual, actual * 1.5, actual * 1.1, TZ,
        champion_available=False)
    assert result["promote"] is True
    assert "erster belastbarer" in result["reason"]


def test_interval_challenger_uses_holdout_score():
    ratios = np.tile([0.80, 0.90, 1.00, 1.10, 1.20], 40)
    result = compare_intervals(
        ratios, 0.95, 1.05, 0.80, 1.20,
        min_samples=96, min_improvement_percent=1.0)
    assert result["promote"] is True
    assert result["challenger"]["score"] < result["champion"]["score"]


def test_load_profile_uses_hour_and_global_fallback():
    idx = pd.date_range("2026-07-01 05:00", periods=2, freq="1h", tz=TZ)
    raw = pd.Series(1000.0, index=idx)
    corrected = apply_load_profile(
        raw, {"load_hourly": {"5": 0.8}, "load_global": 1.1}, TZ)
    assert corrected.iloc[0] == 800.0
    assert corrected.iloc[1] == 1100.0
