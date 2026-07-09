"""Tests der Temperatur-Anbindung (Open-Meteo -> lokale SQLite)."""
from __future__ import annotations

import pandas as pd

from ems import weather
from ems.local_history import read_temperature, write_temperature

TZ = "Europe/Berlin"


def test_to_map_key_format():
    payload = {"hourly": {"time": ["2026-07-01T00:00", "2026-07-01T01:00"],
                          "temperature_2m": [12.0, None]}}
    m = weather._to_map(payload)
    # None-Werte übersprungen, Schlüssel als UTC-ISO
    assert m == {"2026-07-01T00:00:00+00:00": 12.0}


def test_temperature_roundtrip_and_resample(tmp_path):
    db = str(tmp_path / "w.sqlite")
    # 3 Stundenwerte 10/20/30 °C
    base = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    mapping = {(base + pd.Timedelta(hours=i)).isoformat(): 10.0 * (i + 1)
               for i in range(3)}
    assert write_temperature(db, mapping) == 3
    start = pd.Timestamp("2026-07-01 00:00", tz=TZ).tz_convert(TZ)
    # lokale Zeit: UTC 00:00 = 02:00 MESZ
    s = read_temperature(db, base.tz_convert(TZ), (base + pd.Timedelta(hours=2)).tz_convert(TZ),
                         TZ, "15min")
    assert len(s) == 8            # 2 h * 4 Slots
    assert abs(s.iloc[0] - 10.0) < 1e-6      # erster Stundenwert
    # Interpolation Mitte zwischen 10 und 20 -> ~15 bei +30 min
    assert 12.0 < s.iloc[2] < 18.0
    assert str(s.index.tz) == TZ


def test_read_temp_uses_open_meteo(monkeypatch, tmp_path):
    """_read_temp holt bei weather.enabled von Open-Meteo (gemockt) und liest lokal."""
    from ems import main as emsmain
    from tests.test_synthetic import make_config
    cfg = make_config()
    cfg.weather.enabled = True
    cfg.weather.latitude, cfg.weather.longitude = 47.85, 12.07
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "w.sqlite")
    now = pd.Timestamp("2026-07-01 12:00", tz=TZ)
    # tz-bewusste Endpunkte -> kein tz= (sonst Konflikt)
    hrs = pd.date_range((now - pd.Timedelta(hours=3)).tz_convert("UTC"),
                        (now + pd.Timedelta(hours=6)).tz_convert("UTC"), freq="1h")
    fake = {t.isoformat(): 15.0 for t in hrs}
    monkeypatch.setattr(emsmain, "_last_temp_fetch", 0.0)
    import ems.weather as w
    monkeypatch.setattr(w, "fetch_forecast", lambda *a, **k: fake)
    s = emsmain._read_temp(None, cfg, now, now + pd.Timedelta(hours=4))
    assert s is not None and len(s) == 16 and abs(s.iloc[0] - 15.0) < 1e-6
