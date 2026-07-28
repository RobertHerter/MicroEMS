"""Deutschlandweite Wetter-Indizes als Treiber des Börsenpreises.

Der Day-Ahead-Preis folgt der Merit-Order über der **Residuallast** (deutsche
Last minus Wind- und Solareinspeisung) - nicht dem Wetter am Standort der
Anlage. Ein sonniger Tag in Bayern sagt wenig, ein Sturm über der Nordsee sehr
viel. Dieses Modul verdichtet das Wetter über acht kapazitätsgewichtete
Stützpunkte zu drei Zahlen je Stunde:

``wind_index``   0..1, gewichtete **Windleistung** (nicht -geschwindigkeit)
``solar_index``  W/m², gewichtete Globalstrahlung
``temp_index``   °C, bevölkerungsgewichtete Temperatur (Treiber der Last)

Datenquelle ist Open-Meteo - dieselbe kostenlose API ohne Key, die schon für die
lokale Temperatur genutzt wird (``ems/weather.py``). Alle Stützpunkte kommen in
EINEM Aufruf; Prognose (96 h) und ERA5-Archiv (tiefe Historie fürs Lernen)
liefern dieselben Felder.
"""
from __future__ import annotations

import logging
from typing import Dict, Tuple

import numpy as np
import pandas as pd

from .weather import _ARCHIVE, _FORECAST, _get

log = logging.getLogger("ems.gridweather")

_FIELDS = "wind_speed_100m,shortwave_radiation,temperature_2m"

# (lat, lon, Wind-, Solar-, Lastgewicht). Wind steht im Norden/Osten, PV eher im
# Süden, die Last folgt der Bevölkerung. Die Gewichte sind Näherungen der
# installierten Leistung bzw. Einwohnerzahl - sie müssen nicht exakt sein, das
# Preismodell lernt die Übertragung selbst; wichtig ist die räumliche Struktur.
POINTS: Tuple[Tuple[float, float, float, float, float], ...] = (
    (54.3, 9.8, 0.18, 0.04, 0.04),    # Schleswig-Holstein
    (52.9, 8.6, 0.22, 0.12, 0.10),    # Niedersachsen
    (52.4, 13.2, 0.16, 0.13, 0.08),   # Brandenburg/Berlin
    (51.5, 7.5, 0.14, 0.16, 0.26),    # NRW
    (51.3, 12.4, 0.14, 0.12, 0.09),   # Sachsen/Sachsen-Anhalt
    (50.1, 8.7, 0.06, 0.13, 0.15),    # Hessen/Rheinland-Pfalz
    (48.8, 9.2, 0.05, 0.15, 0.14),    # Baden-Württemberg
    (48.6, 11.6, 0.05, 0.15, 0.14),   # Bayern
)

# Vereinfachte Turbinenkennlinie (m/s): unter Einschaltwind nichts, dann ~v³ bis
# Nennwind, darüber Plateau, über der Sturmgrenze Abschaltung. Ohne diese
# Umrechnung wäre der Zusammenhang zum Preis stark nichtlinear und das Modell
# müsste ihn aus wenigen Sturmtagen selbst lernen.
_CUT_IN_MS = 3.0
_RATED_MS = 12.0
_CUT_OUT_MS = 25.0


def wind_power_fraction(speed_kmh) -> np.ndarray:
    """Windgeschwindigkeit (km/h) -> Leistungsanteil 0..1 je Stützpunkt."""
    v = np.asarray(speed_kmh, dtype="float64") / 3.6
    frac = np.clip((v - _CUT_IN_MS) / (_RATED_MS - _CUT_IN_MS), 0.0, 1.0) ** 3
    return np.where(v > _CUT_OUT_MS, 0.0, frac)


def _column(hourly: dict, field: str) -> np.ndarray:
    return np.array([np.nan if v is None else float(v)
                     for v in hourly.get(field) or []], dtype="float64")


def to_indices(payload) -> pd.DataFrame:
    """Antwort mit mehreren Stützpunkten zu den drei Indizes verdichten.

    Fehlt ein Stützpunkt oder eine Stunde, bleibt der Wert NaN - lieber eine
    Lücke als ein stillschweigend zu niedriger Index.
    """
    locs = payload if isinstance(payload, list) else [payload]
    if not locs or "hourly" not in locs[0]:
        return pd.DataFrame(columns=["wind_index", "solar_index", "temp_index"])
    index = pd.to_datetime(locs[0]["hourly"]["time"], utc=True)
    wind = np.zeros(len(index))
    solar = np.zeros(len(index))
    temp = np.zeros(len(index))
    for loc, (_, _, w_wind, w_solar, w_load) in zip(locs, POINTS):
        hourly = loc.get("hourly") or {}
        speed = _column(hourly, "wind_speed_100m")
        rad = _column(hourly, "shortwave_radiation")
        air = _column(hourly, "temperature_2m")
        if len(speed) != len(index):
            return pd.DataFrame(
                columns=["wind_index", "solar_index", "temp_index"])
        wind += w_wind * wind_power_fraction(speed)
        solar += w_solar * rad
        temp += w_load * air
    return pd.DataFrame({"wind_index": wind, "solar_index": solar,
                         "temp_index": temp}, index=index)


def _coords() -> Dict[str, str]:
    return {"latitude": ",".join(str(p[0]) for p in POINTS),
            "longitude": ",".join(str(p[1]) for p in POINTS)}


def fetch_forecast(past_days: int = 2, forecast_days: int = 4) -> pd.DataFrame:
    """Prognose-Indizes (bis 4 Tage) in einem Aufruf für alle Stützpunkte."""
    payload = _get(_FORECAST, dict(
        _coords(), hourly=_FIELDS, past_days=past_days,
        forecast_days=forecast_days, timezone="UTC"), timeout=45.0)
    return to_indices(payload)


def fetch_archive(start_date: str, end_date: str) -> pd.DataFrame:
    """ERA5-Indizes für einen Zeitraum (Training/Backfill)."""
    payload = _get(_ARCHIVE, dict(
        _coords(), hourly=_FIELDS, start_date=start_date,
        end_date=end_date, timezone="UTC"), timeout=120.0)
    return to_indices(payload)


def as_mapping(frame: pd.DataFrame) -> Dict[str, tuple]:
    """DataFrame -> {UTC-ISO: (wind, solar, temp)} für die Persistenz."""
    out: Dict[str, tuple] = {}
    for ts, row in frame.iterrows():
        values = (row["wind_index"], row["solar_index"], row["temp_index"])
        if any(pd.isna(v) for v in values):
            continue
        out[pd.Timestamp(ts).tz_convert("UTC").isoformat()] = tuple(
            round(float(v), 4) for v in values)
    return out
