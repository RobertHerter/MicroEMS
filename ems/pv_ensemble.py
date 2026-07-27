"""Lernendes Mehrmodell-Ensemble für die lokale pvlib-Prognose.

Jedes Wettermodell wird separat durch pvlib gerechnet und als unveränderlicher
Forecast-Snapshot archiviert. Aus den echten Rolling-Origin-Fehlern werden
Gewichte je Vorlaufzeit gelernt. P10/P90 verbinden die aktuelle Modellstreuung
mit empirischen Ist/Prognose-Residualen der bisherigen Ensemble-Ausgabe.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Iterable

import numpy as np
import pandas as pd


def member_source(model: str, aggregate_source: str) -> str:
    """Stabile Archiv-ID eines Wettermodell-/Array-Paars."""
    array = aggregate_source.split("pvmodel:", 1)[-1]
    return f"pvmodel-member:{model}:{array}"


def model_source_map(models: Iterable[str],
                     aggregate_sources: Iterable[str]) -> dict[str, list[str]]:
    return {
        model: [member_source(model, source) for source in aggregate_sources]
        for model in models
    }


def _bucket_edges(hours: Iterable[float]) -> list[float]:
    return sorted({float(v) for v in hours if float(v) > 0.0})


def bucket_ranges(hours: Iterable[float]) -> list[tuple[str, float, float]]:
    edges = _bucket_edges(hours)
    out = []
    low = 0.0
    for high in edges:
        out.append((f"{low:g}-{high:g}h", low, high))
        low = high
    out.append((f"{low:g}h+", low, float("inf")))
    return out


def bucket_name(lead_hours: float, hours: Iterable[float]) -> str:
    for name, low, high in bucket_ranges(hours):
        if low <= lead_hours < high:
            return name
    return bucket_ranges(hours)[-1][0]


def _actuals(db: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    con = sqlite3.connect(db, timeout=10)
    try:
        rows = con.execute(
            "SELECT ts, pv_w FROM actuals WHERE ts>=? AND ts<? "
            "AND pv_w IS NOT NULL ORDER BY ts",
            (start.tz_convert("UTC").isoformat(),
             end.tz_convert("UTC").isoformat())).fetchall()
    finally:
        con.close()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([row[0] for row in rows], utc=True, format="ISO8601")
    return pd.Series([float(row[1]) for row in rows], index=idx,
                     dtype="float64")


def _archive_groups(db: str, groups: Dict[str, list[str]], start: pd.Timestamp,
                    end: pd.Timestamp) -> pd.DataFrame:
    """Archivquellen je Gruppe/Issue/Target vollständig summieren."""
    source_group = {
        source: group for group, sources in groups.items() for source in sources
    }
    if not source_group:
        return pd.DataFrame(columns=["group", "issue", "target", "pred"])
    marks = ",".join("?" for _ in source_group)
    con = sqlite3.connect(db, timeout=10)
    try:
        rows = con.execute(
            "SELECT source, issue_ts, target_ts, pv_w "
            f"FROM pv_forecast_archive WHERE source IN ({marks}) "
            "AND target_ts>=? AND target_ts<? AND pv_w IS NOT NULL",
            (*source_group, start.tz_convert("UTC").isoformat(),
             end.tz_convert("UTC").isoformat())).fetchall()
    finally:
        con.close()
    if not rows:
        return pd.DataFrame(columns=["group", "issue", "target", "pred"])
    frame = pd.DataFrame(rows, columns=["source", "issue", "target", "pred"])
    frame["group"] = frame["source"].map(source_group)
    frame["issue"] = pd.to_datetime(frame["issue"], utc=True, format="ISO8601")
    frame["target"] = pd.to_datetime(frame["target"], utc=True, format="ISO8601")
    expected = {group: len(sources) for group, sources in groups.items()}
    summed = frame.groupby(
        ["group", "issue", "target"], as_index=False).agg(
            pred=("pred", "sum"), n_sources=("source", "nunique"))
    summed = summed[
        summed.apply(
            lambda row: row["n_sources"] >= expected.get(row["group"], 1),
            axis=1)
    ].copy()
    summed["lead_h"] = (
        summed["target"] - summed["issue"]).dt.total_seconds() / 3600.0
    return summed[summed["lead_h"] >= 0.0]


def _paired_bucket(frame: pd.DataFrame, actual: pd.Series, low: float,
                   high: float) -> pd.DataFrame:
    """Pro Target/Gruppe genau die jüngste Prognose innerhalb des Lead-Buckets."""
    if frame.empty or actual.empty:
        return pd.DataFrame()
    part = frame[(frame["lead_h"] >= low) & (frame["lead_h"] < high)]
    if part.empty:
        return pd.DataFrame()
    part = (part.sort_values("issue")
            .drop_duplicates(["group", "target"], keep="last"))
    wide = part.pivot(index="target", columns="group", values="pred")
    wide["actual"] = actual.reindex(wide.index)
    return wide.dropna()


def learn_weights(db: str, model_sources: Dict[str, list[str]], now,
                  lookback_days: int, min_samples: int,
                  horizon_hours: Iterable[float],
                  min_weight: float = 0.05) -> dict:
    """Inverse-MAE-Gewichte je Prognosehorizont aus echten Archivfehlern.

    Die Schätzung wird mit Gleichgewicht geschrumpft, bis viermal die
    Mindeststichprobe erreicht ist. So kann ein kurzer Wetterabschnitt kein
    Modell sofort dominieren.
    """
    issue = pd.Timestamp(now)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue = issue.tz_convert("UTC")
    start = issue - pd.Timedelta(days=max(1, int(lookback_days)))
    archived = _archive_groups(db, model_sources, start, issue)
    actual = _actuals(db, start, issue)
    models = list(model_sources)
    equal = {model: 1.0 / len(models) for model in models} if models else {}
    result = {}
    for name, low, high in bucket_ranges(horizon_hours):
        pairs = _paired_bucket(archived, actual, low, high)
        if not pairs.empty:
            pairs = pairs.dropna(subset=models)
            pairs = pairs[
                pairs[["actual", *models]].max(axis=1) >= 50.0]
        n = int(len(pairs))
        mae = {}
        if n:
            mae = {
                model: float(np.mean(np.abs(
                    pairs[model].to_numpy() - pairs["actual"].to_numpy())))
                for model in models
            }
        if n < max(1, int(min_samples)) or not mae:
            weights = dict(equal)
            learned = False
        else:
            # Ein kleiner Sockel verhindert extreme Gewichte bei fast perfektem
            # MAE und hält die Schätzung numerisch stabil.
            raw = {model: 1.0 / (value + 100.0) ** 2
                   for model, value in mae.items()}
            total = sum(raw.values()) or 1.0
            raw = {model: value / total for model, value in raw.items()}
            alpha = min(1.0, n / max(1.0, 4.0 * min_samples))
            weights = {
                model: (1.0 - alpha) * equal[model] + alpha * raw[model]
                for model in models
            }
            floor = max(0.0, min(float(min_weight), 0.99 / len(models)))
            weights = {model: max(floor, value)
                       for model, value in weights.items()}
            total = sum(weights.values()) or 1.0
            weights = {model: value / total
                       for model, value in weights.items()}
            learned = True
        result[name] = {
            "weights": weights, "n": n, "mae_w": mae, "learned": learned,
        }
    return result


def residual_quantiles(db: str, aggregate_sources: list[str], now,
                       lookback_days: int, min_samples: int,
                       horizon_hours: Iterable[float],
                       fallback_low: float, fallback_high: float) -> dict:
    """Multiplikative Ist/Ensemble-Quantile je Vorlaufzeit."""
    issue = pd.Timestamp(now)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue = issue.tz_convert("UTC")
    start = issue - pd.Timedelta(days=max(1, int(lookback_days)))
    archived = _archive_groups(
        db, {"ensemble": aggregate_sources}, start, issue)
    actual = _actuals(db, start, issue)
    out = {}
    for name, low, high in bucket_ranges(horizon_hours):
        pairs = _paired_bucket(archived, actual, low, high)
        if not pairs.empty:
            pairs = pairs[(pairs["ensemble"] >= 100.0)
                          & (pairs["actual"] >= 0.0)]
        n = int(len(pairs))
        if n >= max(8, int(min_samples)):
            ratio = (pairs["actual"] / pairs["ensemble"]).to_numpy()
            q10 = float(np.clip(np.quantile(ratio, 0.10), 0.05, 1.0))
            q90 = float(np.clip(np.quantile(ratio, 0.90), 1.0, 4.0))
            learned = True
        else:
            q10, q90 = fallback_low, fallback_high
            learned = False
        out[name] = {
            "q10_ratio": q10, "q90_ratio": q90,
            "n": n, "learned": learned,
        }
    return out


def _weighted_quantile(values: dict[str, float], weights: dict[str, float],
                       quantile: float) -> float:
    pairs = sorted((float(value), max(0.0, float(weights.get(model, 0.0))))
                   for model, value in values.items())
    total = sum(weight for _, weight in pairs)
    if not pairs:
        return 0.0
    if total <= 0.0:
        return float(np.quantile([value for value, _ in pairs], quantile))
    threshold = quantile * total
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return pairs[-1][0]


def combine(model_outputs: dict, weights_by_bucket: dict,
            residuals_by_bucket: dict, issue_time,
            horizon_hours: Iterable[float]) -> tuple[dict, dict]:
    """Modell-/Array-Prognosen zu kompatiblen ``pvmodel:<Array>``-Quellen
    kombinieren. P10/P90 nutzen Modellstreuung UND empirische Residuen."""
    issue = pd.Timestamp(issue_time)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue = issue.tz_convert("UTC")
    models = list(model_outputs)
    aggregate_sources = sorted({
        source for outputs in model_outputs.values() for source in outputs
    })
    targets = sorted({
        target for outputs in model_outputs.values()
        for mapping in outputs.values() for target in mapping
    })
    combined = {source: {} for source in aggregate_sources}
    usage = {name: 0 for name in weights_by_bucket}
    for target in targets:
        target_ts = pd.Timestamp(target)
        if target_ts.tzinfo is None:
            target_ts = target_ts.tz_localize("UTC")
        lead = max(0.0, (target_ts.tz_convert("UTC") - issue).total_seconds()
                   / 3600.0)
        bucket = bucket_name(lead, horizon_hours)
        configured = (weights_by_bucket.get(bucket) or {}).get("weights", {})
        available = [
            model for model in models
            if all(target in model_outputs[model].get(source, {})
                   for source in aggregate_sources)
        ]
        if not available:
            continue
        weights = {model: max(0.0, float(configured.get(model, 0.0)))
                   for model in available}
        total_weight = sum(weights.values())
        if total_weight <= 0.0:
            weights = {model: 1.0 / len(available) for model in available}
        else:
            weights = {model: value / total_weight
                       for model, value in weights.items()}

        per_model_total = {
            model: sum(model_outputs[model][source][target][0]
                       for source in aggregate_sources)
            for model in available
        }
        mean_total = sum(weights[model] * per_model_total[model]
                         for model in available)
        spread_low = _weighted_quantile(per_model_total, weights, 0.10)
        spread_high = _weighted_quantile(per_model_total, weights, 0.90)
        residual = residuals_by_bucket.get(bucket) or {}
        residual_low = mean_total * float(residual.get("q10_ratio", 1.0))
        residual_high = mean_total * float(residual.get("q90_ratio", 1.0))
        low_total = min(mean_total, spread_low, residual_low)
        high_total = max(mean_total, spread_high, residual_high)

        array_means = {
            source: sum(weights[model]
                        * model_outputs[model][source][target][0]
                        for model in available)
            for source in aggregate_sources
        }
        denom = sum(array_means.values())
        for source, point in array_means.items():
            share = point / denom if denom > 0.0 else 0.0
            combined[source][target] = (
                float(point), float(low_total * share),
                float(high_total * share))
        usage[bucket] = usage.get(bucket, 0) + 1
    diagnostics = {
        "models": models, "weights": weights_by_bucket,
        "residuals": residuals_by_bucket, "slots_by_bucket": usage,
    }
    return combined, diagnostics
