"""Tests der freien pvlib-PV-Ertragsprognose (ems/pvforecast.py)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pvlib")

from ems import pvforecast
from ems.config import PvArray, PvModelConfig, WeatherConfig
from tests.test_synthetic import make_config

UTC_DAY = pd.date_range("2026-07-15 00:00", "2026-07-15 23:00", freq="1h", tz="UTC")


def _maps(ghi_peak=850.0, temp=25.0):
    hour = UTC_DAY.hour + UTC_DAY.minute / 60.0
    ghi = np.clip(ghi_peak * np.exp(-((hour - 11) ** 2) / 9), 0, None)
    dni = np.clip(0.88 * ghi_peak * np.exp(-((hour - 11) ** 2) / 10), 0, None)
    dhi = np.clip(0.25 * ghi, 0, None)

    def m(vals):
        return {t.isoformat(): float(v) for t, v in zip(UTC_DAY, vals)}
    return {"shortwave_radiation": m(ghi), "direct_normal_irradiance": m(dni),
            "diffuse_radiation": m(dhi),
            "temperature_2m": m(np.full(len(UTC_DAY), temp)),
            "wind_speed_10m": m(np.full(len(UTC_DAY), 2.0))}


def _cfg(arrays, **pm):
    cfg = make_config()
    cfg.weather = WeatherConfig(enabled=True, latitude=47.85, longitude=12.07)
    cfg.pv_model = PvModelConfig(enabled=True, arrays=arrays, **pm)
    return cfg


def _peak_hour_local(series):
    ts = pd.to_datetime(list(series), utc=True).tz_convert("Europe/Berlin")
    vals = np.array([v[0] for v in series.values()])
    return ts[int(np.argmax(vals))].hour


def test_orientation_shifts_peak_time():
    """Ost-Array spitzt früher als West-Array (POA-Transposition wirkt)."""
    cfg = _cfg([PvArray("Ost", 5.0, 30, 90), PvArray("West", 5.0, 30, 270)])
    out = pvforecast.compute(cfg, _maps())
    assert _peak_hour_local(out["pvmodel:Ost"]) < _peak_hour_local(out["pvmodel:West"])


def test_yield_scales_with_kwp():
    cfg1 = _cfg([PvArray("A", 5.0, 30, 180)])
    cfg2 = _cfg([PvArray("A", 10.0, 30, 180)])
    e1 = sum(v[0] for v in pvforecast.compute(cfg1, _maps())["pvmodel:A"].values())
    e2 = sum(v[0] for v in pvforecast.compute(cfg2, _maps())["pvmodel:A"].values())
    assert abs(e2 - 2 * e1) < 0.02 * e2, "Ertrag nicht linear in kWp"


def test_night_is_zero_and_bands_ordered():
    cfg = _cfg([PvArray("S", 5.0, 30, 180)], p10_uncertainty=0.35,
               p90_uncertainty=0.15)
    out = pvforecast.compute(cfg, _maps())["pvmodel:S"]
    for ts, (pv, p10, p90) in out.items():
        assert p10 <= pv <= p90 + 1e-9
        h = pd.Timestamp(ts).tz_convert("Europe/Berlin").hour
        if h < 4 or h > 22:
            assert pv < 1.0, f"Nacht {h}h liefert {pv:.0f} W"
    assert max(v[0] for v in out.values()) > 2000.0    # tagsüber realer Ertrag


def test_system_loss_reduces_yield():
    base = _cfg([PvArray("S", 5.0, 30, 180)], system_loss=0.0)
    lossy = _cfg([PvArray("S", 5.0, 30, 180)], system_loss=0.20)
    eb = sum(v[0] for v in pvforecast.compute(base, _maps())["pvmodel:S"].values())
    el = sum(v[0] for v in pvforecast.compute(lossy, _maps())["pvmodel:S"].values())
    assert el < eb
    assert abs(el - 0.80 * eb) < 0.01 * eb


def test_enabled_and_source_ids():
    cfg = _cfg([PvArray("Ost", 5.0, 30, 90), PvArray("West", 5.0, 30, 270)])
    assert pvforecast.enabled(cfg)
    assert pvforecast.source_ids(cfg) == ["pvmodel:Ost", "pvmodel:West"]
    cfg.pv_model.enabled = False
    assert not pvforecast.enabled(cfg)


def test_read_pv_signal_dispatches_to_pvmodel(tmp_path):
    """read_pv_signal (Solcast-Dispatcher) liefert bei aktivem pvlib-Modell die
    lokal geschriebene pvlib-Prognose."""
    from ems import local_history, solcast
    cfg = _cfg([PvArray("S", 5.0, 30, 180)])
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    per_source = pvforecast.compute(cfg, _maps())
    for src, m in per_source.items():
        local_history.write_pv_forecast(cfg.e3dc_rscp.history_db_path, src, m)
    start = UTC_DAY[0].tz_convert("Europe/Berlin")
    end = UTC_DAY[-1].tz_convert("Europe/Berlin")
    s = solcast.read_pv_signal(cfg, None, "pv_forecast", start, end)
    assert not s.empty and float(s.max()) > 2000.0


def test_config_rejects_solcast_and_pvmodel_together(tmp_path):
    import yaml
    from ems.config import load_config
    base = yaml.safe_load(open("config.example.yaml"))
    base["solcast"] = {"enabled": True, "sources": [
        {"api_key": "k", "resource_id": "r"}]}
    base["pv_model"] = {"enabled": True, "arrays": [
        {"name": "S", "kwp": 5, "tilt": 30, "azimuth": 180}]}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(base))
    with pytest.raises(ValueError, match="nicht gleichzeitig"):
        load_config(str(p))


def test_config_rejects_pvmodel_without_arrays(tmp_path):
    import yaml
    from ems.config import load_config
    base = yaml.safe_load(open("config.example.yaml"))
    base["pv_model"] = {"enabled": True, "arrays": []}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(base))
    with pytest.raises(ValueError, match="arrays"):
        load_config(str(p))


def test_read_pv_forecast_interpolates_hourly_without_gaps(tmp_path):
    """Regression: pvlib schreibt STÜNDLICHE Zeitstempel. read_pv_forecast muss
    sie glatt aufs 15-min-Raster interpolieren - das alte ffill(limit=1) hielt
    nur EINEN Sub-Slot und ließ danach NaN-Löcher (Zackenkurve im Dashboard)."""
    from ems import local_history
    db = str(tmp_path / "h.sqlite")
    base = pd.Timestamp("2026-07-15 08:00", tz="UTC")
    # zwei Arrays, je stündlich 08:00..14:00
    for arr, scale in (("pvmodel:Ost", 1.0), ("pvmodel:West", 0.8)):
        m = {}
        for h in range(7):
            ts = (base + pd.Timedelta(hours=h)).isoformat()
            w = 1000.0 * (h + 1) * scale
            m[ts] = (w, 0.7 * w, 1.2 * w)
        local_history.write_pv_forecast(db, arr, m)
    start = base.tz_convert("Europe/Berlin")
    end = (base + pd.Timedelta(hours=8)).tz_convert("Europe/Berlin")   # > letzter Punkt
    s = local_history.read_pv_forecast(db, start, end, "Europe/Berlin", 15,
                                       "sum", "pv",
                                       ["pvmodel:Ost", "pvmodel:West"])
    last_real = (base + pd.Timedelta(hours=6)).tz_convert("Europe/Berlin")  # 14:00
    # zwischen den Stundenwerten (bis zum letzten echten Punkt) KEIN NaN-Loch
    interior = s[s.index <= last_real]
    assert interior.notna().all(), "NaN-Löcher zwischen Stundenwerten"
    # nach dem letzten echten Punkt bleibt NaN (löst Schätzung/Frische aus)
    assert s[s.index > last_real].isna().all()
    # linear interpoliert: 08:15/08:30 liegen zwischen 08:00 und 09:00
    v0 = float(s.iloc[0]); v4 = float(s.iloc[4])          # 08:00 und 09:00
    assert v0 < float(s.iloc[1]) < float(s.iloc[2]) < v4, "nicht monoton interpoliert"


def test_read_pv_forecast_filters_sources_no_cross_pollution(tmp_path):
    """Regression: Solcast UND pvlib-Schatten schreiben in dieselbe Tabelle.
    Ein Read der einen Quelle (ohne require_complete) darf die andere NICHT
    mitsummieren - sonst Verdopplung/Sägezahn an gemeinsamen Zeitstempeln."""
    from ems import local_history
    db = str(tmp_path / "h.sqlite")
    base = pd.Timestamp("2026-07-15 08:00", tz="UTC")
    # Solcast (30-min) und pvmodel (stündlich) mit teils GLEICHEN Zeitstempeln
    sol = {(base + pd.Timedelta(minutes=30 * k)).isoformat(): (1000.0, 700.0, 1200.0)
           for k in range(5)}
    pvm = {(base + pd.Timedelta(hours=k)).isoformat(): (5000.0, 3000.0, 6000.0)
           for k in range(3)}
    local_history.write_pv_forecast(db, "solA", sol)
    local_history.write_pv_forecast(db, "pvmodel:X", pvm)
    start = base.tz_convert("Europe/Berlin")
    end = (base + pd.Timedelta(hours=3)).tz_convert("Europe/Berlin")
    # nur Solcast lesen (kein require_complete) -> nie den pvmodel-Wert dazu
    s = local_history.read_pv_forecast(db, start, end, "Europe/Berlin", 15,
                                       "sum", "pv", sources=["solA"])
    assert float(s.dropna().max()) < 1100.0, "pvmodel wurde mitsummiert"
    # pvmodel getrennt lesbar
    p = local_history.read_pv_forecast(db, start, end, "Europe/Berlin", 15,
                                       "sum", "pv", sources=["pvmodel:X"])
    assert 4900.0 < float(p.dropna().max()) < 5100.0
