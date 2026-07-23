"""Tests der Temperatur-/Solar-Einstrahlungs-Anbindung (Open-Meteo -> lokale
SQLite). Beide Signale kommen aus DEMSELBEN Open-Meteo-Call (fetch_forecast/
fetch_archive liefern ein Tupel (temp_map, radiation_map))."""
from __future__ import annotations

import pandas as pd

from ems import weather
from ems.local_history import (read_radiation, read_temperature,
                               read_weather_forecast_asof, write_radiation,
                               write_temperature,
                               write_weather_forecast_archive)

TZ = "Europe/Berlin"


def test_to_map_key_format():
    payload = {"hourly": {"time": ["2026-07-01T00:00", "2026-07-01T01:00"],
                          "temperature_2m": [12.0, None]}}
    m = weather._to_map(payload)
    # None-Werte übersprungen, Schlüssel als UTC-ISO
    assert m == {"2026-07-01T00:00:00+00:00": 12.0}


def test_to_map_selects_field():
    """_to_map liest ein beliebiges hourly-Feld (z.B. shortwave_radiation)."""
    payload = {"hourly": {"time": ["2026-07-01T00:00", "2026-07-01T01:00"],
                          "temperature_2m": [12.0, 13.0],
                          "shortwave_radiation": [0.0, 250.0]}}
    m = weather._to_map(payload, "shortwave_radiation")
    assert m == {"2026-07-01T00:00:00+00:00": 0.0,
                 "2026-07-01T01:00:00+00:00": 250.0}


def test_radiation_roundtrip_and_resample(tmp_path):
    """write_radiation/read_radiation spiegeln write_temperature/read_temperature
    (gleiche Zeitraster-Interpolation) und clippen nie unter 0."""
    db = str(tmp_path / "w.sqlite")
    base = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    mapping = {(base + pd.Timedelta(hours=i)).isoformat(): 100.0 * i for i in range(3)}
    assert write_radiation(db, mapping) == 3
    s = read_radiation(db, base.tz_convert(TZ), (base + pd.Timedelta(hours=2)).tz_convert(TZ),
                       TZ, "15min")
    assert len(s) == 8
    assert abs(s.iloc[0] - 0.0) < 1e-6
    assert (s >= 0.0).all()


def test_temperature_roundtrip_and_resample(tmp_path):
    db = str(tmp_path / "w.sqlite")
    # 3 Stundenwerte 10/20/30 °C
    base = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    mapping = {(base + pd.Timedelta(hours=i)).isoformat(): 10.0 * (i + 1)
               for i in range(3)}
    assert write_temperature(db, mapping) == 3
    # lokale Zeit: UTC 00:00 = 02:00 MESZ
    s = read_temperature(db, base.tz_convert(TZ), (base + pd.Timedelta(hours=2)).tz_convert(TZ),
                         TZ, "15min")
    assert len(s) == 8            # 2 h * 4 Slots
    assert abs(s.iloc[0] - 10.0) < 1e-6      # erster Stundenwert
    # Interpolation Mitte zwischen 10 und 20 -> ~15 bei +30 min
    assert 12.0 < s.iloc[2] < 18.0
    assert str(s.index.tz) == TZ


def test_reader_grid_is_slot_aligned_despite_offset_start(tmp_path):
    """Regression (vgl. pool_calibration): wird ein aus now abgeleitetes,
    NICHT gefloortes start/end übergeben (Sekunden-Offset), muss das Raster
    trotzdem bündig auf den Slot-Grenzen liegen - sonst trifft ein exaktes
    reindex(grid) beim Verbraucher (Forecaster/Kalibrierung) keinen Punkt und
    Temperatur/Strahlung werden still komplett NaN."""
    db = str(tmp_path / "w.sqlite")
    base = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    write_temperature(db, {(base + pd.Timedelta(hours=i)).isoformat(): 10.0 * (i + 1)
                           for i in range(4)})
    write_radiation(db, {(base + pd.Timedelta(hours=i)).isoformat(): 100.0 * i
                         for i in range(4)})
    # start/end mit Sekunden-/Minuten-Offset (wie pd.Timestamp.now())
    off = pd.Timedelta(minutes=7, seconds=42, microseconds=123456)
    start = base.tz_convert(TZ) + off
    end = (base + pd.Timedelta(hours=3)).tz_convert(TZ) + off
    for reader in (read_temperature, read_radiation):
        s = reader(db, start, end, TZ, "15min")
        assert not s.empty and s.notna().any()
        # jeder Index-Punkt liegt exakt auf einer 15-min-Grenze (kein Offset)
        assert (s.index == s.index.floor("15min")).all(), list(s.index[:3])
        # und deckt sich mit einem unabhängig gefloorten Verbraucher-Raster
        grid = pd.date_range(start.floor("15min"), periods=4, freq="15min")
        assert not s.reindex(grid).isna().all()


