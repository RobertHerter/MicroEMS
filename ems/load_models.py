"""Produktionsnahe Zusatzmodelle für die Hauslastprognose.

Die Funktionen bleiben bewusst außerhalb des MILP:
* gemessene steuerbare Lasten werden zur Grundlast zerlegt,
* Similar-Days und ML laufen als Rolling-Origin-Kandidaten,
* Gewichte und Temperatur-Residual werden nur aus vorab erstellten
  Optimierer-Snapshots gegen später gemessene Ist-Werte gelernt.
"""
from __future__ import annotations

import copy
import logging
import threading
import time

import numpy as np
import pandas as pd

from .loads import _season_mask
from .local_history import (read_controllable_load_power,
                            read_optimizer_forecast_snapshots)

log = logging.getLogger("ems.load_models")

_STATUS = {
    "updated": None, "disaggregation": {}, "ensemble": {},
    "temperature": {},
}
_ANALYSIS_CACHE = {"at": 0.0, "key": None, "value": None}
_ML_LOCK = threading.Lock()
_ML_PENDING = {}
_ML_TRAINING = set()
_ML_READY = set()


def status() -> dict:
    return {key: (dict(value) if isinstance(value, dict) else value)
            for key, value in _STATUS.items()}


def status_summary() -> str:
    dis = _STATUS.get("disaggregation") or {}
    ens = _STATUS.get("ensemble") or {}
    temp = _STATUS.get("temperature") or {}
    parts = []
    if dis.get("sources"):
        source_count = len(dis["sources"])
        parts.append(
            f"Grundlast bereinigt: {source_count} "
            f"{'Rückmeldung' if source_count == 1 else 'Rückmeldungen'}, "
            f"{dis.get('coverage_percent', 0):.0f}% Abdeckung")
    elif dis.get("enabled"):
        parts.append("Grundlastzerlegung wartet auf Rückmeldungen")
    if ens.get("enabled"):
        learned = sum(1 for item in (ens.get("buckets") or {}).values()
                      if item.get("learned"))
        total = len(ens.get("buckets") or {})
        recommendation = ens.get("recommendation")
        text = f"Lastmodell {learned}/{total} Horizonte gelernt"
        if recommendation:
            text += f", Empfehlung {recommendation}"
        parts.append(text)
    if temp.get("enabled"):
        parts.append(
            "Temperatur-Residual aktiv" if temp.get("learned")
            else f"Temperatur-Residual {temp.get('folds', 0)}/"
                 f"{temp.get('min_folds', 0)} Tage")
    return " · ".join(parts) if parts else "Last-Zusatzmodelle deaktiviert"


def _active_controllable_loads(config) -> list:
    active = []
    for load in getattr(config, "controllable_loads", []):
        if not load.enabled:
            continue
        if load.type == "thermal":
            configured = any(
                stage.feedback_topic or stage.power_topic
                for stage in load.stages)
        else:
            configured = bool(load.feedback_topic or load.power_topic)
        if configured:
            active.append(load)
    return active


def _embedded_profile(measured: pd.Series, complete: pd.Series,
                      target_index: pd.DatetimeIndex, forecaster,
                      loads: list, min_samples: int) -> pd.Series:
    valid = pd.Series(measured).where(pd.Series(complete)).dropna()
    if valid.empty:
        return pd.Series(0.0, index=target_index)
    features = forecaster._features(valid.index)
    features["power"] = valid.values
    target_features = forecaster._features(target_index)
    preferred = features.groupby(
        ["slot_of_day", "daytype"])["power"].agg(["mean", "count"])
    fallback = features.groupby("slot_of_day")["power"].agg(["mean", "count"])
    keys = pd.MultiIndex.from_arrays([
        target_features["slot_of_day"].to_numpy(dtype=int),
        target_features["daytype"].to_numpy(dtype=int),
    ])
    pref_mean = preferred["mean"].reindex(keys).to_numpy(dtype=float)
    pref_count = preferred["count"].reindex(keys).fillna(0).to_numpy(dtype=int)
    slots = target_features["slot_of_day"].to_numpy(dtype=int)
    fallback_mean = fallback["mean"].reindex(slots).to_numpy(dtype=float)
    fallback_count = fallback["count"].reindex(
        slots).fillna(0).to_numpy(dtype=int)
    values = np.where(
        pref_count >= min_samples, pref_mean,
        np.where(fallback_count >= min_samples, fallback_mean, 0.0))
    values = np.nan_to_num(values, nan=0.0)
    local = target_index.tz_convert(forecaster.cfg.general.timezone)
    month_days = list(zip(local.month, local.day))
    active = np.zeros(len(target_index), dtype=bool)
    for load in loads:
        active |= _season_mask(load, month_days)
    # Für intermittierende Lasten ist der Mittelwert absichtlich korrekt:
    # Einschaltdauer × Leistung ergibt die erwartete Energie.
    return pd.Series(
        np.where(active, np.clip(values, 0.0, None), 0.0),
        index=target_index, dtype="float64")


