"""SMARD-Zeitraster, Datenlücken und Umrechnung ohne Netzwerkzugriff."""
import pandas as pd
import pytest

from ems import smard


def _series(day):
    start = pd.Timestamp(day, tz="Europe/Berlin")
    grid = pd.date_range(start, start + pd.DateOffset(days=1),
                         freq="15min", inclusive="left")
    return [[int(t.timestamp() * 1000), (i - 40) * 1.23] for i, t in enumerate(grid)]


def _mock(monkeypatch, series):
    stamp = series[0][0]
    calls = []
    def get(url):
        calls.append(url)
        return {"timestamps": [stamp]} if "index_" in url else {"series": series}
    monkeypatch.setattr(smard, "_get", get)
    return calls


@pytest.mark.parametrize("day,count", [("2026-10-05", 96),
    ("2026-03-29", 92), ("2026-10-25", 100)])
def test_full_day_conversion_and_dst(monkeypatch, day, count):
    series = _series(day)
    _mock(monkeypatch, series)
    result = smard.fetch_spot("DE-LU", day, day)
    assert len(result) == count
    assert list(result.values()) == pytest.approx([v / 10 for t, v in series])
    times = pd.to_datetime(list(result), utc=True)
    assert (times[1:] - times[:-1] == pd.Timedelta(minutes=15)).all()
    assert all(t.tz_convert("Europe/Berlin").date().isoformat() == day for t in times)


def test_week_boundary_uses_index_and_filters_requested_dates(monkeypatch):
    sunday, monday = _series("2026-10-04"), _series("2026-10-05")
    before = int(pd.Timestamp("2026-09-28", tz="Europe/Berlin").timestamp() * 1000)
    after = int(pd.Timestamp("2026-10-12", tz="Europe/Berlin").timestamp() * 1000)
    calls = []
    def get(url):
        calls.append(url)
        if "index_" in url:
            return {"timestamps": [after, monday[0][0], before]}
        if str(before) in url:
            return {"series": _series("2026-10-03") + sunday}
        return {"series": monday + _series("2026-10-06")}
    monkeypatch.setattr(smard, "_get", get)
    result = smard.fetch_spot("DE-LU", "2026-10-04", "2026-10-05")
    assert len(result) == 192 and len(calls) == 3
    assert not any(str(after) in url for url in calls)


@pytest.mark.parametrize("missing", ["null", "absent"])
def test_incomplete_day_is_not_filled_but_other_complete_day_survives(monkeypatch, missing):
    first, second = _series("2026-10-05"), _series("2026-10-06")
    if missing == "null":
        first[40][1] = None
    else:
        del first[40]
    _mock(monkeypatch, first + second)
    result = smard.fetch_spot("DE-LU", "2026-10-05", "2026-10-07")
    assert len(result) == 96
    assert all(pd.Timestamp(t).tz_convert("Europe/Berlin").day == 6 for t in result)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "12.5", True])
def test_invalid_prices_are_rejected(monkeypatch, bad):
    series = _series("2026-10-05")
    series[1][1] = bad
    _mock(monkeypatch, series)
    with pytest.raises(ValueError):
        smard.fetch_spot("DE-LU", "2026-10-05", "2026-10-05")


@pytest.mark.parametrize("kind", ["offset", "conflict"])
def test_invalid_timestamps_and_conflicting_duplicates(monkeypatch, kind):
    series = _series("2026-10-05")
    if kind == "offset":
        series[1][0] += 1000
    else:
        series.append([series[1][0], 999.0])
    _mock(monkeypatch, series)
    with pytest.raises(ValueError):
        smard.fetch_spot("DE-LU", "2026-10-05", "2026-10-05")


def test_unpublished_and_empty_index(monkeypatch):
    series = _series("2026-10-05")
    for row in series:
        row[1] = None
    _mock(monkeypatch, series)
    assert smard.fetch_spot("DE-LU", "2026-10-05", "2026-10-05") == {}
    monkeypatch.setattr(smard, "_get", lambda url: {"timestamps": []})
    assert smard.fetch_spot("DE-LU", "2026-10-05", "2026-10-05") == {}


def test_unsupported_zone_and_reversed_dates_make_no_request(monkeypatch):
    monkeypatch.setattr(smard, "_get", lambda url: pytest.fail("unexpected network"))
    with pytest.raises(ValueError, match="DE-LU"):
        smard.fetch_spot("FR", "2026-10-05", "2026-10-05")
    with pytest.raises(ValueError, match="Startdatum"):
        smard.fetch_spot("DE-LU", "2026-10-06", "2026-10-05")
