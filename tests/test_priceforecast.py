"""Tests der Börsenpreis-Prognose aus deutschlandweiten Wetter-Indizes
(ems/gridweather.py + ems/priceforecast.py) und ihrer Verkabelung.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.gridweather import POINTS, to_indices, wind_power_fraction
from ems.local_history import read_grid_weather, write_grid_weather, write_spot
from ems.priceforecast import PriceModel, build_features
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