def disaggregate(config, forecaster, history: pd.Series, now):
    """Historische Gesamtlast in nicht steuerbare Grundlast zerlegen.

    Rückgabe ``(training, evaluation_actual, diagnostics)``. Für das Training
    wird ein aus echten Rückmeldungen gelerntes Erwartungsprofil auch auf
    ältere Slots projiziert. Die spätere Ergebnisprüfung nutzt dagegen nur
    exakt gemessene, vollständig rückgemeldete Slots.
    """
    fc = config.forecast
    loads = _active_controllable_loads(config)
    base = pd.Series(history, dtype="float64").sort_index()
    if (not fc.disaggregate_controllable_loads or not loads or base.empty):
        diag = {"enabled": bool(fc.disaggregate_controllable_loads),
                "sources": [], "coverage_percent": 0.0,
                "mean_removed_w": 0.0}
        _STATUS["disaggregation"] = diag
        return base, base.copy(), diag
    start = max(base.index.min(), pd.Timestamp(now) - pd.Timedelta(
        days=max(1, fc.disaggregation_lookback_days)))
    measured, complete, labels = read_controllable_load_power(
        config.e3dc_rscp.history_db_path, loads, start, now,
        config.general.timezone, config.general.slot_minutes)
    profile = _embedded_profile(
        measured, complete, base.index, forecaster, loads,
        max(1, fc.disaggregation_min_samples))
    exact_power = measured.reindex(base.index)
    exact_mask = complete.reindex(base.index).fillna(False) & exact_power.notna()
    # Wo GEMESSEN wurde, zaehlt die Messung - nicht das Erwartungsprofil. Vorher
    # wurde ueberall das Profil abgezogen, auch auf Slots mit exakter
    # Rueckmeldung, an denen die Last nachweislich stillstand.
    removal = exact_power.where(exact_mask)
    if fc.disaggregation_project_unmeasured:
        removal = removal.combine_first(profile.reindex(base.index))
    # Ohne Projektion bleibt die Energie unbekannter Slots in der Grundlast:
    # die Prognose faellt dann eher zu HOCH aus. Das ist die sichere Richtung -
    # ein zu tief bereinigter Sockel liess den Optimierer nachts mit 400 W
    # statt real 1200 W planen (Bias -374 W, 3,7 kWh in einer Nacht).
    training = (base - removal.fillna(0.0)).clip(lower=0.0)
    evaluation = (base - exact_power).clip(lower=0.0).where(exact_mask)
    recent_slots = int((base.index >= start).sum())
    coverage = 100.0 * float(exact_mask.sum()) / max(1, recent_slots)
    diag = {
        "enabled": True, "sources": labels,
        "coverage_percent": coverage,
        "evaluation_slots": int(exact_mask.sum()),
        "mean_removed_w": float(removal.fillna(0.0).mean()) if len(removal) else 0.0,
        "projected_unmeasured": bool(fc.disaggregation_project_unmeasured),
    }
    _STATUS["disaggregation"] = diag
    return training, evaluation, diag


