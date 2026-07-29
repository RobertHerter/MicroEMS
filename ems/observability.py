"""Auswertungen fürs Dashboard: Ersparnis über die Zeit und Prognosegüte.

Beide Funktionen sind reine Leser/Aggregatoren über die lokale Historie (kein
RSCP/MQTT, keine Steuerung) und damit direkt testbar. Sie werden on-demand von
den Dashboard-Endpoints aufgerufen, nicht im Steuer-Zyklus.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .quality import bias_w, enough, shortfall_note


def _wape(actual, pred) -> float:
    """Weighted Absolute Percentage Error (%). 0 bei leerer Basis."""
    actual = np.asarray(actual, dtype=float)
    pred = np.asarray(pred, dtype=float)
    denom = float(np.sum(np.abs(actual)))
    if denom <= 1e-9:
        return 0.0
    return round(100.0 * float(np.sum(np.abs(pred - actual))) / denom, 2)


def _metrics(actual: list, pred: list) -> dict:
    a, p = np.asarray(actual, dtype=float), np.asarray(pred, dtype=float)
    if len(a) == 0:
        return {"n": 0, "wape_pct": None, "bias_w": None, "mae_w": None}
    return {"n": int(len(a)), "wape_pct": _wape(a, p),
            "bias_w": round(bias_w(a, p), 1),
            "mae_w": round(float(np.mean(np.abs(p - a))), 1)}


_LEAD_BUCKETS = ((0.0, 6.0, "0–6 h"),
                 (6.0, 24.0, "6–24 h"),
                 (24.0, 48.01, "24–48 h"))


def _json_values(series: pd.Series) -> list:
    """Numerische Reihe JSON-sicher machen; Lücken bleiben ehrliche ``null``."""
    return [None if pd.isna(value) else round(float(value), 1)
            for value in series]


def _forecast_error_heatmaps(config, snapshots: list, start, now) -> dict:
    """Produktive Prognosefehler nach lokaler Zielstunde und Vorlaufzeit.

    Jeder 6-h-Origin wird einmal gewertet. Dadurch dominieren die viertel-
    stündlichen Replan-Läufe nicht künstlich die Statistik.
    """
    from .local_history import read_actual, read_house_load

    db, tz = config.e3dc_rscp.history_db_path, config.general.timezone
    actual = {
        "pv": read_actual(db, "pv_w", start, now, tz),
        "load": read_house_load(db, start, now, tz),
    }
    cells = {
        signal: [[([], []) for _hour in range(24)] for _bucket in _LEAD_BUCKETS]
        for signal in ("pv", "load")
    }
    columns = {"pv": "pv_w", "load": "house_load_w"}
    for issue, frame in snapshots:
        for signal, column in columns.items():
            if column not in frame or actual[signal].empty:
                continue
            predictions = pd.to_numeric(frame[column], errors="coerce")
            targets = predictions.index.intersection(actual[signal].index)
            for target in targets:
                if target < start or target >= now:
                    continue
                lead_h = (target - issue).total_seconds() / 3600.0
                bucket = next((i for i, (low, high, _label)
                               in enumerate(_LEAD_BUCKETS)
                               if low <= lead_h < high), None)
                if bucket is None:
                    continue
                pred, measured = predictions.loc[target], actual[signal].loc[target]
                if not np.isfinite(pred) or not np.isfinite(measured):
                    continue
                # PV-Nachtwerte liefern keine Prognoseinformation und würden
                # WAPE/Bias der Randstunden optisch überbetonen.
                if signal == "pv" and max(float(pred), float(measured)) < 100.0:
                    continue
                a_values, p_values = cells[signal][bucket][target.hour]
                a_values.append(float(measured))
                p_values.append(float(pred))

    result = {}
    for signal in ("pv", "load"):
        wape, bias, counts = [], [], []
        for bucket in cells[signal]:
            w_row, b_row, n_row = [], [], []
            for measured, predicted in bucket:
                metrics = _metrics(measured, predicted)
                w_row.append(metrics["wape_pct"] if metrics["n"] else None)
                b_row.append(metrics["bias_w"] if metrics["n"] else None)
                n_row.append(metrics["n"])
            wape.append(w_row)
            bias.append(b_row)
            counts.append(n_row)
        result[signal] = {
            "hours": [f"{hour:02d}" for hour in range(24)],
            "lead_buckets": [label for _low, _high, label in _LEAD_BUCKETS],
            "wape": wape, "bias_w": bias, "n": counts,
            "samples": int(sum(sum(row) for row in counts)),
        }
    return result


def _spread_selection(items: list, limit: int = 6) -> list:
    """Wenige, über den verfügbaren Zeitraum verteilte Vintages auswählen."""
    if len(items) <= limit:
        return items
    positions = np.linspace(0, len(items) - 1, limit)
    indices = sorted(set(int(round(pos)) for pos in positions))
    return [items[index] for index in indices]


def _forecast_vintages(config, snapshots: list, day, now) -> dict:
    """Mehrere Erstellungsstände eines Zieltags und dessen Ist-Verlauf."""
    from .local_history import read_actual, read_house_load

    tz, db = config.general.timezone, config.e3dc_rscp.history_db_path
    end = day + pd.DateOffset(days=1)
    freq = f"{int(config.general.slot_minutes)}min"
    index = pd.date_range(day, end, freq=freq, inclusive="left")
    candidates = []
    for issue, frame in snapshots:
        selected = frame[(frame.index >= day) & (frame.index < end)]
        if selected.empty:
            continue
        if not any(name in selected for name in ("pv_w", "house_load_w")):
            continue
        candidates.append((issue, selected))
    candidates = _spread_selection(candidates, 6)

    series = []
    for issue, frame in candidates:
        series.append({
            "issue": issue.isoformat(),
            "pv_w": _json_values(
                pd.to_numeric(frame.get("pv_w"), errors="coerce").reindex(index)
                if "pv_w" in frame else pd.Series(index=index, dtype="float64")),
            "load_w": _json_values(
                pd.to_numeric(frame.get("house_load_w"), errors="coerce").reindex(index)
                if "house_load_w" in frame
                else pd.Series(index=index, dtype="float64")),
        })
    actual_end = min(end, now)
    pv_actual = read_actual(db, "pv_w", day, actual_end, tz).reindex(index)
    load_actual = read_house_load(db, day, actual_end, tz).reindex(index)
    return {
        "day": day.strftime("%Y-%m-%d"),
        "timestamps": [stamp.isoformat() for stamp in index],
        "actual": {"pv_w": _json_values(pv_actual),
                   "load_w": _json_values(load_actual)},
        "series": series,
    }


def _forecast_day_comparison(config, snapshots: list, day, now) -> dict:
    """PV-Ist, beide PV-Quellen und produktives Last-Soll eines Zieltags.

    Die Quellkurven sind Rolling-Origin: je Zielslot gilt nur der jüngste
    Forecast, der zu diesem Zeitpunkt bereits erstellt war. Das Last-Soll wird
    genauso aus den archivierten Optimierereingängen zusammengesetzt.
    """
    from . import pv_eval, pvforecast
    from .local_history import read_actual

    tz, db = config.general.timezone, config.e3dc_rscp.history_db_path
    slot = int(config.general.slot_minutes)
    end = day + pd.DateOffset(days=1)
    index = pd.date_range(
        day, end, freq=f"{slot}min", inclusive="left")
    actual_end = min(end, now)
    actual = read_actual(db, "pv_w", day, actual_end, tz).reindex(index)

    solcast_ids = [
        source.resource_id
        for source in (getattr(config.solcast, "sources", None) or [])
        if getattr(source, "resource_id", None)]
    pvlib_ids = (
        pvforecast.source_ids(config)
        if getattr(config.pv_model, "arrays", None) else [])

    def source_curve(source_ids):
        if not source_ids:
            return pd.Series(index=index, dtype="float64")
        values = pv_eval.read_group_asof(
            db, source_ids, day, end, tz, slot, "pv")
        # Manche Provider archivieren 30-min-Werte. Für die reine Darstellung
        # darf genau ein Zwischen-Slot gehalten werden; größere Lücken bleiben
        # sichtbar und werden nicht kaschiert.
        limit = max(0, 30 // slot - 1)
        if limit and not values.empty:
            values = values.reindex(index).ffill(limit=limit)
        return values.reindex(index)

    load = pd.Series(index=index, dtype="float64")
    for issue, frame in sorted(snapshots, key=lambda item: item[0]):
        if "house_load_w" not in frame:
            continue
        candidate = pd.to_numeric(
            frame["house_load_w"], errors="coerce").reindex(index)
        known = (index >= issue) & candidate.notna().to_numpy()
        load.loc[known] = candidate.loc[known]

    solcast = source_curve(solcast_ids)
    pvlib = source_curve(pvlib_ids)
    return {
        "day": day.strftime("%Y-%m-%d"),
        "timestamps": [stamp.isoformat() for stamp in index],
        "pv_actual_w": _json_values(actual),
        "solcast_w": _json_values(solcast),
        "pvlib_w": _json_values(pvlib),
        "load_forecast_w": _json_values(load),
        "coverage": {
            "pv_actual": int(actual.notna().sum()),
            "solcast": int(solcast.notna().sum()),
            "pvlib": int(pvlib.notna().sum()),
            "load_forecast": int(load.notna().sum()),
        },
        "method": "rolling_origin",
    }


def _maturity_level(percent: float) -> tuple[str, str]:
    if percent >= 80.0:
        return "current", "belastbar"
    if percent >= 40.0:
        return "partial", "fortgeschritten"
    return "partial", "im Aufbau"


def _calibration_history_points(config, report: dict, profile: dict) -> list:
    """Archivstände in kompakte, direkt visualisierbare Kennzahlen wandeln."""
    from .local_history import read_calibration_history

    def number(value):
        try:
            value = float(value)
            return round(value, 4) if np.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    def point(item_report, item_profile, generated, current=False):
        pv = item_report.get("pv_forecast") or {}
        pv_metrics = pv.get("metrics") or {}
        validation = item_report.get("forecast_validation") or {}
        load = item_report.get("load_forecast") or {}
        load_metrics = load.get("metrics") or {}
        band = item_report.get("pv_band") or {}
        applied = item_report.get("empfohlene_config") or {}
        months = len(pv.get("monthly") or item_profile.get("pv_month") or {})
        pv_n = int(pv_metrics.get("n") or 0)
        band_n = int(band.get("n") or 0)
        load_n = int(load_metrics.get("n") or 0)
        folds = int(validation.get("archive_folds") or 0)
        min_folds = max(1, int(validation.get("archive_min_folds") or 6))
        p10 = applied.get("pv_model.p10_uncertainty")
        p90 = applied.get("pv_model.p90_uncertainty")
        if current:
            p10 = p10 if p10 is not None else config.pv_model.p10_uncertainty
            p90 = p90 if p90 is not None else config.pv_model.p90_uncertainty
        return {
            "generated": str(generated),
            "pv_factor": number(
                item_profile.get("pv_global", pv.get("suggested_scale"))),
            "load_factor": number(
                validation.get("global_correction",
                               load.get("suggested_correction_factor"))),
            "p10": number(p10),
            "p90": number(p90),
            "pv_samples": pv_n,
            "band_samples": band_n,
            "load_samples": load_n,
            "load_folds": folds,
            "pv_confidence_pct": round(
                100.0 * min(1.0, pv_n / 500.0, months / 3.0), 0),
            "band_confidence_pct": round(
                100.0 * min(1.0, band_n / 192.0), 0),
            "load_confidence_pct": round(
                100.0 * min(1.0, folds / min_folds), 0),
        }

    rows = read_calibration_history(
        config.e3dc_rscp.history_db_path, limit=26)
    points = [
        point(row.get("report") or {}, row.get("profile") or {},
              row.get("generated"))
        for row in rows]
    generated = report.get("generated") or profile.get("generated")
    if generated:
        current_point = point(report, profile, generated, current=True)
        # Der aktuelle YAML-Stand erscheint sofort; nach dem nächsten
        # Kalibrierungslauf kommt derselbe Stand bereits aus dem DB-Archiv.
        points = [item for item in points
                  if pd.Timestamp(item["generated"]) != pd.Timestamp(generated)]
        points.append(current_point)
    points.sort(key=lambda item: pd.Timestamp(item["generated"]))
    labels = {
        "pv_factor": "PV-Faktor", "load_factor": "Last-Faktor",
        "p10": "P10-Band", "p90": "P90-Band"}
    previous = None
    for item in points:
        changes = []
        if previous:
            for key, label in labels.items():
                old, new = previous.get(key), item.get(key)
                if old is not None and new is not None and abs(new - old) >= 0.0005:
                    changes.append({
                        "key": key, "label": label,
                        "from": old, "to": new,
                        "delta": round(new - old, 4),
                    })
        item["changes"] = changes
        previous = item
    return points[-26:]


def calibration_maturity(config, now=None) -> dict:
    """Transparente Datenreife der produktiven Prognosekalibrierungen.

    Der Prozentwert ist bewusst ein Reifeindikator aus Stichprobe und zeitlicher
    Abdeckung, keine vorgetäuschte statistische Wahrscheinlichkeit.
    """
    import os

    import yaml

    from .local_history import read_pv_source_selection

    tz = config.general.timezone
    current = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=tz)
    if current.tzinfo is None:
        current = current.tz_localize(tz)
    else:
        current = current.tz_convert(tz)
    profile_path = os.path.abspath(config.calibration.pv_profile)
    report_path = os.path.join(os.path.dirname(profile_path), "kalibrierung.yaml")

    def _yaml(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        except (OSError, ValueError, TypeError):
            return {}

    report, profile = _yaml(report_path), _yaml(profile_path)
    generated = report.get("generated") or profile.get("generated")
    pv = report.get("pv_forecast") or {}
    pv_metrics = pv.get("metrics") or {}
    months = len(pv.get("monthly") or profile.get("pv_month") or {})
    pv_n = int(pv_metrics.get("n") or 0)
    # Für eine saisonal brauchbare Korrektur zählen sowohl viele Slots als auch
    # mehrere Monate. 900 Punkte aus nur einem Sommermonat sind nicht "fertig".
    pv_conf = 100.0 * min(1.0, pv_n / 500.0, months / 3.0)
    pv_level, pv_state = _maturity_level(pv_conf)
    pv_factor = profile.get("pv_global", pv.get("suggested_scale"))

    band = report.get("pv_band") or {}
    band_n = int(band.get("n") or 0)
    band_conf = 100.0 * min(1.0, band_n / 192.0)
    band_level, band_state = _maturity_level(band_conf)

    validation = report.get("forecast_validation") or {}
    load = report.get("load_forecast") or {}
    load_metrics = load.get("metrics") or {}
    folds = int(validation.get("archive_folds") or 0)
    min_folds = max(1, int(validation.get("archive_min_folds") or 6))
    load_conf = 100.0 * min(1.0, folds / min_folds)
    load_level, load_state = _maturity_level(load_conf)
    if folds < min_folds:
        load_state = f"Hybridprofil {folds}/{min_folds} Folds"

    selection = read_pv_source_selection(
        config.e3dc_rscp.history_db_path) or {}
    selection_metrics = selection.get("metrics") or {}
    groups = selection_metrics.get("groups") or {}
    min_samples = max(1, int(selection_metrics.get(
        "min_samples", config.pv_source_selection.min_samples)))
    group_samples = [int(value.get("n") or 0) for value in groups.values()]
    sample_ratio = (min(group_samples) / min_samples) if group_samples else 0.0
    selected_since = selection.get("selected_since")
    age_days = 0.0
    if selected_since:
        try:
            since = pd.Timestamp(selected_since)
            since = (since.tz_localize("UTC") if since.tzinfo is None else since
                     ).tz_convert(tz)
            age_days = max(0.0, (current - since).total_seconds() / 86400.0)
        except (TypeError, ValueError):
            pass
    # Mindeststichprobe plus zwei Wochen unabhängige Wetterlagen.
    source_conf = 100.0 * min(1.0, sample_ratio, age_days / 14.0)
    source_level, source_state = _maturity_level(source_conf)
    selected = selection.get("selected") or "–"
    context_pct = min(
        [float(value.get("context_coverage_pct") or 0.0)
         for value in groups.values()] or [0.0])
    competition = report.get("calibration_competition") or {}
    contests = competition.get("signals") or {}
    contest_labels = {
        "pv_correction": "PV", "load_correction": "Last",
        "pv_band": "PV-Band",
    }
    promoted = [
        contest_labels.get(name, name) for name, value in contests.items()
        if value.get("promote")]
    held = [
        contest_labels.get(name, name) for name, value in contests.items()
        if value.get("status") == "held"]
    insufficient = [
        contest_labels.get(name, name) for name, value in contests.items()
        if value.get("status") == "insufficient"]
    min_comp_samples = max(
        1, int(competition.get(
            "min_samples", config.calibration.promotion_min_samples)))
    evidence = [int(value.get("n") or 0) for value in contests.values()]
    competition_conf = (
        100.0 * min(1.0, min(evidence) / min_comp_samples)
        if evidence else 0.0)
    if insufficient:
        competition_level, competition_state = "partial", "Prüfdaten im Aufbau"
    elif promoted:
        competition_level, competition_state = "current", "Challenger übernommen"
    elif contests:
        competition_level, competition_state = "current", "Champion bestätigt"
    else:
        competition_level, competition_state = "partial", "noch kein Vergleich"
    competition_days = int(competition.get(
        "promotion_days", config.calibration.promotion_days))
    competition_min_improvement = float(competition.get(
        "min_improvement_percent",
        config.calibration.promotion_min_improvement_percent))

    return {
        "generated": generated,
        "note": "Konfidenz = Datenreife aus Stichprobe und zeitlicher Abdeckung",
        "history": _calibration_history_points(config, report, profile),
        "cards": [
            {
                "key": "calibration_competition",
                "title": "Champion / Challenger",
                "level": competition_level, "state": competition_state,
                "confidence_pct": round(competition_conf, 0),
                "detail": (
                    f"{competition_days} Tage Holdout · Mindestvorsprung "
                    f"{competition_min_improvement:.1f} %"),
                "values": [
                    {"label": "Übernommen",
                     "value": ", ".join(promoted) if promoted else "keiner"},
                    {"label": "Champion gehalten",
                     "value": ", ".join(held) if held else "–"},
                    {"label": "Im Aufbau",
                     "value": ", ".join(insufficient) if insufficient else "–"},
                ],
                "reason": " · ".join(
                    f"{contest_labels.get(name, name)}: "
                    f"{value.get('reason', '–')}"
                    for name, value in contests.items()),
            },
            {
                "key": "pv_correction", "title": "PV-Korrektur",
                "level": pv_level,
                "state": ("saisonal " + pv_state if months < 3 else pv_state),
                "confidence_pct": round(pv_conf, 0),
                "detail": (f"{pv_n} Messpunkte · {months}/3 Monate · "
                           f"nMAE {pv_metrics.get('nmae_pct', 0):.1f} %"),
                "values": [
                    {"label": "Aktiver Faktor", "value": pv_factor},
                    {"label": "Korrelation", "value": pv_metrics.get("corr")},
                ],
            },
            {
                "key": "pv_band", "title": "PV P10/P90-Band",
                "level": band_level, "state": band_state,
                "confidence_pct": round(band_conf, 0),
                "detail": (f"{band_n}/192 Residuen · unter P10 "
                           f"{band.get('current_below_p10_pct', 0):.1f} % · "
                           f"über P90 {band.get('current_above_p90_pct', 0):.1f} %"),
                "values": [
                    {"label": "P10 aktiv → Ziel",
                     "value": [config.pv_model.p10_uncertainty,
                               band.get("recommended_p10_uncertainty")]},
                    {"label": "P90 aktiv → Ziel",
                     "value": [config.pv_model.p90_uncertainty,
                               band.get("recommended_p90_uncertainty")]},
                ],
            },
            {
                "key": "load_correction", "title": "Lastkorrektur",
                "level": load_level, "state": load_state,
                "confidence_pct": round(load_conf, 0),
                "detail": (f"{int(load_metrics.get('n') or 0)} Messpunkte · "
                           f"Archivanteil {100 * float(validation.get('archive_weight') or 0):.0f} % · "
                           f"WAPE {load_metrics.get('nmae_pct', 0):.1f} %"),
                "values": [
                    {"label": "Produktiver Faktor",
                     "value": validation.get("global_correction")},
                    {"label": "Methode",
                     "value": validation.get("empfehlung") or config.forecast.method},
                ],
            },
            {
                "key": "source_selection", "title": "PV-Quellenwahl",
                "level": source_level, "state": source_state,
                "confidence_pct": round(source_conf, 0),
                "detail": (f"{min(group_samples) if group_samples else 0}/"
                           f"{min_samples} Paare · {age_days:.1f}/14 Tage · "
                           f"Kontext {context_pct:.0f} %"),
                "values": [
                    {"label": "Aktiv", "value": selected},
                    {"label": "Entscheidungsscore",
                     "value": {name: value.get("decision_score_pct")
                               for name, value in groups.items()}},
                    {"label": "WAPE",
                     "value": {name: value.get("wape_pct")
                               for name, value in groups.items()}},
                ],
                "reason": selection.get("reason"),
            },
        ],
    }


def forecast_analysis(config, days: int = 30, target_day=None, now=None) -> dict:
    """Heatmaps und Forecast-Vintages aus echten Produktions-Snapshots.

    Rein lesend und robust bei leerer/junger Historie. ``target_day`` ist ein
    lokales Kalenderdatum; standardmäßig wird der heutige Tag dargestellt.
    """
    from .local_history import read_optimizer_forecast_snapshots

    tz = config.general.timezone
    current = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=tz)
    if current.tzinfo is None:
        current = current.tz_localize(tz)
    else:
        current = current.tz_convert(tz)
    try:
        day = pd.Timestamp(target_day) if target_day else current.normalize()
        day = day.tz_localize(tz) if day.tzinfo is None else day.tz_convert(tz)
        day = day.normalize()
    except (TypeError, ValueError):
        day = current.normalize()
    # Das UI darf auch morgen betrachten, aber keine beliebig großen/teuren
    # Archivabfragen auslösen.
    earliest = current.normalize() - pd.Timedelta(days=90)
    latest = current.normalize() + pd.DateOffset(days=1)
    day = min(max(day, earliest), latest)

    heat_start = current - pd.Timedelta(days=max(1, min(int(days), 90)))
    read_start = min(heat_start - pd.Timedelta(hours=48),
                     day - pd.Timedelta(hours=48))
    read_end = current + pd.Timedelta(minutes=config.general.slot_minutes)
    try:
        snapshots = read_optimizer_forecast_snapshots(
            config.e3dc_rscp.history_db_path, read_start, read_end, tz,
            stride_hours=6)
    except Exception:
        snapshots = []
    heat_snapshots = [(issue, frame) for issue, frame in snapshots
                      if issue >= heat_start - pd.Timedelta(hours=48)]
    vintage_snapshots = [(issue, frame) for issue, frame in snapshots
                         if issue >= day - pd.Timedelta(hours=48)
                         and issue < day + pd.DateOffset(days=1)]
    return {
        "generated": current.isoformat(),
        "lookback_days": max(1, min(int(days), 90)),
        "calibration": calibration_maturity(config, current),
        "day_comparison": _forecast_day_comparison(
            config, vintage_snapshots, day, current),
        "heatmaps": _forecast_error_heatmaps(
            config, heat_snapshots, heat_start, current),
        "vintages": _forecast_vintages(
            config, vintage_snapshots, day, current),
        "available_from": (snapshots[0][0].strftime("%Y-%m-%d")
                           if snapshots else None),
        "available_to": latest.strftime("%Y-%m-%d"),
    }


def savings_over_time(db: str, start_day: Optional[str] = None) -> dict:
    """Validierte Tagesersparnisse zu Tages-/Wochen-/Monatsreihen aggregieren.

    Quelle: Tabelle savings_validated (read_savings_validated). Liefert Summen
    je Periode plus die Gesamtersparnis - Grundlage des Ersparnis-Charts."""
    from .local_history import read_savings_validated
    df = read_savings_validated(db, start_day)
    empty = {"days": 0, "total_saved_eur": 0.0,
             "daily": [], "weekly": [], "monthly": []}
    if df is None or df.empty:
        return empty
    df = df.copy()
    df["date"] = pd.to_datetime(df["day"], errors="coerce")
    df = df[df["date"].notna()]
    if df.empty:
        return empty
    num = ["saved_eur", "import_kwh", "export_kwh",
           "meter_cost_eur", "baseline_cost_eur"]
    for col in num:
        df[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(0.0)

    def _agg(group) -> dict:
        return {
            "saved_eur": round(float(group["saved_eur"].sum()), 2),
            "import_kwh": round(float(group["import_kwh"].sum()), 1),
            "export_kwh": round(float(group["export_kwh"].sum()), 1),
            "meter_cost_eur": round(float(group["meter_cost_eur"].sum()), 2),
            "baseline_cost_eur": round(float(group["baseline_cost_eur"].sum()), 2),
            "days": int(len(group)),
        }

    iso = df["date"].dt.isocalendar()
    weekly = [{"period": f"{int(y)}-KW{int(w):02d}", **_agg(g)}
              for (y, w), g in df.groupby([iso["year"], iso["week"]], sort=True)]
    monthly = [{"period": str(p), **_agg(g)}
               for p, g in df.groupby(df["date"].dt.strftime("%Y-%m"), sort=True)]
    daily = [{"period": r["day"], "saved_eur": round(float(r["saved_eur"]), 2),
              "balance_ok": bool(r.get("balance_ok"))}
             for _, r in df.iterrows()]
    return {"days": int(len(df)),
            "total_saved_eur": round(float(df["saved_eur"].sum()), 2),
            "daily": daily, "weekly": weekly, "monthly": monthly}


def savings_drivers(config, days: int = 30) -> dict:
    """Treiber des Nutzens aus den Ist-Werten (kein exakter €-Split, sondern die
    physikalischen Hebel): Eigenverbrauchsquote, Autarkiegrad und die – idealer-
    weise vermiedene – Einspeisung bei negativem Börsenpreis. Rein lesend über
    actuals (+ spot_price für den Negativpreis-Anteil)."""
    from .local_history import read_actual, read_spot
    db = config.e3dc_rscp.history_db_path
    tz = config.general.timezone
    dt = config.general.dt_hours
    now = pd.Timestamp.now(tz=tz)
    start = now - pd.Timedelta(days=int(days))
    pv = read_actual(db, "pv_w", start, now, tz).dropna()
    grid = read_actual(db, "grid_w", start, now, tz).dropna()   # +Bezug / -Einspeisung
    house = read_actual(db, "house_w", start, now, tz).dropna()
    out = {"days": int(days), "n": int(len(grid)), "pv_kwh": None,
           "self_consumed_kwh": None, "self_consumption_pct": None,
           "autarky_pct": None, "import_kwh": None, "export_kwh": None,
           "negative_price_export_kwh": None}
    if grid.empty:
        return out
    imp = grid.clip(lower=0.0)
    exp = (-grid).clip(lower=0.0)
    to_kwh = lambda s: float(s.sum()) * dt / 1000.0
    import_kwh, export_kwh = to_kwh(imp), to_kwh(exp)
    out["import_kwh"] = round(import_kwh, 1)
    out["export_kwh"] = round(export_kwh, 1)
    if not pv.empty:
        pv_kwh = to_kwh(pv.clip(lower=0.0))
        self_c = max(0.0, pv_kwh - export_kwh)          # lokal genutzte PV
        out["pv_kwh"] = round(pv_kwh, 1)
        out["self_consumed_kwh"] = round(self_c, 1)
        out["self_consumption_pct"] = (round(100.0 * self_c / pv_kwh, 1)
                                       if pv_kwh > 0.1 else None)
    if not house.empty:
        load_kwh = to_kwh(house.clip(lower=0.0))
        if load_kwh > 0.1:
            out["autarky_pct"] = round(
                100.0 * max(0.0, load_kwh - import_kwh) / load_kwh, 1)
    try:
        spot = read_spot(db, start, now, tz, config.general.slot_minutes)
        neg = spot.reindex(exp.index).astype(float) < 0.0
        if neg.any():
            out["negative_price_export_kwh"] = round(
                float(exp[neg.fillna(False)].sum()) * dt / 1000.0, 2)
    except Exception:
        pass
    return out


def battery_health(config, days: int = 30) -> dict:
    """Akku-Gesundheit aus den Ist-Werten: Ladeenergie-Durchsatz und äquivalente
    Vollzyklen sowie Verweildauer bei ~100 %/~min-SoC (langes Vollstehen altert
    die Zellen). Rein lesend über die actuals-Tabelle."""
    from .local_history import read_actual
    db = config.e3dc_rscp.history_db_path
    tz = config.general.timezone
    hb = config.house_battery
    cap = float(hb.capacity_wh)
    dt = config.general.slot_minutes / 60.0
    now = pd.Timestamp.now(tz=tz)
    start = now - pd.Timedelta(days=int(days))
    soc = read_actual(db, "soc", start, now, tz).dropna()
    batt = read_actual(db, "battery_w", start, now, tz).dropna()
    out = {"days": int(days), "n": int(len(soc)),
           "throughput_kwh": None, "cycles_equiv": None,
           "full_hours": None, "empty_hours": None,
           "time_full_pct": None, "time_empty_pct": None,
           "soc_min_pct": None, "soc_avg_pct": None, "soc_max_pct": None}
    if not batt.empty and cap > 0:
        charge_wh = float(batt.clip(lower=0.0).sum()) * dt   # nur Laden zählt
        out["throughput_kwh"] = round(charge_wh / 1000.0, 1)
        out["cycles_equiv"] = round(charge_wh / cap, 1)
    if not soc.empty:
        full_mask = soc >= 99.0
        empty_mask = soc <= float(hb.min_soc_percent) + 1.0
        out.update(
            full_hours=round(float(full_mask.sum()) * dt, 1),
            empty_hours=round(float(empty_mask.sum()) * dt, 1),
            time_full_pct=round(100.0 * float(full_mask.mean()), 1),
            time_empty_pct=round(100.0 * float(empty_mask.mean()), 1),
            soc_min_pct=round(float(soc.min()), 1),
            soc_avg_pct=round(float(soc.mean()), 1),
            soc_max_pct=round(float(soc.max()), 1))
    return out


def _load_accuracy(config, start, now) -> dict:
    """Rollierende Last-Prognosegüte: je Tag den vor Tagesbeginn gültigen
    Optimierer-Snapshot gegen die real gemessene Hauslast stellen."""
    from .local_history import read_house_load, read_optimizer_forecast_asof
    db = config.e3dc_rscp.history_db_path
    tz = config.general.timezone
    a_vals, p_vals = [], []
    day = pd.Timestamp(start).tz_convert(tz).normalize()
    end = pd.Timestamp(now).tz_convert(tz)
    while day < end:
        nxt = min(day + pd.Timedelta(days=1), end)
        try:
            _issue, frame = read_optimizer_forecast_asof(db, day, day, nxt, tz)
        except Exception:
            frame = None
        if frame is not None and not frame.empty and "house_load_w" in frame:
            actual = read_house_load(db, day, nxt, tz)
            fc = pd.to_numeric(frame["house_load_w"], errors="coerce")
            common = actual.index.intersection(fc.index)
            for ts in common:
                a, p = float(actual.loc[ts]), float(fc.loc[ts])
                if np.isfinite(a) and np.isfinite(p):
                    a_vals.append(a)
                    p_vals.append(p)
        day = day + pd.Timedelta(days=1)
    return _metrics(a_vals, p_vals)


def _pv_nowcast_accuracy(config, start, now) -> dict:
    """Produktiven PV-Nahbereich gegen denselben Forecast ohne Nowcast messen.

    Bewertet werden ausschließlich Slots, für die der Nowcast laut Konfiguration
    überhaupt wirken durfte. Die Prognosepaare stammen aus demselben
    Optimierer-Snapshot; Quelle, Kalibrierung und Datenlücken sind damit identisch
    und nur die operative Intraday-Korrektur unterscheidet Champion/Challenger.
    """
    from .local_history import (read_actual,
                                read_optimizer_forecast_snapshots)

    db, tz = config.e3dc_rscp.history_db_path, config.general.timezone
    slot_minutes = int(config.general.slot_minutes)
    max_slots = int(config.forecast.intraday_pv_operational_slots)
    snapshots = read_optimizer_forecast_snapshots(
        db, start - pd.Timedelta(hours=1), now, tz, stride_hours=1)
    actual = read_actual(db, "pv_w", start, now, tz)
    measured, productive, challenger = [], [], []
    horizon = pd.Timedelta(minutes=slot_minutes * max_slots)
    for issue, frame in snapshots:
        if "pv_w" not in frame or "pv_without_nowcast_w" not in frame:
            continue
        prod = pd.to_numeric(frame["pv_w"], errors="coerce")
        base = pd.to_numeric(
            frame["pv_without_nowcast_w"], errors="coerce")
        # Der Snapshot wird erst einige Sekunden nach dem auf 15 Minuten
        # gerundeten Optimierungs-Origin geschrieben. Für die Slotzuordnung
        # zählt derselbe gerundete Origin wie beim produktiven Faktor.
        origin = issue.floor(f"{slot_minutes}min")
        targets = prod.index.intersection(base.index).intersection(actual.index)
        targets = targets[(targets >= origin) & (targets < origin + horizon)
                          & (targets >= start) & (targets < now)]
        for target in targets:
            a, p, b = actual.loc[target], prod.loc[target], base.loc[target]
            if not all(np.isfinite(value) for value in (a, p, b)):
                continue
            # Nacht/Dämmerung ohne relevante PV-Energie trägt keine Information
            # über den Nutzen einer Wolkenkorrektur.
            if max(float(a), float(p), float(b)) < 100.0:
                continue
            measured.append(float(a))
            productive.append(float(p))
            challenger.append(float(b))
    prod_metrics = _metrics(measured, productive)
    base_metrics = _metrics(measured, challenger)
    delta_wape = (
        round(float(base_metrics["wape_pct"] - prod_metrics["wape_pct"]), 2)
        if prod_metrics["wape_pct"] is not None
        and base_metrics["wape_pct"] is not None else None)
    delta_mae = (
        round(float(base_metrics["mae_w"] - prod_metrics["mae_w"]), 1)
        if prod_metrics["mae_w"] is not None
        and base_metrics["mae_w"] is not None else None)
    winner = ("nowcast" if delta_wape is not None and delta_wape > 0.05
              else "ohne_nowcast"
              if delta_wape is not None and delta_wape < -0.05
              else "gleichauf")
    result = {
        "n": prod_metrics["n"], "operational_slots": max_slots,
        "productive": prod_metrics, "without_nowcast": base_metrics,
        "improvement_wape_pp": delta_wape,
        "improvement_mae_w": delta_mae, "winner": winner,
    }
    # Keine AUSSAGE ohne Stichprobe: der Nowcast wirkt nur auf wenige Slots je
    # Origin, anfangs steht hinter der Kennzahl ein einzelner Messpunkt. Die
    # Arithmetik bleibt sichtbar - aber ein Sieger wird erst ausgerufen, wenn
    # genug Paare vorliegen. Sonst behauptet die Kachel eine Verbesserung, die
    # aus einem Messpunkt stammt.
    result["sufficient"] = enough("nowcast", prod_metrics["n"])
    if not result["sufficient"]:
        result["winner"] = "unklar"
        result["reason"] = shortfall_note("nowcast", prod_metrics["n"])
    return result


def forecast_accuracy(config, days: int = 7) -> dict:
    """Prognosegüte (WAPE/Bias/MAE) für PV und Hauslast über die letzten `days`.

    PV nutzt die rollierende Bewertung aus pv_eval (gegen die realen Ertrags-
    daten, aktive Quelle); die Last kommt aus den Optimierer-Snapshots gegen die
    gemessene Hauslast. Robuste, leere Metriken (n=0), wenn noch keine Historie
    da ist."""
    now = pd.Timestamp.now(tz=config.general.timezone)
    start = now - pd.Timedelta(days=int(days))
    pv = {"n": 0, "wape_pct": None, "bias_w": None, "mae_w": None, "source": None}
    try:
        from .pv_eval import compare_sources
        cmp = compare_sources(config, lookback_days=int(days), now=now)
        groups = cmp.get("groups", {})
        active = (getattr(config, "_pv_selected_source", None)
                  or (cmp.get("recommendation") or {}).get("better"))
        chosen = groups.get(active) if active in groups else None
        if chosen is None and groups:
            active, chosen = next(iter(groups.items()))
        if chosen:
            pv = {"n": int(chosen.get("n", 0)),
                  "wape_pct": chosen.get("wape_pct"),
                  "bias_w": chosen.get("bias_w"),
                  "mae_w": chosen.get("mae_w"),
                  "source": active}
    except Exception:
        pass
    try:
        pv_nowcast = _pv_nowcast_accuracy(config, start, now)
    except Exception:
        pv_nowcast = {
            "n": 0, "operational_slots":
                int(config.forecast.intraday_pv_operational_slots),
            "productive": _metrics([], []),
            "without_nowcast": _metrics([], []),
            "improvement_wape_pp": None,
            "improvement_mae_w": None, "winner": "gleichauf",
        }
    return {"days": int(days), "pv": pv,
            "load": _load_accuracy(config, start, now),
            "pv_nowcast": pv_nowcast}