def test_weather_archive_excludes_past_and_selects_asof(tmp_path):
    db = str(tmp_path / "w.sqlite")
    target = pd.Timestamp("2026-07-10 12:00", tz="UTC")
    issue1 = target - pd.Timedelta(hours=6)
    issue2 = target - pd.Timedelta(hours=2)
    past = issue1 - pd.Timedelta(hours=1)
    assert write_weather_forecast_archive(
        db, issue1,
        {past.isoformat(): 9.0, target.isoformat(): 10.0},
        {past.isoformat(): 50.0, target.isoformat(): 100.0}) == 1
    write_weather_forecast_archive(
        db, issue2, {target.isoformat(): 20.0}, {target.isoformat(): 200.0})
    end = target + pd.Timedelta(hours=1)
    old = read_weather_forecast_asof(
        db, issue1 + pd.Timedelta(hours=1), target, end, "UTC", "15min")
    new = read_weather_forecast_asof(
        db, issue2 + pd.Timedelta(hours=1), target, end, "UTC", "15min")
    assert old.iloc[0] == 10.0 and new.iloc[0] == 20.0


def test_weather_reader_does_not_interpolate_multi_hour_outage(tmp_path):
    db = str(tmp_path / "w.sqlite")
    base = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    write_temperature(db, {
        base.isoformat(): 10.0,
        (base + pd.Timedelta(hours=6)).isoformat(): 20.0})
    s = read_temperature(db, base, base + pd.Timedelta(hours=6), "UTC", "15min")
    assert s.isna().any(), "Mehrstündige Wetterlücke wurde als Messdaten getarnt"


def _fake_cfg_and_window(tmp_path):
    from tests.test_synthetic import make_config
    cfg = make_config()
    cfg.weather.enabled = True
    cfg.weather.latitude, cfg.weather.longitude = 47.85, 12.07
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "w.sqlite")
    now = pd.Timestamp("2026-07-01 12:00", tz=TZ)
    hrs = pd.date_range((now - pd.Timedelta(hours=3)).tz_convert("UTC"),
                        (now + pd.Timedelta(hours=6)).tz_convert("UTC"), freq="1h")
    return cfg, now, hrs


def test_read_temp_uses_open_meteo(monkeypatch, tmp_path):
    """_read_temp holt bei weather.enabled von Open-Meteo (gemockt, EIN Tupel-
    Rückgabewert temp+radiation) und liest die Temperatur lokal."""
    from ems import main as emsmain
    cfg, now, hrs = _fake_cfg_and_window(tmp_path)
    fake_temp = {t.isoformat(): 15.0 for t in hrs}
    fake_rad = {t.isoformat(): 200.0 for t in hrs}
    monkeypatch.setattr(emsmain, "_last_weather_fetch", 0.0)
    import ems.weather as w
    monkeypatch.setattr(w, "fetch_forecast", lambda *a, **k: (fake_temp, fake_rad))
    s = emsmain._read_temp(None, cfg, now, now + pd.Timedelta(hours=4))
    assert s is not None and len(s) == 16 and abs(s.iloc[0] - 15.0) < 1e-6


def test_read_solar_uses_open_meteo(monkeypatch, tmp_path):
    """_read_solar liest die Strahlung aus demselben gemockten Open-Meteo-Call."""
    from ems import main as emsmain
    cfg, now, hrs = _fake_cfg_and_window(tmp_path)
    fake_temp = {t.isoformat(): 15.0 for t in hrs}
    fake_rad = {t.isoformat(): 321.0 for t in hrs}
    monkeypatch.setattr(emsmain, "_last_weather_fetch", 0.0)
    import ems.weather as w
    monkeypatch.setattr(w, "fetch_forecast", lambda *a, **k: (fake_temp, fake_rad))
    s = emsmain._read_solar(cfg, now, now + pd.Timedelta(hours=4))
    assert s is not None and len(s) == 16 and abs(s.iloc[0] - 321.0) < 1e-6


def test_read_temp_and_solar_share_one_http_call(monkeypatch, tmp_path):
    """Ein Zyklus ruft _read_temp UND _read_solar auf - fetch_forecast (der
    HTTP-Call) darf dabei nur EINMAL ausgeführt werden (geteilter Cache-Refresh,
    5-Minuten-Gate in _refresh_weather_cache)."""
    from ems import main as emsmain
    cfg, now, hrs = _fake_cfg_and_window(tmp_path)
    fake_temp = {t.isoformat(): 15.0 for t in hrs}
    fake_rad = {t.isoformat(): 200.0 for t in hrs}
    calls = []
    monkeypatch.setattr(emsmain, "_last_weather_fetch", 0.0)
    import ems.weather as w

    def _counting_fetch(*a, **k):
        calls.append(1)
        return fake_temp, fake_rad
    monkeypatch.setattr(w, "fetch_forecast", _counting_fetch)
    emsmain._read_temp(None, cfg, now, now + pd.Timedelta(hours=4))
    emsmain._read_solar(cfg, now, now + pd.Timedelta(hours=4))
    assert len(calls) == 1, "fetch_forecast wurde mehr als einmal aufgerufen"
