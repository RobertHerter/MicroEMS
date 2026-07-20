"""Freie PV-Ertragsprognose mit pvlib + Open-Meteo (Alternative zu Solcast).

Modelliert je konfiguriertem Panel-Array (kWp, Neigung, Azimut) den DC-Ertrag
aus den Open-Meteo-Einstrahlungskomponenten und der Lufttemperatur:

  Sonnenstand -> POA-Transposition (Hay-Davies, GHI/DNI/DHI auf die geneigte
  Fläche) -> Zelltemperatur (Faiman) -> DC-Leistung (PVWatts) -> Systemverluste.

Mehrere Ausrichtungen werden je Zeitpunkt summiert. Kein API-Key, kein
Kontingent. Geschrieben wird in DIESELBE pv_forecast-Tabelle wie bei Solcast
(Quelle "pvmodel:<array>"), sodass Auslesen (ems/solcast.read_pv_signal) und
Kalibrierung (kalibrierung.py, pv_month_hour gegen reale actuals.pv_w)
unverändert funktionieren - inkl. Optimierung gegen reale Ertrags- und
Wetterdaten.

p10/p90 sind heuristische Bänder um den Punktwert (pvlib liefert - anders als
Solcast - keine echten Quantile): p10 = pv*(1-p10_uncertainty),
p90 = pv*(1+p90_uncertainty). p10 dimensioniert die Peak-Einspeise-Linie
konservativ.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import local_history

log = logging.getLogger("ems.pvforecast")

_SIGNAL_WHICH = {"pv_forecast": "pv", "pv_forecast_p10": "p10",
                 "pv_forecast_p90": "p90"}


def enabled(config) -> bool:
    """Aktive PV-Quelle (füttert Optimierer/Kalibrierung). Shadow zählt NICHT."""
    return bool(config.pv_model.enabled and config.pv_model.arrays)


def active(config) -> bool:
    """Soll pvlib gerechnet/geschrieben werden? Auch im reinen Vergleichs-
    (shadow-)Modus - dann aber ohne Einfluss auf den Optimierer (enabled=False)."""
    pm = config.pv_model
    return bool((pm.enabled or pm.shadow) and pm.arrays)


def source_ids(config) -> list:
    """Quellen-IDs in der pv_forecast-Tabelle (eine je Array)."""
    return [f"pvmodel:{a.name}" for a in config.pv_model.arrays]


def _array_dc_watts(array, ghi, dni, dhi, temp_air, wind, solpos,
                    dni_extra, cfg) -> pd.Series:
    """DC-Leistung (W) eines Arrays über den Zeitindex von ghi (pvlib)."""
    import pvlib

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=array.tilt, surface_azimuth=array.azimuth,
        solar_zenith=solpos["apparent_zenith"], solar_azimuth=solpos["azimuth"],
        dni=dni, ghi=ghi, dhi=dhi, dni_extra=dni_extra, model="haydavies")
    poa_global = poa["poa_global"].fillna(0.0).clip(lower=0.0)
    cell_temp = pvlib.temperature.faiman(poa_global, temp_air, wind)
    dc = pvlib.pvsystem.pvwatts_dc(
        poa_global, cell_temp, pdc0=array.kwp * 1000.0,
        gamma_pdc=cfg.temp_coeff_per_c)
    dc = dc.fillna(0.0).clip(lower=0.0) * (1.0 - cfg.system_loss)
    return dc


def compute(config, maps: dict) -> dict:
    """pvlib-Ertrag je Array aus den Open-Meteo-Feld-Maps berechnen.

    maps: {feld: {UTC-ISO: Wert}} aus weather.fetch_pv_weather(_archive).
    Rückgabe: {source_id: {UTC-ISO: (pv_w, pv10_w, pv90_w)}}.
    """
    import pvlib

    cfg = config.pv_model
    ghi_map = maps.get("shortwave_radiation") or {}
    if not ghi_map:
        return {}
    idx = pd.to_datetime(sorted(ghi_map), utc=True, format="ISO8601")
    if len(idx) == 0:
        return {}

    def series(field):
        m = maps.get(field) or {}
        return pd.Series([m.get(k) for k in idx.strftime("%Y-%m-%dT%H:%M:%S+00:00")],
                         index=idx, dtype="float64")

    ghi = series("shortwave_radiation").fillna(0.0).clip(lower=0.0)
    dni = series("direct_normal_irradiance").fillna(0.0).clip(lower=0.0)
    dhi = series("diffuse_radiation").fillna(0.0).clip(lower=0.0)
    temp_air = series("temperature_2m").interpolate(limit=6).fillna(15.0)
    wind = series("wind_speed_10m").fillna(1.0).clip(lower=0.0)

    solpos = pvlib.solarposition.get_solarposition(
        idx, config.weather.latitude, config.weather.longitude,
        temperature=temp_air)
    dni_extra = pvlib.irradiance.get_extra_radiation(idx)

    out = {}
    for array in cfg.arrays:
        dc = _array_dc_watts(array, ghi, dni, dhi, temp_air, wind, solpos,
                             dni_extra, cfg)
        m = {}
        for ts, w in dc.items():
            w = float(w)
            m[ts.isoformat()] = (
                w, w * max(0.0, 1.0 - cfg.p10_uncertainty),
                w * (1.0 + cfg.p90_uncertainty))
        out[f"pvmodel:{array.name}"] = m
    return out


def read_compare(config, start, end):
    """Kombinierte pvlib-PV-Prognose [start, end) für den Dashboard-Vergleich
    (Summe der Arrays). Leer, wenn kein pv_model konfiguriert/gerechnet."""
    if not (config.pv_model.arrays):
        return pd.Series(dtype="float64")
    return local_history.read_pv_forecast(
        config.e3dc_rscp.history_db_path, start, end, config.general.timezone,
        config.general.slot_minutes, "sum", "pv", sources=source_ids(config))


_last_refresh = 0.0


def refresh(config, force: bool = False) -> int:
    """Open-Meteo-Wetter holen, pvlib rechnen, pv_forecast schreiben (throttled,
    höchstens ~einmal je 5 min). Deckt past_days (Historie für die Kalibrierung)
    + forecast_days ab. Rückgabe: Anzahl geschriebener (source,ts)-Werte."""
    global _last_refresh
    if not active(config):
        return 0
    import time as _t
    if not force and _t.time() - _last_refresh <= 300:
        return 0
    from .weather import fetch_pv_weather
    w = config.weather
    try:
        maps = fetch_pv_weather(w.latitude, w.longitude, w.past_days,
                                w.forecast_days)
    except Exception as exc:
        log.warning("Open-Meteo (PV-Modell) fehlgeschlagen (%s) – nutze Cache.", exc)
        return 0
    per_source = compute(config, maps)
    db = config.e3dc_rscp.history_db_path
    n = sum(local_history.write_pv_forecast(db, src, m)
            for src, m in per_source.items())
    _last_refresh = _t.time()
    log.info("PV-Modell (pvlib): %d Array-Zeitwerte aus %d Arrays aktualisiert.",
             n, len(per_source))
    return n
