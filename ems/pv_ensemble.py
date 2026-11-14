"""Lernendes Mehrmodell-Ensemble für die lokale pvlib-Prognose.

Jedes Wettermodell wird separat durch pvlib gerechnet und als unveränderlicher
Forecast-Snapshot archiviert. Aus den echten Rolling-Origin-Fehlern werden
Gewichte je Vorlaufzeit gelernt. P10/P90 verbinden die aktuelle Modellstreuung
mit empirischen Ist/Prognose-Residualen der bisherigen Ensemble-Ausgabe.
"""
from __future__ import annotations

import copy
import os
import threading
import time
from contextlib import closing
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

from .local_history import connect

# Gelernte Gewichte und Quantilbaender je Prozess merken. Der Betrieb fragt in
# JEDEM 15-min-Zyklus danach, und jede Antwort las das ganze 45-Tage-Archiv
# (rund 2 Mio. Zeilen, 34 s auf dem Pi). Eine Stunde neuer Daten aendert an
# einem 45-Tage-Fenster praktisch nichts.
LERN_CACHE_S = 3600.0
_LERN_CACHE: dict = {}
_LERN_LOCK = threading.RLock()
# Anteil der Ist-Slots eines Prognoseintervalls, der fuer ein Paar vorliegen
# muss (bei Stundenprognose und 15-min-Ist: 3 von 4).
MIN_IST_ANTEIL = 0.75


def clear_learning_cache() -> None:
    """Gemerkte Lernergebnisse verwerfen (Tests, erzwungenes Neulernen)."""
    with _LERN_LOCK:
        _LERN_CACHE.clear()


def _cached(key: tuple, cache_s: float, compute):
    """``compute()`` hoechstens alle ``cache_s`` Sekunden je Schluessel
    ausfuehren. Threadsicher; gerechnet wird unter der Sperre, damit zwei
    Threads das Archiv nicht gleichzeitig laden. Zurueck kommt eine Kopie -
    der Aufrufer darf sie veraendern."""
    if not cache_s or cache_s <= 0:
        return compute()
    with _LERN_LOCK:
        hit = _LERN_CACHE.get(key)
        jetzt = time.monotonic()
        if hit is not None and jetzt < hit[0]:
            return copy.deepcopy(hit[1])
        value = compute()
        _LERN_CACHE[key] = (time.monotonic() + float(cache_s), value)
        return copy.deepcopy(value)


def _db_key(db: str) -> str:
    return os.path.abspath(str(db))


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
    with closing(connect(db)) as con:
        rows = con.execute(
            "SELECT ts, pv_w FROM actuals WHERE ts>=? AND ts<? "
            "AND pv_w IS NOT NULL ORDER BY ts",
            (start.tz_convert("UTC").isoformat(),
             end.tz_convert("UTC").isoformat())).fetchall()
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
    with closing(connect(db)) as con:
        rows = con.execute(
            "SELECT source, issue_ts, target_ts, pv_w "
            f"FROM pv_forecast_archive WHERE source IN ({marks}) "
            "AND target_ts>=? AND target_ts<? AND pv_w IS NOT NULL",
            (*source_group, start.tz_convert("UTC").isoformat(),
             end.tz_convert("UTC").isoformat())).fetchall()
    if not rows:
        return pd.DataFrame(columns=["group", "issue", "target", "pred"])
    frame = pd.DataFrame(rows, columns=["source", "issue", "target", "pred"])
    del rows
    frame["group"] = frame["source"].map(source_group)
    # Zeitstempel wiederholen sich millionenfach (wenige tausend Ausgabe- und
    # Zielzeiten): jeden nur einmal parsen.
    for spalte in ("issue", "target"):
        einmal = frame[spalte].unique()
        frame[spalte] = frame[spalte].map(pd.Series(
            pd.to_datetime(einmal, utc=True, format="ISO8601"), index=einmal))
    expected = {group: len(sources) for group, sources in groups.items()}
    summed = frame.groupby(
        ["group", "issue", "target"], as_index=False).agg(
            pred=("pred", "sum"), n_sources=("source", "nunique"))
    # Spaltenweise statt zeilenweisem apply (das allein kostete auf dem Pi den
    # Grossteil der Laufzeit).
    summed = summed[
        summed["n_sources"] >= summed["group"].map(expected).fillna(1)
    ].copy()
    summed["lead_h"] = (
        summed["target"] - summed["issue"]).dt.total_seconds() / 3600.0
    return summed[summed["lead_h"] >= 0.0]


