"""Tests der Solcast-Anbindung: Parsen, Kombinieren (sum/mean), Weiche, Scheduler."""
from __future__ import annotations

import io
import json

import pandas as pd

from ems import solcast
from ems.config import SolcastConfig, SolcastSource
from ems.local_history import (last_solcast_fetch, read_pv_forecast,
                               solcast_calls_since, write_pv_forecast)


def _cfg(tmp_path, **skw):
    from tests.test_synthetic import make_config
    c = make_config()
    c.e3dc_rscp.history_db_path = str(tmp_path / "pv.sqlite")
    c.solcast = SolcastConfig(enabled=True, **skw)
    return c


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_fetch_forecast_parse(monkeypatch):
    payload = {"forecasts": [
        {"pv_estimate": 5.0, "pv_estimate10": 4.0, "pv_estimate90": 6.0,
         "period_end": "2026-07-09T10:30:00.0000000Z", "period": "PT30M"},
        {"pv_estimate": 0.0, "period_end": "2026-07-09T11:00:00.0000000Z", "period": "PT30M"},
    ]}
    monkeypatch.setattr(solcast.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(json.dumps(payload).encode()))
    m = solcast.fetch_forecast("KEY", "res-1")
    # period_end 10:30, Periode 30min -> Start 10:00; kW->W
    assert m["2026-07-09T10:00:00+00:00"] == (5000.0, 4000.0, 6000.0)
    assert m["2026-07-09T10:30:00+00:00"][0] == 0.0


def test_combine_sum_vs_mean(tmp_path):
    db = str(tmp_path / "pv.sqlite")
    base = pd.Timestamp("2026-07-09 08:00", tz="UTC")
    a = {(base + pd.Timedelta(minutes=30 * i)).isoformat(): (1000.0, 800.0, 1200.0)
         for i in range(3)}
    b = {(base + pd.Timedelta(minutes=30 * i)).isoformat(): (3000.0, 2000.0, 4000.0)
         for i in range(3)}
    write_pv_forecast(db, "A", a)
    write_pv_forecast(db, "B", b)
    tz = "Europe/Berlin"
    s0, e0 = base.tz_convert(tz), (base + pd.Timedelta(hours=1)).tz_convert(tz)
    summ = read_pv_forecast(db, s0, e0, tz, 15, "sum", "pv")
    mean = read_pv_forecast(db, s0, e0, tz, 15, "mean", "pv")
    assert abs(summ.iloc[0] - 4000.0) < 1e-6      # 1000 + 3000
    assert abs(mean.iloc[0] - 2000.0) < 1e-6      # (1000 + 3000)/2
    # 30-min -> 15-min gehalten: Slot +15min == Slot 0
    assert abs(summ.iloc[1] - summ.iloc[0]) < 1e-6
    # p10-Spalte wird ebenfalls kombiniert
    assert abs(read_pv_forecast(db, s0, e0, tz, 15, "sum", "p10").iloc[0] - 2800.0) < 1e-6


def test_read_pv_signal_weiche(tmp_path):
    cfg = _cfg(tmp_path, combine="sum",
               sources=[SolcastSource("K", "res-1")])
    db = cfg.e3dc_rscp.history_db_path
    base = pd.Timestamp("2026-07-09 08:00", tz="UTC")
    write_pv_forecast(db, "res-1",
                      {(base + pd.Timedelta(minutes=30 * i)).isoformat(): (500.0 * i, 0.0, 0.0)
                       for i in range(4)})
    tz = "Europe/Berlin"
    s = solcast.read_pv_signal(cfg, None, "pv_forecast",  # repo=None: darf nicht genutzt werden
                               base.tz_convert(tz), (base + pd.Timedelta(hours=2)).tz_convert(tz))
    assert not s.empty and s.iloc[0] == 0.0

    # solcast aus -> InfluxDB-Pfad (repo.read_slots)
    cfg.solcast.enabled = False

    class FakeRepo:
        def read_slots(self, sig, a, b):
            return pd.Series([1.0], index=pd.to_datetime([a]))
    assert solcast.read_pv_signal(cfg, FakeRepo(), "pv_forecast", base, base).iloc[0] == 1.0


def test_available():
    from tests.test_synthetic import make_config
    c = make_config()
    c.solcast = SolcastConfig(enabled=True)

    class FakeRepo:
        def signal_available(self, s):
            return False
    assert solcast.available(c, FakeRepo(), "pv_forecast_p10") is True
    assert solcast.available(c, FakeRepo(), "temperature") is False


def test_refresh_budget_and_spacing(tmp_path, monkeypatch):
    """Erster Aufruf holt; sofortiger zweiter nicht (Spacing); Budget wird geachtet."""
    cfg = _cfg(tmp_path, combine="sum", calls_per_key_per_day=1,
               window_start_hour=0, window_end_hour=24,
               sources=[SolcastSource("K", "res-1")])
    db = cfg.e3dc_rscp.history_db_path
    calls = {"n": 0}

    def fake_fetch(key, rid, **k):
        calls["n"] += 1
        base = pd.Timestamp.now(tz="UTC").floor("30min")
        return {(base + pd.Timedelta(minutes=30 * i)).isoformat(): (100.0, 50.0, 150.0)
                for i in range(4)}
    monkeypatch.setattr(solcast, "fetch_forecast", fake_fetch)
    solcast._error_cooldown.clear()

    solcast.refresh(cfg)
    assert calls["n"] == 1                        # erster Abruf
    since = pd.Timestamp.now(tz="UTC").normalize().isoformat()
    assert solcast_calls_since(db, "K", since) == 1
    assert last_solcast_fetch(db, "K", "res-1") is not None

    solcast.refresh(cfg)                          # Budget=1 erschöpft -> kein Abruf
    assert calls["n"] == 1
