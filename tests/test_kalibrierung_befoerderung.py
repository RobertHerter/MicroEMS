"""Befoerderungsrahmen der Lastkorrektur mit rollierendem Ursprung.

Die Champion-/Challenger-Pruefung der Last rechnet nicht mehr auf dem Ende
eines 365-Tage-Hold-outs, sondern je Kalendertag mit frischem Training und
betriebsueblichem Horizont. Geprueft wird auf synthetischer Historie mit einem
Forecaster-Spion (kein echtes Training):
  * Urspruenge liegen auf lokalen Tagesanfaengen, auch ueber die Zeitumstellung,
  * kein Training sieht Daten ab seinem Ursprung,
  * jeder Ist-Slot der Tage zaehlt genau einmal,
  * Rueckfall auf den Hold-out-Rahmen bei zu wenigen gueltigen Urspruengen,
  * Champion und Challenger werden auf dieselbe Reihe angewandt.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import kalibrierung
from ems.calibration_competition import (
    apply_load_profile, compare_point_forecasts)
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
# Sonntag nach der Zeitumstellung vom 25.10.2026, bewusst nicht um Mitternacht
NOW = pd.Timestamp("2026-11-01 10:15", tz=TZ)


class _Repo:
    def signal_available(self, name):
        return True


def _cfg():
    cfg = make_config()
    cfg.general.slot_minutes = 15
    cfg.general.timezone = TZ
    cfg.e3dc_rscp.history_source = True
    return cfg


def _hist(now=NOW, days=120):
    index = pd.date_range(now - pd.Timedelta(days=days), now, freq="15min",
                          inclusive="left")
    lokal = index.tz_convert(TZ)
    werte = 400.0 + 20.0 * lokal.hour + np.arange(len(index)) * 0.001
    return pd.Series(werte, index=index)


def _spion(aufrufe, fehler_ab=None):
    """Forecaster-Ersatz: merkt sich je Aufruf Trainingsende, Start, Horizont."""
    class _Forecaster:
        def __init__(self, _cfg):
            self.load_hourly = "nicht neutralisiert"

        def forecast(self, train, start, horizon, **kw):
            start = pd.Timestamp(start)
            if fehler_ab is not None and start >= fehler_ab:
                raise RuntimeError("kein Modell")
            aufrufe.append({"train_max": train.index.max(),
                            "train_n": len(train), "start": start,
                            "horizon": horizon, "kw": kw,
                            "load_hourly": self.load_hourly})
            idx = pd.date_range(start.tz_convert("UTC"), periods=horizon,
                                freq="15min")
            return pd.Series(450.0, index=idx.tz_convert(TZ))
    return _Forecaster


def _lauf(monkeypatch, hist, aufrufe, now=NOW, fehler_ab=None, **kw):
    cfg = _cfg()
    monkeypatch.setattr(kalibrierung, "_load_hist", lambda *a, **k: hist)
    monkeypatch.setattr(kalibrierung, "_temp_hist", lambda *a, **k: None)
    monkeypatch.setattr(kalibrierung, "_pv_forecast_hist",
                        lambda *a, **k: (None, "test"))
    monkeypatch.setattr(kalibrierung, "LoadForecaster",
                        _spion(aufrufe, fehler_ab))
    res = kalibrierung.calibrate_load(_Repo(), cfg, now, lookback_days=120,
                                      test_days=30, promotion_days=14, **kw)
    return cfg, res


def _rollierend(aufrufe):
    """Der erste Aufruf ist der Hold-out, der Rest die rollierenden Urspruenge."""
    return aufrufe[1:]


def test_origins_are_local_midnights_across_dst_change():
    herbst = kalibrierung._promotion_origins(NOW, TZ, 14)
    assert len(herbst) == 14
    assert herbst[0] == pd.Timestamp("2026-10-18 00:00", tz=TZ)
    assert herbst[-1] == pd.Timestamp("2026-10-31 00:00", tz=TZ)
    assert all((o.hour, o.minute) == (0, 0) for o in herbst)
    assert [o.day for o in herbst] == list(range(18, 32))
    abstaende = {(b - a) / pd.Timedelta(hours=1)
                 for a, b in zip(herbst, herbst[1:])}
    assert abstaende == {24.0, 25.0}            # der 25.10. hat 25 Stunden

    fruehjahr = kalibrierung._promotion_origins(
        pd.Timestamp("2026-04-05 00:00", tz=TZ), TZ, 14)
    assert all((o.hour, o.minute) == (0, 0) for o in fruehjahr)
    # exakt um Mitternacht: der letzte volle Tag ist gestern
    assert fruehjahr[-1] == pd.Timestamp("2026-04-04 00:00", tz=TZ)
    assert 23.0 in {(b - a) / pd.Timedelta(hours=1)
                    for a, b in zip(fruehjahr, fruehjahr[1:])}


def test_no_training_sees_data_from_its_origin_on(monkeypatch):
    aufrufe = []
    _cfg_, res = _lauf(monkeypatch, _hist(), aufrufe)
    roll = _rollierend(aufrufe)
    assert [c["start"] for c in roll] == kalibrierung._promotion_origins(
        NOW, TZ, 14)
    for c in roll:
        assert c["train_max"] < c["start"]
        # ... und nutzt alles bis unmittelbar davor (taeglich frisches Training)
        assert c["train_max"] == c["start"] - pd.Timedelta(minutes=15)
        assert c["horizon"] == 48 * 4               # betriebsueblicher Horizont
        assert c["load_hourly"] is None             # Stundenprofil neutralisiert
        # ohne Prognose-Archiv keine rueckblickend bekannte "Zukunft"
        assert c["kw"]["fut_temp"] is None and c["kw"]["fut_pv"] is None
    assert res["promotion_method"] == "rolling_origin"
    assert res["promotion_origins_valid"] == 14
    assert res["promotion_origins"] == 14
    assert res["promotion_horizon_hours"] == 48


def test_every_actual_slot_counts_exactly_once(monkeypatch):
    hist = _hist()
    _cfg_, res = _lauf(monkeypatch, hist, [])
    rahmen = res["_promotion_frame"]
    anfang = pd.Timestamp("2026-10-18 00:00", tz=TZ)
    ende = pd.Timestamp("2026-11-01 00:00", tz=TZ)
    erwartet = hist[(hist.index >= anfang) & (hist.index < ende)]
    assert rahmen.index.is_unique
    assert rahmen.index.equals(erwartet.index)
    assert len(rahmen) == 14 * 96 + 4               # 25-Stunden-Tag am 25.10.
    assert res["promotion_samples"] == len(rahmen)
    assert np.allclose(rahmen["actual"].values, erwartet.values)
    assert (rahmen["raw"] == 450.0).all()


def test_origin_with_gap_is_skipped(monkeypatch, capsys):
    hist = _hist()
    tag = pd.Timestamp("2026-10-22 00:00", tz=TZ)
    hist = hist[~((hist.index >= tag)
                  & (hist.index < tag + pd.DateOffset(days=1)))]
    aufrufe = []
    _cfg_, res = _lauf(monkeypatch, hist, aufrufe)
    assert res["promotion_method"] == "rolling_origin"
    assert res["promotion_origins_valid"] == 13
    assert res["promotion_origins_skipped"] == ["2026-10-22"]
    assert tag not in [c["start"] for c in _rollierend(aufrufe)]
    lokal = res["_promotion_frame"].index.tz_convert(TZ)
    assert 22 not in set(lokal.day)
    assert "13/14 Tage gueltig" in capsys.readouterr().out


def test_falls_back_to_holdout_frame_with_too_few_origins(monkeypatch, capsys):
    """Nur 5 gueltige Tage (< 7): der bisherige Hold-out-Rahmen entscheidet,
    mit klarer Logzeile und ohne Absturz."""
    hist = _hist()
    luecke_ab = pd.Timestamp("2026-10-18 00:00", tz=TZ)
    luecke_bis = pd.Timestamp("2026-10-27 00:00", tz=TZ)
    hist = hist[~((hist.index >= luecke_ab) & (hist.index < luecke_bis))]
    aufrufe = []
    _cfg_, res = _lauf(monkeypatch, hist, aufrufe)
    assert res["promotion_method"] == "holdout_fallback"
    assert res["promotion_origins_valid"] == 5
    rahmen = res["_promotion_frame"]
    # Hold-out-Rahmen: 14 x 24 h rueckwaerts ab now, aus EINER Prognose
    assert rahmen.index.max() == hist.index.max()
    assert rahmen.index.min() >= NOW - pd.Timedelta(days=14)
    aus = capsys.readouterr().out
    assert "5/14 gueltige Urspruenge" in aus and "Rueckfall" in aus


def test_too_little_training_falls_back(monkeypatch):
    aufrufe = []
    _cfg_, res = _lauf(monkeypatch, _hist(days=50), aufrufe)
    assert res["promotion_method"] == "holdout_fallback"
    assert res["promotion_origins_valid"] == 0
    assert len(aufrufe) == 1                        # nur der Hold-out
    assert res["promotion_samples"] > 0


def test_failing_forecast_does_not_crash_the_run(monkeypatch, capsys):
    """Wirft die Prognose fuer die rollierenden Urspruenge, bleibt der Lauf
    heil und der Hold-out-Rahmen entscheidet."""
    aufrufe = []
    _cfg_, res = _lauf(monkeypatch, _hist(), aufrufe,
                       fehler_ab=pd.Timestamp("2026-10-18 00:00", tz=TZ))
    assert res["promotion_method"] == "holdout_fallback"
    assert res["promotion_origins_valid"] == 0
    assert len(res["promotion_origins_skipped"]) == 14
    assert not res["_promotion_frame"].empty
    assert "Rueckfall" in capsys.readouterr().out


def test_archive_futures_only_when_complete(monkeypatch):
    """Zukunftsreihen kommen nur aus vollstaendigen Issue-Time-Snapshots."""
    komplett_ab = pd.Timestamp("2026-10-25 00:00", tz=TZ)
    gelesen = []

    def leser(origin, end):
        gelesen.append((origin, end))
        idx = pd.date_range(origin, end, freq="15min", inclusive="left")
        return {"temp": pd.Series(8.0, index=idx), "pv": None,
                "complete": origin >= komplett_ab}

    aufrufe = []
    cfg = _cfg()
    hist = _hist()
    temp = pd.Series(10.0, index=hist.index)
    monkeypatch.setattr(kalibrierung, "LoadForecaster", _spion(aufrufe))
    rahmen, info = kalibrierung._rolling_promotion_frame(
        cfg, hist, temp, None, NOW, promotion_days=14, horizon_hours=48,
        archive_reader=leser)
    assert info["valid"] == 14 and info["archive"] == 7
    assert all(end - origin == pd.Timedelta(hours=48)
               for origin, end in gelesen)
    for c in aufrufe:
        if c["start"] >= komplett_ab:
            assert c["kw"]["fut_temp"] is not None
            assert c["kw"]["hist_temp"].index.max() < c["start"]
        else:
            assert c["kw"]["fut_temp"] is None and c["kw"]["hist_temp"] is None
    # die Korrektur des Aufrufers bleibt unangetastet
    cfg.forecast.correction_factor = 1.3
    kalibrierung._rolling_promotion_frame(cfg, hist, None, None, NOW)
    assert cfg.forecast.correction_factor == 1.3


def test_champion_and_challenger_are_compared_on_the_same_series(monkeypatch):
    """Beide Profile werden auf dieselbe rollierende Rohreihe angewandt und
    gegen dieselben Ist-Werte gemessen."""
    cfg, res = _lauf(monkeypatch, _hist(), [])
    rahmen = res["_promotion_frame"]
    champion_profil = {"load_hourly": {h: 1.0 for h in range(24)}}
    challenger_profil = {"load_hourly": {h: 1.1 for h in range(24)}}
    champion = apply_load_profile(rahmen["raw"], champion_profil, TZ)
    challenger = apply_load_profile(rahmen["raw"], challenger_profil, TZ)
    assert champion.index.equals(rahmen.index)
    assert challenger.index.equals(rahmen.index)
    assert np.allclose(champion.values, rahmen["raw"].values)
    assert np.allclose(challenger.values, 1.1 * rahmen["raw"].values)
    entscheid = compare_point_forecasts(
        rahmen["actual"], champion, challenger, TZ, min_samples=96)
    assert entscheid["n"] == len(rahmen) == res["promotion_samples"]
    assert entscheid["status"] in {"promoted", "held"}