def _forecast_step(frame: pd.DataFrame) -> Optional[pd.Timedelta]:
    """Intervalllaenge der Prognose: Abstand der Zielmarken INNERHALB einer
    Ausgabe (Median). None, wenn keine Ausgabe zwei Marken enthaelt."""
    if frame.empty:
        return None
    part = frame.sort_values(["group", "issue", "target"])
    gleich = ((part["group"] == part["group"].shift())
              & (part["issue"] == part["issue"].shift()))
    diffs = part["target"].diff()[gleich]
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return None
    return pd.Timedelta(diffs.median())


def _interval_actual(actual: pd.Series, targets: pd.DatetimeIndex,
                     step: Optional[pd.Timedelta]) -> pd.Series:
    """Ist-Mittel je Prognoseintervall [Marke, Marke + step).

    Die Prognose ist ein Mittel ueber ihr ganzes Intervall (pvlib: eine Stunde,
    auf den Intervall-Anfang gestempelt). Bis 14.11.2026 wurde sie mit dem
    EINEN 15-min-Ist-Slot auf der Marke gepaart - also mit der ersten
    Viertelstunde: vormittags systematisch zu wenig Ist, nachmittags zu viel.
    Gewertet wird ein Intervall nur, wenn mindestens MIN_IST_ANTEIL seiner
    Ist-Slots vorliegen. Ist die Prognose nicht groeber als das Ist (oder ihre
    Intervalllaenge unbekannt), bleibt es beim Wert auf der Marke."""
    if actual.empty or len(targets) == 0:
        return pd.Series(np.nan, index=targets, dtype="float64")
    actual = actual.sort_index()
    ist_schritt = pd.Series(actual.index).diff().median()
    if (step is None or pd.isna(ist_schritt)
            or ist_schritt <= pd.Timedelta(0) or step <= ist_schritt):
        return actual.reindex(targets)
    zeiten = actual.index.as_unit("ns").asi8
    marken = pd.DatetimeIndex(targets).as_unit("ns").asi8
    summe = np.concatenate(([0.0], np.cumsum(actual.to_numpy(dtype=float))))
    lo = np.searchsorted(zeiten, marken, side="left")
    hi = np.searchsorted(zeiten, marken + int(step.value), side="left")
    n = hi - lo
    noetig = max(1, int(np.ceil(MIN_IST_ANTEIL * (step / ist_schritt) - 1e-9)))
    mittel = np.where(n >= noetig, (summe[hi] - summe[lo]) / np.maximum(n, 1),
                      np.nan)
    return pd.Series(mittel, index=targets, dtype="float64")


def _paired_bucket(frame: pd.DataFrame, actual: pd.Series, low: float,
                   high: float,
                   step: Optional[pd.Timedelta] = None) -> pd.DataFrame:
    """Pro Target/Gruppe genau die jüngste Prognose innerhalb des Lead-Buckets,
    gepaart mit dem Ist-MITTEL desselben Prognoseintervalls (``step``)."""
    if frame.empty or actual.empty:
        return pd.DataFrame()
    part = frame[(frame["lead_h"] >= low) & (frame["lead_h"] < high)]
    if part.empty:
        return pd.DataFrame()
    part = (part.sort_values("issue")
            .drop_duplicates(["group", "target"], keep="last"))
    wide = part.pivot(index="target", columns="group", values="pred")
    wide["actual"] = _interval_actual(actual, wide.index, step)
    return wide.dropna()