def candidate_forecasts(forecaster, history: pd.Series, start,
                        horizon_slots: int, **forecast_kwargs) -> dict:
    """Alle Lastmodelle mit identischer Datenbasis berechnen."""
    config = forecaster.cfg
    configured = config.forecast.method
    methods = (["similar_days", "ml"]
               if config.forecast.load_ensemble_enabled else [configured])
    out = {}
    try:
        for method in methods:
            config.forecast.method = method
            model_history = history
            if method == "ml":
                retrain_h = max(
                    1, int(config.forecast.load_ensemble_ml_retrain_hours))
                cutoff = pd.Timestamp(start).floor(f"{retrain_h}h")
                lower = cutoff - pd.Timedelta(days=max(
                    30, config.forecast.load_ensemble_ml_lookback_days))
                bounded = history[
                    (history.index >= lower) & (history.index < cutoff)]
                if not bounded.empty:
                    model_history = bounded
                min_training_slots = int(
                    30 * 24 * config.general.slots_per_hour * 0.9)
                if len(model_history) < min_training_slots:
                    continue
                key = (cutoff.isoformat(), str(model_history.index[0]),
                       str(model_history.index[-1]), len(model_history))
                with _ML_LOCK:
                    ready = key in _ML_READY
                    if (not ready and key not in _ML_TRAINING
                            and key not in _ML_PENDING):
                        # Höchstens der jüngste Trainingswunsch ist relevant.
                        # Verhindert eine Warteschlange, wenn in kurzer Folge
                        # mehrere Neuberechnungen mit anderer Datenbasis laufen.
                        _ML_PENDING.clear()
                        _ML_PENDING[key] = (
                            copy.deepcopy(config), model_history.copy(),
                            pd.Timestamp(start), int(horizon_slots),
                            {name: (value.copy()
                                    if hasattr(value, "copy") else value)
                             for name, value in forecast_kwargs.items()})
                if not ready:
                    continue
            out[method] = forecaster.forecast(
                model_history, start, horizon_slots, **forecast_kwargs)
    finally:
        config.forecast.method = configured
    return out


def start_pending_ml_training() -> bool:
    """Ein wartendes ML-Schattenmodell erst NACH dem Solver trainieren."""
    with _ML_LOCK:
        if not _ML_PENDING or _ML_TRAINING:
            return False
        key, payload = next(iter(_ML_PENDING.items()))
        _ML_PENDING.pop(key, None)
        _ML_TRAINING.add(key)

    def train():
        cfg, history, start, horizon_slots, kwargs = payload
        try:
            cfg.forecast.method = "ml"
            from .forecast import LoadForecaster
            LoadForecaster(cfg).forecast(
                history, start, horizon_slots, **kwargs)
            with _ML_LOCK:
                _ML_READY.add(key)
            log.info(
                "ML-Lastschattenmodell trainiert; ab dem nächsten Zyklus "
                "im Horizontvergleich.")
        except Exception as exc:
            log.warning("ML-Lastschattenmodell nicht trainierbar (%s).", exc)
        finally:
            with _ML_LOCK:
                _ML_TRAINING.discard(key)

    threading.Thread(
        target=train, name="ems-load-ml-shadow", daemon=True).start()
    return True


def _bucket_name(hours: float, boundaries: list[float]) -> str:
    lower = 0.0
    for upper in boundaries:
        if hours < upper + 1e-9:
            return f"{lower:g}-{upper:g}h"
        lower = upper
    return f">{boundaries[-1]:g}h"


def _collect_archive_rows(config, now, actual: pd.Series):
    fc = config.forecast
    snapshots = read_optimizer_forecast_snapshots(
        config.e3dc_rscp.history_db_path,
        pd.Timestamp(now) - pd.Timedelta(days=fc.load_ensemble_lookback_days),
        now, config.general.timezone,
        fc.load_ensemble_archive_stride_hours)
    rows = []
    latest_actual = pd.Series(actual).dropna()
    if latest_actual.empty:
        return pd.DataFrame(), len(snapshots)
    for issue, frame in snapshots:
        candidates = {
            name.removeprefix("house_load_candidate_").removesuffix("_w"): name
            for name in frame.columns
            if name.startswith("house_load_candidate_") and name.endswith("_w")
        }
        base_col = ("house_load_base_w"
                    if "house_load_base_w" in frame.columns else None)
        if not candidates and base_col is None:
            continue
        temp = frame.get("ambient_temp_c")
        temp_24h = (temp.rolling("24h", min_periods=4).mean()
                    if temp is not None else None)
        for target in frame.index:
            lead_h = (target - issue).total_seconds() / 3600.0
            if lead_h < 0.0 or lead_h > max(
                    fc.load_ensemble_horizon_hours, default=48.0) + 0.1:
                continue
            actual_value = latest_actual.get(target)
            if actual_value is None or not np.isfinite(actual_value):
                continue
            row = {
                "issue": issue, "fold": issue.date().isoformat(),
                "target": target, "lead_h": lead_h,
                "bucket": _bucket_name(
                    lead_h, fc.load_ensemble_horizon_hours),
                "actual": float(actual_value),
            }
            for model, column in candidates.items():
                value = frame.at[target, column]
                row[model] = float(value) if pd.notna(value) else np.nan
            if base_col:
                value = frame.at[target, base_col]
                row["base"] = float(value) if pd.notna(value) else np.nan
            if temp is not None:
                row["temp"] = temp.get(target)
                row["temp_24h"] = temp_24h.get(target)
            rows.append(row)
    return pd.DataFrame(rows), len(snapshots)


