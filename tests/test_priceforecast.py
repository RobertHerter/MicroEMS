"""Tests der Börsenpreis-Prognose aus deutschlandweiten Wetter-Indizes
(ems/gridweather.py + ems/priceforecast.py) und ihrer Verkabelung.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd
import pytest

from ems.gridweather import POINTS, to_indices, wind_power_fraction
from ems.local_history import read_grid_weather, write_grid_weather, write_spot
from ems.priceforecast import PriceModel, baseline_name, build_features
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


# --------------------------------------------------------------------------- #
# Wetter-Indizes
# --------------------------------------------------------------------------- #
def test_wind_power_curve_follows_the_turbine_characteristic():
    """Nicht die Geschwindigkeit zaehlt, sondern die Leistung: unter Einschalt-
    wind nichts, dann steil, ab Nennwind Plateau, im Sturm Abschaltung."""
    kmh = np.array([0.0, 10.0, 25.0, 43.2, 70.0, 100.0])   # 0, 2.8, 7, 12, 19, 28 m/s
    p = wind_power_fraction(kmh)
    assert p[0] == 0.0 and p[1] == 0.0            # unter 3 m/s
    assert 0.0 < p[2] < 0.5                       # 7 m/s: deutlich unter Nenn
    assert p[3] == pytest.approx(1.0)             # 12 m/s: Nennleistung
    assert p[4] == pytest.approx(1.0)             # Plateau
    assert p[5] == 0.0                            # ueber Sturmgrenze aus


def test_point_weights_sum_to_one_per_purpose():
    """Sonst waeren die Indizes nicht als gewichtete Mittel interpretierbar."""
    for column in (2, 3, 4):
        assert sum(p[column] for p in POINTS) == pytest.approx(1.0, abs=0.01)


def _payload(hours=3, wind=43.2, rad=500.0, temp=10.0):
    times = pd.date_range("2026-01-10", periods=hours, freq="h")
    return [{"hourly": {"time": [t.isoformat() for t in times],
                        "wind_speed_100m": [wind] * hours,
                        "shortwave_radiation": [rad] * hours,
                        "temperature_2m": [temp] * hours}}
            for _ in POINTS]


def test_to_indices_builds_weighted_means():
    frame = to_indices(_payload())
    assert list(frame.columns) == ["wind_index", "solar_index", "temp_index"]
    # Alle Stuetzpunkte gleich -> Index = Einzelwert (Gewichte summieren zu 1).
    assert frame["wind_index"].iloc[0] == pytest.approx(1.0, abs=0.01)
    assert frame["solar_index"].iloc[0] == pytest.approx(500.0, abs=5.0)
    assert frame["temp_index"].iloc[0] == pytest.approx(10.0, abs=0.1)


def test_to_indices_reports_a_gap_instead_of_a_low_index():
    """Fehlt ein Stuetzpunkt, darf der Index nicht stillschweigend zu klein
    werden - dann waere ein Sturm ueber der Nordsee als Windstille getarnt."""
    payload = _payload()
    payload[0]["hourly"]["wind_speed_100m"] = [None, None, None]
    frame = to_indices(payload)
    assert frame["wind_index"].isna().all()
    # Struktureller Fehler (falsche Laenge) -> leeres Ergebnis, keine Kruecke.
    payload[1]["hourly"]["wind_speed_100m"] = [10.0]
    assert to_indices(payload).empty


# --------------------------------------------------------------------------- #
# Speicherung
# --------------------------------------------------------------------------- #
def test_grid_weather_roundtrip(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    times = pd.date_range("2026-01-10", periods=4, freq="h", tz="UTC")
    mapping = {t.isoformat(): (0.5, 300.0, 7.5) for t in times}
    assert write_grid_weather(db, mapping) == 4
    frame = read_grid_weather(db, times[0], times[-1] + pd.Timedelta(hours=1), TZ)
    assert len(frame) == 4
    assert frame["wind_index"].iloc[0] == pytest.approx(0.5)
    # Mit freq wird auf das Slot-Raster interpoliert (tz-lokal wie die Verbraucher).
    fine = read_grid_weather(db, times[0], times[-1], TZ, freq="15min")
    assert len(fine) == 12 and str(fine.index.tz) == TZ


# --------------------------------------------------------------------------- #
# Merkmale
# --------------------------------------------------------------------------- #
def _weather(index):
    hours = index.tz_convert("UTC").floor("h").unique()
    return pd.DataFrame({"wind_index": np.linspace(0.1, 0.9, len(hours)),
                         "solar_index": np.full(len(hours), 200.0),
                         "temp_index": np.full(len(hours), 5.0)}, index=hours)


def test_build_features_refuses_to_guess_without_weather():
    index = pd.date_range("2026-01-10", periods=8, freq="15min", tz=TZ)
    assert build_features(index, pd.DataFrame(), 30.0, 10.0, set()) is None
    gappy = _weather(index).copy()
    gappy.iloc[0, 0] = np.nan
    assert build_features(index, gappy, 30.0, 10.0, set()) is None


def test_build_features_carries_the_price_anchor():
    """Ohne Preisanker koennte das Modell einer Niveauverschiebung (Gaspreis,
    Jahresgang) nicht folgen - das Wetter sagt darueber nichts."""
    index = pd.date_range("2026-01-10", periods=8, freq="15min", tz=TZ)
    x = build_features(index, _weather(index), 42.0, 13.0, set())
    assert x is not None and x.shape == (8, 9)
    assert (x[:, 7] == 42.0).all() and (x[:, 8] == 13.0).all()


def test_build_features_survives_the_dst_fold():
    """Zur Zeitumstellung gibt es lokal eine doppelte Stunde - in UTC runden."""
    index = pd.date_range("2025-10-26 01:00", periods=12, freq="15min",
                          tz="UTC").tz_convert(TZ)
    assert build_features(index, _weather(index), 30.0, 10.0, set()) is not None


# --------------------------------------------------------------------------- #
# Modell mit Selbstpruefung
# --------------------------------------------------------------------------- #
def _synthetic_history(days=110):
    """Preis = Funktion von Wind und Tageszeit + Rauschen. Ein lernbares
    Muster, das die Aehnliche-Tage-Mittelung NICHT kennt (der Wind wechselt
    taeglich, sie mittelt ihn weg)."""
    rng = np.random.default_rng(7)
    start = pd.Timestamp("2026-01-01", tz=TZ)
    index = pd.date_range(start, periods=days * 96, freq="15min", tz=TZ)
    hours = index.tz_convert("UTC").floor("h").unique()
    wind = rng.uniform(0.0, 1.0, len(hours)).repeat(1)
    wx = pd.DataFrame({"wind_index": wind,
                       "solar_index": np.full(len(hours), 150.0),
                       "temp_index": np.full(len(hours), 6.0)}, index=hours)
    w_slot = wx["wind_index"].reindex(index.tz_convert("UTC").floor("h")).to_numpy()
    hour = index.hour.to_numpy(dtype=float)
    price = (30.0 - 22.0 * w_slot + 6.0 * np.sin(2 * np.pi * hour / 24.0)
             + rng.normal(0.0, 0.8, len(index)))
    return pd.Series(price, index=index, dtype="float64"), wx


def test_model_beats_similar_days_on_a_weather_driven_price():
    spot, weather = _synthetic_history()
    model = PriceModel(make_config())
    assert model.fit(spot, weather, holdout_days=10, min_train_days=60) is True
    st = model.status()
    assert st["active"] is True
    assert st["mae_model_ct"] < st["mae_baseline_ct"], st
    assert st["train_days"] >= 100



def test_baseline_name_follows_the_forecast_method():
    """Mit forecast.method "ml" ist die bisherige Preisschaetzung die
    ML-Prognose - im Log stand bis 23.10.2026 trotzdem "Aehnliche Tage"."""
    cfg = make_config()
    cfg.forecast.method = "ml"
    assert baseline_name(cfg) == "ML-Prognose"
    cfg.forecast.method = "similar_days"
    assert baseline_name(cfg) == "Ähnliche-Tage-Prognose"


def test_model_verdict_names_the_baseline_it_was_measured_against():
    spot, weather = _synthetic_history()
    cfg = make_config()
    cfg.forecast.method = "ml"
    model = PriceModel(cfg)
    model.fit(spot, weather, holdout_days=10, min_train_days=60)
    assert "ML-Prognose" in model.reason, model.reason
    assert "Ähnliche" not in model.reason, model.reason


def test_model_stands_down_when_similar_days_wins():
    """Bei einem rein kalendarischen Preis (jeden Tag identisch) hat das
    Wettermodell keinen Vorteil - dann muss es sich selbst abschalten."""
    start = pd.Timestamp("2026-01-01", tz=TZ)
    index = pd.date_range(start, periods=110 * 96, freq="15min", tz=TZ)
    hour = index.hour.to_numpy(dtype=float)
    spot = pd.Series(30.0 + 8.0 * np.sin(2 * np.pi * hour / 24.0), index=index)
    hours = index.tz_convert("UTC").floor("h").unique()
    rng = np.random.default_rng(3)
    weather = pd.DataFrame({"wind_index": rng.uniform(0, 1, len(hours)),
                            "solar_index": rng.uniform(0, 400, len(hours)),
                            "temp_index": rng.uniform(0, 15, len(hours))},
                           index=hours)
    model = PriceModel(make_config())
    ok = model.fit(spot, weather, holdout_days=10, min_train_days=60)
    assert ok is False and model.model is None
    assert "Ähnliche Tage besser" in model.reason or "nicht benutzt" in model.reason


def test_model_needs_enough_history():
    spot, weather = _synthetic_history(days=30)
    model = PriceModel(make_config())
    assert model.fit(spot, weather, holdout_days=10, min_train_days=60) is False
    assert "zu wenig Historie" in model.reason
    assert model.predict(spot.index[:8], weather, spot[:96]) is None


def test_predict_needs_weather_for_the_target_slots():
    spot, weather = _synthetic_history()
    model = PriceModel(make_config())
    assert model.fit(spot, weather, holdout_days=10, min_train_days=60) is True
    future = pd.date_range(spot.index[-1] + pd.Timedelta(minutes=15),
                           periods=96, freq="15min", tz=TZ)
    # Kein Wetter fuer die Zielslots -> keine Schaetzung (statt Extrapolation).
    assert model.predict(future, weather, spot) is None


# --------------------------------------------------------------------------- #
# Verkabelung: _price_series nutzt das Modell und faellt sauber zurueck
# --------------------------------------------------------------------------- #
class _Repo:
    def read_slots(self, *a, **k):
        return pd.Series(dtype="float64")

    def signal_available(self, *a, **k):
        return False


def test_price_series_falls_back_without_grid_weather(tmp_path, monkeypatch):
    """Ohne deutschlandweite Historie muss die alte Schaetzung greifen - und der
    Zyklus darf nicht scheitern."""
    import ems.main as m

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    monkeypatch.setattr(m, "_price_model", None, raising=False)
    monkeypatch.setattr(m, "_price_model_day", None, raising=False)
    monkeypatch.setattr(m, "_last_price_fill", None)
    # Kein Netzabruf im Test.
    monkeypatch.setattr(m, "_refresh_grid_weather", lambda config: None)

    now = pd.Timestamp("2026-02-10 00:00", tz=TZ)
    hist = pd.date_range(now - pd.Timedelta(days=20), now, freq="15min",
                         tz=TZ, inclusive="left")
    write_spot(cfg.e3dc_rscp.history_db_path,
               {t.tz_convert("UTC").isoformat(): 20.0 + t.hour
                for t in hist})
    index = pd.date_range(now, periods=192, freq="15min", tz=TZ)
    price, estimated = m._price_series(_Repo(), cfg, index, now,
                                       return_estimated=True)
    assert len(price) == 192 and price.notna().all()
    assert estimated.any()          # der Folgetag ist geschaetzt
    # Ohne jede Preisquelle fuellt der Festpreis - und so steht es auch da.
    assert m._last_price_fill == "Festpreis"


def test_quality_display_names_the_estimator_that_filled(monkeypatch):
    """Die Anzeige sagte immer "Aehnliche-Tage-Preisprognose" - auch wenn das
    Residuallast-Modell oder (forecast.method: ml) die ML-Prognose fuellte."""
    import ems.main as m

    now = pd.Timestamp("2026-02-10 00:00", tz=TZ)
    vergangen = pd.date_range(now - pd.Timedelta(days=20), now, freq="15min",
                              tz=TZ, inclusive="left")
    hist = pd.Series([20.0 + t.hour for t in vergangen], index=vergangen)
    monkeypatch.setattr(m, "read_price_signal", lambda config, repo, start, end:
                        hist[(hist.index >= start) & (hist.index < end)])
    monkeypatch.setattr(m, "_refresh_grid_weather", lambda config: None)
    monkeypatch.setattr(m, "_price_model_for", lambda config: None)
    monkeypatch.setattr(m, "_last_price_fill", None)
    index = pd.date_range(now, periods=96, freq="15min", tz=TZ)
    for methode, name in (("similar_days", "Ähnliche-Tage-Prognose"),
                          ("ml", "ML-Prognose")):
        cfg = make_config()
        cfg.forecast.method = methode
        price = m._price_series(_Repo(), cfg, index, now)
        assert price.notna().all()
        assert m._last_price_fill == name


# --------------------------------------------------------------------------- #
# Training im Hintergrund (ems/main.py)
# --------------------------------------------------------------------------- #
class _Modell:
    """Ersatz fuer PriceModel: fit wartet auf eine Freigabe."""
    freigabe = None
    fits = []
    fehler = None

    def __init__(self, config):
        self.model = None
        self.reason = "Ersatz"

    def fit(self, spot, weather, holdout_days, min_train_days):
        from threadpoolctl import threadpool_info
        _Modell.fits.append({i["num_threads"] for i in threadpool_info()
                             if i["user_api"] == "openmp"})
        assert _Modell.freigabe.wait(10)
        if _Modell.fehler is not None:
            raise _Modell.fehler
        self.model = "neu"
        return True


@pytest.fixture
def preismodell(monkeypatch):
    import sklearn.ensemble  # noqa: F401 - laedt das OpenMP der HGB

    import ems.local_history as lh
    import ems.main as m
    import ems.priceforecast as pf
    bisher = _Modell(None)
    bisher.model = "alt"
    monkeypatch.setattr(pf, "PriceModel", _Modell)
    monkeypatch.setattr(lh, "read_spot", lambda *a, **k: pd.Series(dtype="float64"))
    monkeypatch.setattr(lh, "read_grid_weather", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(m, "_price_model", bisher)
    monkeypatch.setattr(m, "_price_model_day", None)      # zuletzt gestern
    monkeypatch.setattr(m, "_price_model_thread", None)
    monkeypatch.setattr(m, "_price_model_async", True)
    monkeypatch.setattr(_Modell, "freigabe", threading.Event())
    monkeypatch.setattr(_Modell, "fits", [])
    monkeypatch.setattr(_Modell, "fehler", None)
    yield m
    _Modell.freigabe.set()
    if m._price_model_thread is not None:
        m._price_model_thread.join(10)


def test_cycle_does_not_wait_for_the_training_and_keeps_the_previous_model(preismodell):
    """Vom 10. bis 23.10.2026 lief das taegliche Training um Mitternacht in den
    Genetik-Lauf von EOS und dauerte bis 19 Minuten - so lange kam kein
    Steuerbefehl, weil der Zyklus darauf wartete."""
    m = preismodell
    cfg = make_config()
    t0 = time.monotonic()
    assert m._price_model_for(cfg).model == "alt"
    assert time.monotonic() - t0 < 2
    assert m._price_model_thread.is_alive()
    assert m._price_model_for(cfg).model == "alt"       # kein zweites Training

    _Modell.freigabe.set()
    m._price_model_thread.join(10)
    assert m._price_model_for(cfg).model == "neu"
    # Ein Training je Tag, mit einem OpenMP-Thread (gilt nur in SEINEM Thread).
    assert _Modell.fits == [{m._PRICE_MODEL_THREADS}]


def test_failed_training_keeps_the_previous_model(preismodell, caplog):
    m = preismodell
    _Modell.fehler = RuntimeError("Datenbank weg")
    _Modell.freigabe.set()
    cfg = make_config()
    m._price_model_for(cfg)
    m._price_model_thread.join(10)

    assert m._price_model_for(cfg).model == "alt"
    assert len(_Modell.fits) == 1                       # kein Neuversuch am selben Tag
    assert "das bisherige bleibt in Gebrauch" in caplog.text


def test_one_shot_run_waits_for_the_training(preismodell):
    """Ein Einzellauf (ohne --loop) waere sonst vor dem Training vorbei."""
    m = preismodell
    m._price_model_async = False
    _Modell.freigabe.set()
    assert m._price_model_for(make_config()).model == "neu"
    assert m._price_model_thread is None