def learn_weights(db: str, model_sources: Dict[str, list[str]], now,
                  lookback_days: int, min_samples: int,
                  horizon_hours: Iterable[float],
                  min_weight: float = 0.05,
                  cache_s: float = LERN_CACHE_S) -> dict:
    """Inverse-MAE-Gewichte je Prognosehorizont aus echten Archivfehlern.

    Die Schätzung wird mit Gleichgewicht geschrumpft, bis viermal die
    Mindeststichprobe erreicht ist. So kann ein kurzer Wetterabschnitt kein
    Modell sofort dominieren.

    Das Ergebnis wird je Prozess ``cache_s`` Sekunden gemerkt (Schluessel:
    Datenbank + alle Parameter ausser ``now``); ``cache_s=0`` rechnet immer neu.
    """
    horizon_hours = [float(v) for v in horizon_hours]
    key = ("weights", _db_key(db),
           tuple((model, tuple(sources))
                 for model, sources in model_sources.items()),
           int(lookback_days), int(min_samples), tuple(horizon_hours),
           float(min_weight))
    return _cached(key, cache_s, lambda: _learn_weights(
        db, model_sources, now, lookback_days, min_samples, horizon_hours,
        min_weight))


def _learn_weights(db: str, model_sources: Dict[str, list[str]], now,
                   lookback_days: int, min_samples: int,
                   horizon_hours: Iterable[float],
                   min_weight: float = 0.05) -> dict:
    issue = pd.Timestamp(now)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue = issue.tz_convert("UTC")
    start = issue - pd.Timedelta(days=max(1, int(lookback_days)))
    archived = _archive_groups(db, model_sources, start, issue)
    actual = _actuals(db, start, issue)
    step = _forecast_step(archived)
    models = list(model_sources)
    equal = {model: 1.0 / len(models) for model in models} if models else {}
    result = {}
    for name, low, high in bucket_ranges(horizon_hours):
        pairs = _paired_bucket(archived, actual, low, high, step)
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
                       fallback_low: float, fallback_high: float,
                       timezone: str = "UTC",
                       cache_s: float = LERN_CACHE_S) -> dict:
    """Multiplikative Ist/Ensemble-Quantile je Vorlaufzeit und Wetterlage.

    Innerhalb jedes Vorlauf-Buckets wird das Band zusätzlich nach
    meteorologischer Saison und prognostiziertem Leistungsniveau gelernt.
    Schwach besetzte Zellen fallen hierarchisch auf Leistungsniveau, Saison
    und zuletzt das robuste Vorlaufband zurück.

    Wie ``learn_weights`` je Prozess ``cache_s`` Sekunden gemerkt.
    """
    horizon_hours = [float(v) for v in horizon_hours]
    aggregate_sources = list(aggregate_sources)
    key = ("residuals", _db_key(db), tuple(aggregate_sources),
           int(lookback_days), int(min_samples), tuple(horizon_hours),
           float(fallback_low), float(fallback_high), str(timezone))
    return _cached(key, cache_s, lambda: _residual_quantiles(
        db, aggregate_sources, now, lookback_days, min_samples,
        horizon_hours, fallback_low, fallback_high, timezone))


