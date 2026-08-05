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

Mehrere Wettermodelle werden getrennt durch pvlib gerechnet und archiviert.
Ihre Gewichte werden je Prognosehorizont aus echten Rolling-Origin-Fehlern
gelernt. p10/p90 kombinieren die aktuelle Modellstreuung mit empirischen
Ist/Prognose-Residualen; die konfigurierten Unsicherheiten bleiben nur Fallback.
"""
from __future__ import annotations

import logging

import pandas as pd

from . import local_history

log = logging.getLogger("ems.pvforecast")

_ensemble_status = {
    "updated": None, "models": [], "weights": {}, "residuals": {},
}

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


def ensemble_status() -> dict:
    """Letzte Diagnose des laufenden PV-lib-Ensembles."""
    return dict(_ensemble_status)


def status_summary() -> str:
    status = _ensemble_status
    models = status.get("models") or []
    if not models:
        return "PV-lib-Ensemble wartet auf den ersten Lauf"
    parts = [f"PV-lib-Ensemble: {len(models)} Modelle"]
    weights = status.get("weights") or {}
    learned = sum(1 for value in weights.values() if value.get("learned"))
    if weights:
        parts.append(f"Gewichte {learned}/{len(weights)} Horizonte gelernt")
        shown_name = "6-24h" if "6-24h" in weights else next(iter(weights))
        shown = (weights.get(shown_name) or {}).get("weights") or {}
        if shown:
            labels = {
                "best_match": "Best", "dwd_icon": "DWD",
                "ecmwf_ifs": "ECMWF",
            }
            parts.append(
                shown_name + " " + "/".join(
                    f"{labels.get(model, model)} {value:.0%}"
                    for model, value in shown.items()))
    residuals = status.get("residuals") or {}
    bands = sum(1 for value in residuals.values() if value.get("learned"))
    if residuals:
        parts.append(f"Band {bands}/{len(residuals)} Horizonte empirisch")
        conditional = sum(
            len(value.get("conditions") or {}) for value in residuals.values())
        if conditional:
            parts.append(f"{conditional} konditionale Bandzellen")
    return " · ".join(parts)


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

    # Open-Meteo liefert INTERVALL-MITTELWERTE, gestempelt auf den Intervall-
    # ANFANG. Die Sonnenposition gehoert deshalb in die Intervall-MITTE, nicht
    # auf die Zeitmarke - sonst wird ein Stundenmittel mit dem Sonnenstand vom
    # Stundenbeginn transponiert.
    #
    # Die Folge ist keine Streuung, sondern ein Versatz mit Richtung: morgens
    # steht die Sonne zum Intervallbeginn tiefer als im Mittel (Ertrag zu
    # niedrig), nachmittags hoeher (zu hoch). Der Energieschwerpunkt wandert um
    # eine halbe Intervalllaenge nach hinten. Gemessen am 05.08.2026 ueber sieben
    # Tage: Ist 13,0 h, Solcast 13,0 h, pvlib 13,6 h - bei einer Ost/West-Anlage,
    # deren Schwerpunkt per Konstruktion beim Sonnenhoechststand liegen muss.
    schritt = pd.Series(idx).diff().median()
    if pd.isna(schritt) or schritt <= pd.Timedelta(0):
        schritt = pd.Timedelta(hours=1)
    mitte = idx + schritt / 2
    solpos = pvlib.solarposition.get_solarposition(
        mitte, config.general.latitude, config.general.longitude,
        temperature=temp_air.to_numpy())
    solpos.index = idx          # zurueck auf das Raster der Einstrahlung
    dni_extra = pvlib.irradiance.get_extra_radiation(mitte)
    dni_extra.index = idx

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
    """Wettermodelle holen, getrennt durch pvlib rechnen und lernend mischen."""
    global _last_refresh
    if not active(config):
        return 0
    import time as _t
    if not force and _t.time() - _last_refresh <= 300:
        return 0
    from .weather import fetch_pv_weather_models
    from . import pv_ensemble
    w = config.weather
    model_maps = fetch_pv_weather_models(
        config.general.latitude, config.general.longitude,
        min(2, w.past_days), w.forecast_days,
        config.pv_model.weather_models)
    model_outputs = {
        model: output for model, maps in model_maps.items()
        if (output := compute(config, maps))
    }
    if not model_outputs:
        log.warning("Kein PV-Wettermodell verfügbar – nutze vorhandenen Cache.")
        return 0
    db = config.e3dc_rscp.history_db_path
    # Schema auch bei einer frischen Datenbank vor den lesenden Ensemble-
    # Auswertungen sicherstellen.
    con = local_history._con(db)
    con.close()
    # issue_ts = jetzt: unveränderlicher Snapshot je Lauf (Rolling-Origin). So
    # kann pv_source_report.py pvlib fair gegen Solcast (das ebenso archiviert
    # wird, ems/solcast.py) und die realen actuals.pv_w messen, statt nur den
    # ständig überschriebenen Live-Cache zu sehen.
    issue = pd.Timestamp.now(tz="UTC")
    aggregate_sources = source_ids(config)
    model_sources = pv_ensemble.model_source_map(
        model_outputs, aggregate_sources)
    cfg = config.pv_model
    weights = pv_ensemble.learn_weights(
        db, model_sources, issue, cfg.ensemble_lookback_days,
        cfg.ensemble_min_samples, cfg.ensemble_horizon_hours,
        cfg.ensemble_min_weight)
    residuals = pv_ensemble.residual_quantiles(
        db, aggregate_sources, issue, cfg.ensemble_lookback_days,
        cfg.ensemble_min_samples, cfg.ensemble_horizon_hours,
        max(0.05, 1.0 - cfg.p10_uncertainty),
        1.0 + cfg.p90_uncertainty, config.general.timezone)
    per_source, diagnostics = pv_ensemble.combine(
        model_outputs, weights, residuals, issue,
        cfg.ensemble_horizon_hours)
    n = 0
    # Wettermodell-Mitglieder separat halten: Grundlage der später gelernten
    # Gewichte, aber niemals direkt vom Optimierer gelesen.
    for model, outputs in model_outputs.items():
        for aggregate_source, mapping in outputs.items():
            src = pv_ensemble.member_source(model, aggregate_source)
            n += local_history.write_pv_forecast(db, src, mapping)
            local_history.write_pv_forecast_archive(db, src, issue, mapping)
    # Kompatible Ensemble-Ausgabe unter den bisherigen pvmodel:<Array>-IDs.
    for src, m in per_source.items():
        n += local_history.write_pv_forecast(db, src, m)
        local_history.write_pv_forecast_archive(db, src, issue, m)
    _ensemble_status.update(
        updated=issue.isoformat(), models=list(model_outputs),
        weights=diagnostics["weights"], residuals=diagnostics["residuals"],
        slots_by_bucket=diagnostics["slots_by_bucket"])
    _last_refresh = _t.time()
    log.info(
        "PV-Modell (pvlib-Ensemble): %d Werte, %d Wettermodelle, %d Arrays "
        "aktualisiert (%s).", n, len(model_outputs), len(per_source),
        status_summary())
    return n
