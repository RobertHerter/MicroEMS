"""Hold-out der Lastkalibrierung: der Prognosehorizont folgt der Zeitspanne."""
from __future__ import annotations

import pandas as pd

import kalibrierung
from tests.test_synthetic import make_config


class _Repo:
    def signal_available(self, name):
        return True


def test_holdout_horizon_spans_the_test_window_despite_gaps(monkeypatch):
    """Fehlen im Testzeitraum Ist-Slots, darf die Prognose nicht entsprechend
    frueher enden: ``len(actual_test)`` zaehlte nur vorhandene Slots, und es
    fehlte genau das Ende - das Befoerderungsfenster der Champion-/Challenger-
    Pruefung."""
    cfg = make_config()
    cfg.general.slot_minutes = 15
    cfg.e3dc_rscp.history_source = True
    tz = cfg.general.timezone
    now = pd.Timestamp("2026-06-10 00:00", tz=tz)
    index = pd.date_range(now - pd.Timedelta(days=60), now, freq="15min",
                          inclusive="left")
    hist = pd.Series(500.0, index=index)
    # drei Tage Ausfall mitten im Testzeitraum
    luecke = ((index >= now - pd.Timedelta(days=20))
              & (index < now - pd.Timedelta(days=17)))
    hist = hist[~luecke]
    gesehen = {}

    class _Forecaster:
        def __init__(self, _cfg):
            self.load_hourly = None

        def forecast(self, train, start, horizon, **_kw):
            gesehen["horizon"] = horizon
            idx = pd.date_range(pd.Timestamp(start).tz_convert("UTC"),
                                periods=horizon, freq="15min")
            return pd.Series(480.0, index=idx.tz_convert(tz))

    monkeypatch.setattr(kalibrierung, "_load_hist", lambda *a, **k: hist)
    monkeypatch.setattr(kalibrierung, "_temp_hist", lambda *a, **k: None)
    monkeypatch.setattr(kalibrierung, "_pv_forecast_hist",
                        lambda *a, **k: (None, "test"))
    monkeypatch.setattr(kalibrierung, "LoadForecaster", _Forecaster)

    res = kalibrierung.calibrate_load(_Repo(), cfg, now, lookback_days=60,
                                      test_days=30, promotion_days=14)
    assert gesehen["horizon"] == 30 * 96          # nicht 27 * 96
    rahmen = res["_promotion_frame"]
    # das Befoerderungsfenster reicht bis zum letzten Ist-Slot vor ``now``
    assert rahmen.index.max() == hist.index.max()
    assert res["promotion_samples"] == 14 * 96


def test_horizon_helper_rounds_to_the_slot_grid():
    now = pd.Timestamp("2026-06-10 00:00", tz="Europe/Berlin")
    start = now - pd.Timedelta(days=1) + pd.Timedelta(minutes=7)
    # die Prognose beginnt am abgerundeten Slot - von dort bis now
    assert kalibrierung._test_horizon_slots(start, now, 15) == 96
    assert kalibrierung._test_horizon_slots(now, now, 15) == 1
