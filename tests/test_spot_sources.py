"""Preisquellen-Ausfall am echten Cache-Einstieg, ohne Netz/Live-Datenbank."""
import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pandas as pd
import pytest

from ems import energycharts, main, smard
from ems.local_history import read_spot, write_spot
from tests.test_synthetic import make_config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = make_config()
    cfg.tariff.enabled = True
    cfg.tariff.type = "dynamic"
    cfg.tariff.bidding_zone = "DE-LU"
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "spot.sqlite")
    class Clock(pd.Timestamp):
        @classmethod
        def now(cls, tz=None):
            return pd.Timestamp("2026-10-05T09:00:00+02:00").tz_convert(tz)
    monkeypatch.setattr(main, "pd", SimpleNamespace(Timestamp=Clock, DateOffset=pd.DateOffset))
    monkeypatch.setattr(main, "_last_spot_fetch", 0.0)
    return cfg


@pytest.mark.parametrize("failure", [TimeoutError("timeout"),
    urllib.error.HTTPError("https://api.energy-charts.info/price", 503,
                           "Unavailable", {}, None)])
def test_refresh_spot_outage_uses_smard(cfg, monkeypatch, failure):
    today = pd.Timestamp("2026-10-05", tz="Europe/Berlin")
    end = today + pd.DateOffset(days=1)
    grid = pd.date_range(today, end, freq="15min", inclusive="left")
    week = today - pd.DateOffset(days=today.weekday())
    stamp = int(week.timestamp() * 1000)

    def fail(*args):
        raise failure

    def response(url, timeout):
        assert url.startswith("https://www.smard.de/app/chart_data/4169/DE-LU/")
        data = ({"timestamps": [stamp]} if "index_" in url else
                {"series": [[int(t.timestamp() * 1000), -12.3] for t in grid]})
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(main, "_last_spot_fetch", 0.0)
    monkeypatch.setattr(energycharts, "fetch_spot", fail)
    monkeypatch.setattr(urllib.request, "urlopen", response)
    main._refresh_spot(cfg)
    actual = read_spot(cfg.e3dc_rscp.history_db_path, today, end, "Europe/Berlin")
    assert len(actual) == len(grid) and actual.notna().all()
    assert list(actual) == pytest.approx([-1.23] * len(grid))


def test_primary_wins_without_smard_request(cfg, monkeypatch):
    prices = {"2026-10-04T22:00:00+00:00": 12.34}
    monkeypatch.setattr(energycharts, "fetch_spot", lambda *args: prices)
    monkeypatch.setattr(smard, "fetch_spot", lambda *args: pytest.fail("unexpected fallback"))
    main._refresh_spot(cfg)
    start = pd.Timestamp(next(iter(prices)))
    assert read_spot(cfg.e3dc_rscp.history_db_path, start,
                     start + pd.Timedelta(minutes=15), "UTC").iloc[0] == 12.34


def test_empty_primary_falls_back_and_recovered_primary_overwrites(cfg, monkeypatch):
    stamp = "2026-10-04T22:00:00+00:00"
    primary = {}
    calls = []
    monkeypatch.setattr(energycharts, "fetch_spot", lambda *args: primary)
    def fallback(*args):
        calls.append(args)
        return {stamp: -1.0}
    monkeypatch.setattr(smard, "fetch_spot", fallback)
    main._refresh_spot(cfg)
    start = pd.Timestamp(stamp)
    def cached():
        return read_spot(cfg.e3dc_rscp.history_db_path, start,
                         start + pd.Timedelta(minutes=15), "UTC").iloc[0]
    assert cached() == -1.0
    primary[stamp] = 2.0
    main._refresh_spot(cfg)  # throttled
    assert cached() == -1.0
    monkeypatch.setattr(main, "_last_spot_fetch", 0.0)
    main._refresh_spot(cfg)
    assert cached() == 2.0 and len(calls) == 1
    assert calls[0] == ("DE-LU", "2026-10-02", "2026-10-07")


@pytest.mark.parametrize("empty", [True, False])
def test_both_sources_unavailable_preserves_cache_and_throttles(cfg, monkeypatch, empty):
    stamp = "2026-10-04T22:00:00+00:00"
    write_spot(cfg.e3dc_rscp.history_db_path, {stamp: 3.5})
    calls = []
    def unavailable(*args):
        calls.append(args)
        if empty:
            return {}
        raise TimeoutError("offline")
    monkeypatch.setattr(energycharts, "fetch_spot", unavailable)
    monkeypatch.setattr(smard, "fetch_spot", unavailable)
    main._refresh_spot(cfg)
    main._refresh_spot(cfg)
    assert len(calls) == 2
    start = pd.Timestamp(stamp)
    assert read_spot(cfg.e3dc_rscp.history_db_path, start,
                     start + pd.Timedelta(minutes=15), "UTC").iloc[0] == 3.5


@pytest.mark.parametrize("disabled,fixed,zone", [(True, False, "DE-LU"),
    (False, True, "DE-LU"), (False, False, "FR")])
def test_no_german_fallback_for_disabled_fixed_or_other_zone(cfg, monkeypatch, disabled, fixed, zone):
    cfg.tariff.enabled = not disabled
    cfg.tariff.type = "fixed" if fixed else "dynamic"
    cfg.tariff.bidding_zone = zone
    def primary(*args):
        assert not disabled and not fixed
        raise TimeoutError("offline")
    monkeypatch.setattr(energycharts, "fetch_spot", primary)
    monkeypatch.setattr(smard, "fetch_spot", lambda *args: pytest.fail("unexpected SMARD"))
    main._refresh_spot(cfg)