def _learn_ensemble(config, rows: pd.DataFrame) -> tuple[dict, dict]:
    fc = config.forecast
    boundaries = fc.load_ensemble_horizon_hours
    names = [_bucket_name((0.0 if i == 0 else boundaries[i - 1]) + 0.01,
                          boundaries)
             for i in range(len(boundaries))]
    models = [name for name in ("similar_days", "ml")
              if name in rows.columns]
    result, diagnostics = {}, {}
    for bucket in names:
        part = rows[rows["bucket"] == bucket] if not rows.empty else rows
        metrics = {}
        for model in models:
            sample = part[["fold", "actual", model]].dropna()
            folds = int(sample["fold"].nunique())
            if sample.empty:
                continue
            error = np.abs(sample["actual"] - sample[model])
            metrics[model] = {
                "folds": folds, "samples": int(len(sample)),
                "mae_w": float(error.mean()),
                "wape_percent": float(
                    100.0 * error.sum() / max(sample["actual"].abs().sum(), 1.0)),
            }
        learned = bool(metrics) and all(
            value["folds"] >= fc.load_ensemble_min_folds
            for value in metrics.values()) and len(metrics) >= 2
        if learned:
            raw = {model: 1.0 / max(25.0, value["mae_w"])
                   for model, value in metrics.items()}
            total = sum(raw.values())
            weights = {model: value / total for model, value in raw.items()}
            floor = max(0.0, fc.load_ensemble_min_weight)
            weights = {model: max(floor, value)
                       for model, value in weights.items()}
            total = sum(weights.values())
            weights = {model: value / total
                       for model, value in weights.items()}
        else:
            selected = (config.forecast.method
                        if config.forecast.method in models
                        else (models[0] if models else "similar_days"))
            weights = {model: float(model == selected) for model in models}
            if not weights:
                weights = {selected: 1.0}
        result[bucket] = weights
        diagnostics[bucket] = {
            "learned": learned, "weights": weights, "metrics": metrics,
        }
    return result, diagnostics


def _learn_temperature(config, rows: pd.DataFrame) -> tuple[dict | None, dict]:
    fc = config.forecast
    required = {"fold", "actual", "base", "temp", "temp_24h"}
    if rows.empty or not required <= set(rows.columns):
        return None, {
            "enabled": bool(fc.temperature_residual_enabled), "learned": False,
            "folds": 0, "samples": 0,
            "min_folds": fc.temperature_residual_min_folds,
        }
    sample = rows[list(required)].dropna()
    folds = int(sample["fold"].nunique())
    diag = {
        "enabled": bool(fc.temperature_residual_enabled), "learned": False,
        "folds": folds, "samples": int(len(sample)),
        "min_folds": fc.temperature_residual_min_folds,
    }
    if (not fc.temperature_residual_enabled
            or folds < fc.temperature_residual_min_folds
            or len(sample) < fc.temperature_residual_min_samples):
        return None, diag
    temp = sample["temp"].to_numpy(dtype=float)
    mean = sample["temp_24h"].to_numpy(dtype=float)
    features = np.column_stack([
        np.clip(10.0 - temp, 0.0, None),
        np.clip(temp - 20.0, 0.0, None),
        np.clip(12.0 - mean, 0.0, None),
        np.clip(mean - 18.0, 0.0, None),
    ])
    residual = (sample["actual"] - sample["base"]).to_numpy(dtype=float)
    lo, hi = np.quantile(residual, [0.05, 0.95])
    residual = np.clip(residual, lo, hi)
    ridge = 100.0
    coef = np.linalg.solve(
        features.T @ features + ridge * np.eye(features.shape[1]),
        features.T @ residual)
    coef = np.clip(coef, -200.0, 200.0)
    predicted = features @ coef
    before = float(np.mean(np.abs(residual)))
    after = float(np.mean(np.abs(residual - predicted)))
    # Ein Residualmodell darf nur produktiv werden, wenn es die archivierten
    # Fehler tatsächlich verbessert.
    if not np.isfinite(after) or after >= before * 0.99:
        diag.update(mae_before_w=before, mae_after_w=after)
        return None, diag
    model = {"coefficients": coef.tolist()}
    diag.update(
        learned=True, coefficients=[round(float(v), 2) for v in coef],
        mae_before_w=before, mae_after_w=after)
    return model, diag


