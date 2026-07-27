"""Mehrmodell-Abruf für die PV-lib-Prognose."""
from ems import weather


def test_fetch_models_keeps_successful_provider(monkeypatch):
    calls = []

    def fake_fetch(lat, lon, past_days, forecast_days, model):
        calls.append(model)
        if model == "ecmwf_ifs":
            raise OSError("vorübergehend nicht verfügbar")
        return {"shortwave_radiation": {"2026-07-20T12:00:00+00:00": 500.0}}

    monkeypatch.setattr(weather, "fetch_pv_weather", fake_fetch)
    result = weather.fetch_pv_weather_models(
        47.85, 12.07, models=["best_match", "dwd_icon", "ecmwf_ifs"])
    assert calls == ["best_match", "dwd_icon", "ecmwf_ifs"]
    assert set(result) == {"best_match", "dwd_icon"}


def test_fetch_model_uses_dedicated_endpoint(monkeypatch):
    seen = {}

    def fake_get(url, params, timeout=20.0):
        seen["url"] = url
        seen["params"] = params
        return {"hourly": {"time": [], "shortwave_radiation": []}}

    monkeypatch.setattr(weather, "_get", fake_get)
    weather.fetch_pv_weather(
        47.85, 12.07, past_days=1, forecast_days=2, model="dwd_icon")
    assert seen["url"].endswith("/v1/dwd-icon")
    assert seen["params"]["wind_speed_unit"] == "ms"
