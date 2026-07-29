"""Sichere Champion-/Challenger-Entscheidungen für Kalibrierungen."""
from __future__ import annotations

import numpy as np
import pandas as pd


_DAYPARTS = (
    (0, 6, "Nacht"), (6, 12, "Morgen"),
    (12, 18, "Mittag"), (18, 24, "Abend"),
)


def _point_metrics(actual: pd.Series, predicted: pd.Series) -> dict:
    frame = pd.DataFrame({"actual": actual, "predicted": predicted}).dropna()
    if frame.empty:
        return {"n": 0, "wape_pct": None, "mae_w": None, "bias_w": None}
    error = frame["predicted"] - frame["actual"]
    denom = float(frame["actual"].abs().sum())
    return {
        "n": int(len(frame)),
        "wape_pct": (
            100.0 * float(error.abs().sum()) / denom if denom > 1e-9 else None),
        "mae_w": float(error.abs().mean()),
        "bias_w": float(error.mean()),
    }


def compare_point_forecasts(
        actual: pd.Series, champion: pd.Series, challenger: pd.Series,
        timezone: str, min_samples: int = 96,
        min_improvement_percent: float = 1.0,
        max_segment_degradation_pct: float = 5.0,
        max_bias_increase_w: float = 75.0,
        champion_available: bool = True) -> dict:
    """Zwei korrigierte Punktprognosen auf demselben Holdout vergleichen."""
    frame = pd.DataFrame({
        "actual": actual, "champion": champion, "challenger": challenger,
    }).dropna()
    champion_metrics = _point_metrics(frame["actual"], frame["champion"])
    challenger_metrics = _point_metrics(frame["actual"], frame["challenger"])
    n = len(frame)
    result = {
        "n": int(n), "champion": champion_metrics,
        "challenger": challenger_metrics, "promote": False,
        "status": "insufficient", "reason": "zu wenig unabhängige Prüfdaten",
        "improvement_percent": None, "worst_segment_degradation_pct": None,
    }
    if n < max(1, int(min_samples)):
        return result
    if not champion_available:
        result.update(
            promote=True, status="promoted",
            reason="erster belastbarer Kalibrierungsstand")
        return result
    champion_wape = champion_metrics.get("wape_pct")
    challenger_wape = challenger_metrics.get("wape_pct")
    if champion_wape is None or challenger_wape is None:
        return result
    improvement = 100.0 * (
        champion_wape - challenger_wape) / max(champion_wape, 1e-9)
    local = frame.index.tz_convert(timezone)
    degradations = []
    segments = {}
    for low, high, name in _DAYPARTS:
        mask = (local.hour >= low) & (local.hour < high)
        if int(mask.sum()) < max(8, min_samples // 8):
            continue
        cm = _point_metrics(
            frame.loc[mask, "actual"], frame.loc[mask, "champion"])
        xm = _point_metrics(
            frame.loc[mask, "actual"], frame.loc[mask, "challenger"])
        degradation = (
            float(xm["wape_pct"]) - float(cm["wape_pct"])
            if cm["wape_pct"] is not None and xm["wape_pct"] is not None
            else 0.0)
        degradations.append(degradation)
        segments[name] = {
            "champion_wape_pct": cm["wape_pct"],
            "challenger_wape_pct": xm["wape_pct"],
            "degradation_pct": degradation,
        }
    worst = max(degradations, default=0.0)
    bias_increase = (
        abs(float(challenger_metrics["bias_w"]))
        - abs(float(champion_metrics["bias_w"])))
    promote = (
        improvement >= float(min_improvement_percent)
        and worst <= float(max_segment_degradation_pct)
        and bias_increase <= float(max_bias_increase_w)
    )
    if improvement < float(min_improvement_percent):
        reason = (
            f"Verbesserung {improvement:+.1f} % unter Mindestvorsprung "
            f"{float(min_improvement_percent):.1f} %")
    elif worst > float(max_segment_degradation_pct):
        reason = (
            f"Teilzeitraum verschlechtert sich um {worst:.1f} Prozentpunkte")
    elif bias_increase > float(max_bias_increase_w):
        reason = f"absoluter Bias steigt um {bias_increase:.0f} W"
    else:
        reason = f"WAPE verbessert sich um {improvement:.1f} %"
    result.update(
        promote=bool(promote),
        status="promoted" if promote else "held",
        reason=reason,
        improvement_percent=round(improvement, 2),
        worst_segment_degradation_pct=round(worst, 2),
        segments=segments,
    )
    return result


def apply_load_profile(series: pd.Series, profile: dict | None,
                       timezone: str) -> pd.Series:
    """Produktive Stundenkorrektur eines Lastprofils anwenden."""
    if series.empty or not profile:
        return series.copy()
    hourly = profile.get("load_hourly") or {}
    global_factor = float(profile.get("load_global", 1.0))
    local = series.index.tz_convert(timezone)
    factors = [
        float(hourly.get(ts.hour, hourly.get(str(ts.hour), global_factor)))
        for ts in local
    ]
    return series * pd.Series(factors, index=series.index)


def interval_score(ratios, low_ratio: float, high_ratio: float,
                   alpha: float = 0.20) -> dict:
    """Normierter Winkler-Score; kleiner ist besser."""
    values = np.asarray(ratios, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"n": 0, "score": None, "below_pct": None, "above_pct": None}
    low, high = float(low_ratio), float(high_ratio)
    width = high - low
    score = np.full(len(values), width, dtype=float)
    below = values < low
    above = values > high
    score[below] += 2.0 / alpha * (low - values[below])
    score[above] += 2.0 / alpha * (values[above] - high)
    return {
        "n": int(len(values)), "score": float(score.mean()),
        "below_pct": 100.0 * float(below.mean()),
        "above_pct": 100.0 * float(above.mean()),
    }


def compare_intervals(
        ratios, champion_low: float, champion_high: float,
        challenger_low: float, challenger_high: float,
        min_samples: int = 96, min_improvement_percent: float = 1.0) -> dict:
    """Champion-/Challenger-Entscheidung für ein P10/P90-Band."""
    champion = interval_score(ratios, champion_low, champion_high)
    challenger = interval_score(ratios, challenger_low, challenger_high)
    result = {
        "n": challenger["n"], "champion": champion,
        "challenger": challenger, "promote": False, "status": "insufficient",
        "reason": "zu wenig unabhängige Prüfdaten", "improvement_percent": None,
    }
    if challenger["n"] < max(1, int(min_samples)):
        return result
    improvement = 100.0 * (
        float(champion["score"]) - float(challenger["score"])
    ) / max(float(champion["score"]), 1e-9)
    promote = improvement >= float(min_improvement_percent)
    result.update(
        promote=bool(promote), status="promoted" if promote else "held",
        reason=(
            f"Intervallscore verbessert sich um {improvement:.1f} %"
            if promote else
            f"Verbesserung {improvement:+.1f} % unter Mindestvorsprung "
            f"{float(min_improvement_percent):.1f} %"),
        improvement_percent=round(improvement, 2),
    )
    return result