def analyze(config, now, evaluation_actual: pd.Series):
    """Archivbasierte Gewichte und Temperaturmodell höchstens stündlich lernen."""
    key = (pd.Timestamp(now).floor("1h"),
           int(pd.Series(evaluation_actual).notna().sum()))
    if (_ANALYSIS_CACHE["key"] == key and _ANALYSIS_CACHE["value"] is not None
            and time.monotonic() - _ANALYSIS_CACHE["at"] < 3600.0):
        return _ANALYSIS_CACHE["value"]
    rows, snapshots = _collect_archive_rows(config, now, evaluation_actual)
    weights, buckets = _learn_ensemble(config, rows)
    temp_model, temp_diag = _learn_temperature(config, rows)
    recommendation = None
    totals = {}
    for item in buckets.values():
        for model, metric in item.get("metrics", {}).items():
            totals.setdefault(model, []).append(metric["wape_percent"])
    if totals:
        recommendation = min(
            totals, key=lambda model: float(np.mean(totals[model])))
    ensemble_diag = {
        "enabled": bool(config.forecast.load_ensemble_enabled),
        "snapshots": snapshots, "rows": int(len(rows)),
        "buckets": buckets, "recommendation": recommendation,
    }
    value = (weights, temp_model, {
        "ensemble": ensemble_diag, "temperature": temp_diag})
    _ANALYSIS_CACHE.update(at=time.monotonic(), key=key, value=value)
    _STATUS.update(
        updated=pd.Timestamp(now).isoformat(),
        ensemble=ensemble_diag, temperature=temp_diag)
    return value


def combine(candidates: dict, weights: dict, start) -> pd.Series:
    """Kandidaten je Vorlauf-Bucket gewichtet kombinieren."""
    if not candidates:
        return pd.Series(dtype="float64")
    first = next(iter(candidates.values()))
    out = pd.Series(0.0, index=first.index)
    boundaries = []
    # Bucket-Grenzen direkt aus den gelernten Namen rekonstruieren.
    for name in weights:
        if "-" in name and name.endswith("h"):
            boundaries.append(float(name[:-1].split("-")[1]))
    boundaries = sorted(set(boundaries))
    for ts in out.index:
        bucket = _bucket_name(
            max(0.0, (ts - pd.Timestamp(start)).total_seconds() / 3600.0),
            boundaries)
        current = weights.get(bucket) or {}
        available = {model: float(value)
                     for model, value in current.items()
                     if model in candidates and pd.notna(candidates[model].get(ts))}
        if not available:
            model = next(iter(candidates))
            out.at[ts] = candidates[model].at[ts]
            continue
        total = sum(available.values())
        if total <= 0.0:
            model = next(iter(available))
            out.at[ts] = candidates[model].at[ts]
        else:
            out.at[ts] = sum(
                candidates[model].at[ts] * value
                for model, value in available.items()) / total
    return out


def apply_temperature_residual(point: pd.Series, temp: pd.Series | None,
                               model: dict | None, max_percent: float):
    """Gelerntes Heiz-/Kühlresidual mit harter Sicherheitsgrenze anwenden."""
    base = pd.Series(point, dtype="float64")
    if model is None or temp is None or base.empty:
        return base, pd.Series(0.0, index=base.index)
    temperature_all = pd.Series(temp).sort_index()
    mean_all = temperature_all.rolling("24h", min_periods=4).mean()
    temperature = temperature_all.reindex(base.index)
    mean = mean_all.reindex(base.index)
    coef = np.asarray(model["coefficients"], dtype=float)
    features = np.column_stack([
        np.clip(10.0 - temperature.values, 0.0, None),
        np.clip(temperature.values - 20.0, 0.0, None),
        np.clip(12.0 - mean.values, 0.0, None),
        np.clip(mean.values - 18.0, 0.0, None),
    ])
    adjustment = np.nan_to_num(features @ coef, nan=0.0)
    limit = np.maximum(100.0, np.abs(base.values) * max(0.0, max_percent))
    adjustment = np.clip(adjustment, -limit, limit)
    delta = pd.Series(adjustment, index=base.index)
    return (base + delta).clip(lower=0.0), delta