def _residual_quantiles(db: str, aggregate_sources: list[str], now,
                        lookback_days: int, min_samples: int,
                        horizon_hours: Iterable[float],
                        fallback_low: float, fallback_high: float,
                        timezone: str = "UTC") -> dict:
    issue = pd.Timestamp(now)
    if issue.tzinfo is None:
        issue = issue.tz_localize("UTC")
    issue = issue.tz_convert("UTC")
    start = issue - pd.Timedelta(days=max(1, int(lookback_days)))
    archived = _archive_groups(
        db, {"ensemble": aggregate_sources}, start, issue)
    actual = _actuals(db, start, issue)
    step = _forecast_step(archived)
    out = {}
    for name, low, high in bucket_ranges(horizon_hours):
        pairs = _paired_bucket(archived, actual, low, high, step)
        if not pairs.empty:
            pairs = pairs[(pairs["ensemble"] >= 100.0)
                          & (pairs["actual"] >= 0.0)]
        n = int(len(pairs))
        required = max(8, int(min_samples))
        if n >= required:
            ratio = (pairs["actual"] / pairs["ensemble"]).to_numpy()
            q10 = float(np.clip(np.quantile(ratio, 0.10), 0.05, 1.0))
            q90 = float(np.clip(np.quantile(ratio, 0.90), 1.0, 4.0))
            learned = True
        else:
            q10, q90 = fallback_low, fallback_high
            learned = False
        conditions = {}
        thresholds = {"low_w": None, "high_w": None}
        if n:
            power = pairs["ensemble"].to_numpy(dtype=float)
            low_w, high_w = np.quantile(power, [1.0 / 3.0, 2.0 / 3.0])
            thresholds = {"low_w": float(low_w), "high_w": float(high_w)}
            local = pairs.index.tz_convert(timezone)
            condition_frame = pd.DataFrame({
                "ratio": pairs["actual"].to_numpy(dtype=float) / power,
                "season": ((local.month % 12) // 3).astype(int),
                "power": np.where(
                    power < low_w, "low",
                    np.where(power < high_w, "mid", "high")),
            }, index=pairs.index)
            conditional_required = max(8, required // 2)
            selectors = {}
            for season in sorted(condition_frame["season"].unique()):
                selectors[f"season:{season}"] = (
                    condition_frame["season"] == season)
            for power_name in ("low", "mid", "high"):
                selectors[f"power:{power_name}"] = (
                    condition_frame["power"] == power_name)
            for season in sorted(condition_frame["season"].unique()):
                for power_name in ("low", "mid", "high"):
                    selectors[f"season:{season}|power:{power_name}"] = (
                        (condition_frame["season"] == season)
                        & (condition_frame["power"] == power_name))
            for key, selector in selectors.items():
                sample = condition_frame.loc[selector, "ratio"].to_numpy()
                sample_n = int(len(sample))
                if sample_n < conditional_required:
                    continue
                raw_low = float(np.clip(
                    np.quantile(sample, 0.10), 0.05, 1.0))
                raw_high = float(np.clip(
                    np.quantile(sample, 0.90), 1.0, 4.0))
                # Kleine Teilgruppen vorsichtig zum stabilen Vorlaufband
                # schrumpfen; ab vier Mindeststichproben gilt die Zelle voll.
                alpha = min(
                    1.0, sample_n / max(1.0, 4.0 * conditional_required))
                conditions[key] = {
                    "q10_ratio": float(
                        np.clip((1.0 - alpha) * q10 + alpha * raw_low,
                                0.05, 1.0)),
                    "q90_ratio": float(
                        np.clip((1.0 - alpha) * q90 + alpha * raw_high,
                                1.0, 4.0)),
                    "n": sample_n, "learned": True,
                }
        out[name] = {
            "q10_ratio": q10, "q90_ratio": q90,
            "n": n, "learned": learned, "conditions": conditions,
            "power_thresholds_w": thresholds, "timezone": timezone,
        }
    return out


def _conditional_residual(residual: dict, target: pd.Timestamp,
                          power_w: float) -> tuple[dict, str]:
    """Beste belastbare Kondition eines Vorlaufbandes auswählen."""
    conditions = residual.get("conditions") or {}
    thresholds = residual.get("power_thresholds_w") or {}
    low_w, high_w = thresholds.get("low_w"), thresholds.get("high_w")
    if low_w is None or high_w is None:
        return residual, "lead"
    if power_w < float(low_w):
        power = "low"
    elif power_w < float(high_w):
        power = "mid"
    else:
        power = "high"
    local = target.tz_convert(residual.get("timezone") or "UTC")
    season = int((local.month % 12) // 3)
    for key in (
        f"season:{season}|power:{power}",
        f"power:{power}",
        f"season:{season}",
    ):
        selected = conditions.get(key)
        if selected and selected.get("learned"):
            return selected, key
    return residual, "lead"


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
    condition_usage = {name: {} for name in weights_by_bucket}
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
        selected_residual, condition = _conditional_residual(
            residual, target_ts, mean_total)
        residual_low = mean_total * float(
            selected_residual.get("q10_ratio", 1.0))
        residual_high = mean_total * float(
            selected_residual.get("q90_ratio", 1.0))
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
        bucket_conditions = condition_usage.setdefault(bucket, {})
        bucket_conditions[condition] = bucket_conditions.get(condition, 0) + 1
    diagnostics = {
        "models": models, "weights": weights_by_bucket,
        "residuals": residuals_by_bucket, "slots_by_bucket": usage,
        "condition_slots_by_bucket": condition_usage,
    }
    return combined, diagnostics
