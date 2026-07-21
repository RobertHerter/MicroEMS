from types import SimpleNamespace

import pandas as pd
import pytest

import backtest
from ems.local_history import (read_optimizer_forecast_asof,
                               write_optimizer_forecast_archive)


def test_optimizer_archive_reads_one_snapshot_known_at_origin(tmp_path):
    db = str(tmp_path / "history.sqlite")
    idx = pd.date_range("2026-07-18 12:00", periods=4, freq="15min", tz="UTC")
    first = pd.Series([100.0, 200.0, 300.0, 400.0], index=idx)
    second = first + 1000.0
    estimated = pd.Series([False, True, True, False], index=idx)

    write_optimizer_forecast_archive(
        db, pd.Timestamp("2026-07-18 11:00", tz="UTC"),
        {"house_load_w": first, "price_ct_kwh": first / 10},
        {"price_ct_kwh": estimated})
    write_optimizer_forecast_archive(
        db, pd.Timestamp("2026-07-18 11:45", tz="UTC"),
        {"house_load_w": second, "price_ct_kwh": second / 10})

    issue, frame = read_optimizer_forecast_asof(
        db, pd.Timestamp("2026-07-18 11:30", tz="UTC"), idx[0],
        idx[-1] + pd.Timedelta(minutes=15), "Europe/Berlin")

    assert issue == pd.Timestamp("2026-07-18 13:00", tz="Europe/Berlin")
    assert frame["house_load_w"].tolist() == first.tolist()
    assert frame["price_ct_kwh_estimated"].tolist() == estimated.tolist()


def test_historical_plan_rejects_stale_snapshot(monkeypatch):
    day = pd.Timestamp("2026-07-18", tz="Europe/Berlin")
    idx = pd.date_range(day, periods=192, freq="15min")
    frame = pd.DataFrame({
        "house_load_w": 1000.0,
        "pv_w": 0.0,
        "price_ct_kwh": 30.0,
        "feedin_ct_kwh": 8.0,
    }, index=idx)
    seen = {}

    def reader(*args, **kwargs):
        seen["cutoff"] = args[1]
        return day - pd.Timedelta(hours=2), frame

    monkeypatch.setattr(backtest, "read_optimizer_forecast_asof", reader)
    config = SimpleNamespace(
        general=SimpleNamespace(
            timezone="Europe/Berlin", slot_minutes=15,
            optimization_horizon_hours=48, run_interval_minutes=15),
        e3dc_rscp=SimpleNamespace(history_db_path="unused"),
    )

    with pytest.raises(backtest.HistoricalForecastUnavailable, match="zu alt"):
        backtest._historical_day_plan(object(), config, day)
    assert seen["cutoff"] == day + pd.Timedelta(minutes=5)
