"""Temperatur + Solar-Einstrahlung direkt von Open-Meteo (kostenlos, kein Key).

Ersetzt für den Standalone-Betrieb das InfluxDB-Signal `temperature` (das nur
als Ähnlichkeits-Gewicht in der Verbrauchsprognose dient) und liefert zusätzlich
`shortwave_radiation` (W/m² Globalstrahlung) für den solaren Wärmeeintrag
thermischer steuerbarer Lasten (Pool, ems/loads.py). Beide Felder kommen aus
DEMSELBEN Open-Meteo-Call (kein zusätzlicher HTTP-Request). Zwei Endpunkte:

  * Forecast-API: letzte `past_days` (max 92) + `forecast_days` Zukunft,
    stündlich – je Zyklus abgerufen und in die lokale SQLite gecacht.
  * Archive-API (ERA5): tiefe Historie in einem Call – für den einmaligen
    Backfill (weather_backfill.py).

Stündliche Werte werden beim Auslesen auf das Slot-Raster interpoliert (wie
zuvor read_slots), damit sie 1:1 als hist_temp/fut_temp bzw. solar_w_m2 taugen.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Dict

log = logging.getLogger("ems.weather")

_FORECAST = "https://api.open-meteo.com/v1/forecast"
_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _get(url: str, params: dict, timeout: float = 20.0) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=timeout) as r:
        return json.load(r)


def _to_map(payload: dict, field: str = "temperature_2m") -> Dict[str, float]:
    """Open-Meteo hourly (UTC) -> {UTC-ISO-Stunde: Wert}, NaN übersprungen."""
    h = payload.get("hourly", {}) or {}
    times = h.get("time", []) or []
    vals = h.get(field, []) or []
    out: Dict[str, float] = {}
    for t, v in zip(times, vals):
        if v is None:
            continue
        # Open-Meteo liefert "YYYY-MM-DDTHH:MM" in UTC (timezone=UTC) -> +00:00
        out[t + ":00+00:00" if len(t) == 16 else t] = float(v)
    return out


def fetch_forecast(lat: float, lon: float, past_days: int = 92,
                   forecast_days: int = 4):
    """Stündliche Temperatur + Solar-Einstrahlung (EIN HTTP-Call): jüngste
    Vergangenheit + Zukunft. Rückgabe: (temp_map, radiation_map)."""
    d = _get(_FORECAST, {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,shortwave_radiation",
        "past_days": max(0, min(92, int(past_days))),
        "forecast_days": max(1, min(16, int(forecast_days))),
        "timezone": "UTC"})
    return _to_map(d, "temperature_2m"), _to_map(d, "shortwave_radiation")


def fetch_archive(lat: float, lon: float, start_date: str, end_date: str):
    """Stündliche Temperatur + Solar-Einstrahlung aus dem ERA5-Archiv (EIN Call),
    tiefe Historie. Rückgabe: (temp_map, radiation_map)."""
    d = _get(_ARCHIVE, {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,shortwave_radiation",
        "start_date": start_date, "end_date": end_date, "timezone": "UTC"},
        timeout=60.0)
    return _to_map(d, "temperature_2m"), _to_map(d, "shortwave_radiation")


# Einstrahlungs-KOMPONENTEN für die pvlib-Ertragsprognose (ems/pvforecast.py):
# GHI (shortwave_radiation), DNI (direct_normal_irradiance) und DHI
# (diffuse_radiation) - alle drei braucht pvlib für die POA-Transposition auf
# geneigte Module. Plus Lufttemperatur (Zelltemperatur) und Windgeschwindigkeit
# (Faiman-Zelltemperaturmodell). Eigener Call, nur wenn das PV-Modell aktiv ist.
_PV_FIELDS = ("shortwave_radiation", "direct_normal_irradiance",
              "diffuse_radiation", "temperature_2m", "wind_speed_10m")


def _pv_maps(payload):
    return {f: _to_map(payload, f) for f in _PV_FIELDS}


def fetch_pv_weather(lat: float, lon: float, past_days: int = 92,
                     forecast_days: int = 4):
    """GHI/DNI/DHI + Temperatur + Wind (Forecast-API, EIN Call) für pvlib.
    Rückgabe: dict {feld: {UTC-ISO-Stunde: Wert}}."""
    d = _get(_FORECAST, {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(_PV_FIELDS),
        "past_days": max(0, min(92, int(past_days))),
        "forecast_days": max(1, min(16, int(forecast_days))),
        "timezone": "UTC"})
    return _pv_maps(d)


def fetch_pv_weather_archive(lat: float, lon: float, start_date: str,
                             end_date: str):
    """GHI/DNI/DHI + Temperatur + Wind aus dem ERA5-Archiv (tiefe Historie,
    EIN Call) - für den einmaligen pvlib-Kalibrier-Backfill."""
    d = _get(_ARCHIVE, {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(_PV_FIELDS),
        "start_date": start_date, "end_date": end_date, "timezone": "UTC"},
        timeout=60.0)
    return _pv_maps(d)
