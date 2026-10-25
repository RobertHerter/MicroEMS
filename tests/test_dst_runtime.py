"""Betriebsraster muss beide Stunden der Herbstumstellung unterscheiden."""
from types import SimpleNamespace

import pandas as pd
import pytest

from ems import main
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


@pytest.mark.parametrize("offset", ["+02:00", "+01:00"])
def test_now_slot_keeps_the_observed_dst_occurrence(monkeypatch, offset):
    now = pd.Timestamp(f"2026-10-25T02:17:31{offset}").tz_convert(TZ)
    class Clock(pd.Timestamp):
        @classmethod
        def now(cls, tz=None):
            return now.tz_convert(tz)
    monkeypatch.setattr(main, "pd", SimpleNamespace(Timestamp=Clock))
    result = main._now_slot(make_config())
    assert result == pd.Timestamp(f"2026-10-25T02:15:00{offset}")
    assert result.tz == now.tz


@pytest.mark.parametrize("utc", ["2026-10-25T01:17Z", "2026-10-25T02:17Z"])
def test_house_history_window_can_end_in_either_repeated_hour(utc):
    now = pd.Timestamp(utc).tz_convert(TZ)
    start, end = main._house_history_refresh_window(now, None, 15, 60, 3)
    assert end == (now.tz_convert("UTC") - pd.Timedelta(hours=1)).floor("15min")
    assert end - start == pd.Timedelta(hours=24)


@pytest.mark.parametrize("day,count", [("2026-03-29", 92), ("2026-10-25", 100)])
def test_slot_rounding_preserves_full_dst_day(day, count):
    from ems.time_utils import floor_time, ceil_time
    start = pd.Timestamp(day, tz=TZ)
    grid = pd.date_range(start, start + pd.DateOffset(days=1), freq="15min", inclusive="left")
    floor = floor_time(grid + pd.Timedelta(seconds=17), "15min")
    ceil = ceil_time(grid + pd.Timedelta(seconds=17), "15min")
    assert len(floor) == count and floor.is_unique and floor.equals(grid)
    assert ceil.equals(grid + pd.Timedelta(minutes=15))


def test_history_archive_keeps_both_repeated_issue_slots(tmp_path):
    from ems.local_history import write_optimizer_forecast_archive, read_optimizer_forecast_origins
    db = str(tmp_path / "hist.sqlite")
    for offset, load in [("+02:00", 500.0), ("+01:00", 900.0)]:
        ts = pd.Timestamp(f"2026-10-25T02:15:00{offset}").tz_convert(TZ)
        idx = pd.date_range(ts, periods=2, freq="15min")
        write_optimizer_forecast_archive(db, ts, {"house_load_w": pd.Series(load, index=idx)})
    # Beide Ausgabestaende liegen auf der UTC-Achse eine Stunde auseinander.
    result = read_optimizer_forecast_origins(db,
        pd.Timestamp("2026-10-25T00:00Z"), pd.Timestamp("2026-10-25T02:00Z"), TZ, 15)
    assert len(result) == 2
    assert list(result["house_load_w"]) == [500.0, 900.0]
    assert result.index[1] - result.index[0] == pd.Timedelta(hours=1)
