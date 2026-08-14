"""PV-Prognosequellen quantitativ auswerten: pvlib vs. Solcast gegen die
realen Ertragsdaten (actuals.pv_w).

Zweck (Verbesserungsvorschlag #4): eine ehrliche Entscheidungsgrundlage, ob und
wann von Solcast auf das freie pvlib-Modell (oder umgekehrt) umgeschaltet werden
soll - und die Kalibrierung des pvlib-p10/p90-Bandes aus ECHTEN Residuen statt
des heuristischen Festwerts.

Fair verglichen wird ROLLING-ORIGIN aus dem unveränderlichen Snapshot-Archiv
(``pv_forecast_archive``): je Zielslot der jüngste VOR dem Slot ausgegebene
Forecast jeder Quelle - genau der Wert, den der Optimierer damals sah. Beide
Quellen archivieren so (Solcast: ems/solcast.py, pvlib: ems/pvforecast.py).
Fehlt noch Archivhistorie (pvlib archiviert erst seit diesem Feature), greift
optional der Live-Cache (``pv_forecast``) als Übergangslösung - klar als solche
gekennzeichnet, da dessen Vergangenheitswerte bei pvlib mit (nahezu) realem
Wetter nachgerechnet werden und die Prognosegüte zu optimistisch zeigen.

Alle Quellen einer Gruppe (Solcast-Resourcen bzw. pvlib-Arrays) werden je Slot
summiert und gegen die Gesamt-Ist-PV der Anlage gemessen.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

import numpy as np
import pandas as pd

from .quality import bias_w

from . import pvforecast

log = logging.getLogger("ems.pv_eval")

_COL = {"pv": "pv_w", "p10": "pv10_w", "p90": "pv90_w"}


def _wape(actual: np.ndarray, pred: np.ndarray) -> float:
    """WAPE (%): Σ|Fehler| / Σ|Ist| - robust gegen Werte nahe 0."""
    denom = float(np.sum(np.abs(actual)))
    if denom <= 1e-9:
        return float("nan")
    return 100.0 * float(np.sum(np.abs(pred - actual))) / denom


def _mae(actual: np.ndarray, pred: np.ndarray) -> float:
    return float(np.mean(np.abs(pred - actual))) if len(actual) else float("nan")


def _weighted_wape(actual: np.ndarray, pred: np.ndarray,
                   weights: np.ndarray) -> float:
    denom = float(np.sum(weights * np.abs(actual)))
    if denom <= 1e-9:
        return float("nan")
    return 100.0 * float(np.sum(weights * np.abs(pred - actual))) / denom


def _decision_weights(frame: pd.DataFrame, config) -> tuple[np.ndarray, dict]:
    """Gemeinsame Slotgewichte für beide PV-Quellen.

    Basis 1 bleibt die normale Energie-WAPE. Zusatzgewicht entsteht dort, wo
    ein PV-Fehler wahrscheinlich die EMS-Entscheidung ändert: am Übergang
    Netzbezug/Überschuss, bei Preisextremen/Negativpreisen und nahe der
    möglichen Einspeisegrenze.
    """
    n = len(frame)
    weights = np.ones(n, dtype=float)
    house = pd.to_numeric(frame.get("house"), errors="coerce").to_numpy(
        dtype=float) if "house" in frame else np.full(n, np.nan)
    price = pd.to_numeric(frame.get("spot"), errors="coerce").to_numpy(
        dtype=float) if "spot" in frame else np.full(n, np.nan)
    actual = frame["actual"].to_numpy(dtype=float)
    house_ok = np.isfinite(house)
    price_ok = np.isfinite(price)
    context_pct = 100.0 * float(house_ok.mean()) if n else 0.0
    price_pct = 100.0 * float(price_ok.mean()) if n else 0.0
    cfg = config.pv_source_selection
    enabled = bool(getattr(cfg, "decision_weighted", True))
    min_context = float(getattr(cfg, "decision_min_context_percent", 50.0))
    if not enabled or context_pct < min_context:
        return weights, {
            "mode": "energy_wape", "context_coverage_pct": round(context_pct, 1),
            "price_coverage_pct": round(price_pct, 1),
            "decision_slots": 0, "mean_weight": 1.0}

    # Netz/PV-Kippunkt: bei Ist-PV ungefähr auf Hauslast kann ein kleiner Fehler
    # aus Netzbezug Überschuss machen (oder umgekehrt). Skala robust aus der
    # typischen Hauslast, mindestens 500 W.
    typical_load = float(np.nanmedian(house[house_ok])) if house_ok.any() else 500.0
    boundary_scale = max(500.0, typical_load)
    surplus = actual - house
    boundary = np.zeros(n, dtype=float)
    boundary[house_ok] = np.exp(
        -np.abs(surplus[house_ok]) / boundary_scale)
    weights += 1.5 * boundary
    # Positiver Überschuss beeinflusst Ladezeitpunkt/SoC auch abseits des
    # exakten Kippunkts.
    weights += 0.6 * (house_ok & (surplus > 0.0))

    # Nahe der physikalischen Einspeisegrenze entscheidet PV-Fehler über Peak-
    # Laden bzw. Abregelung. Ohne explizite Grenze ist WR-max-AC die Obergrenze.
    export_limit = (getattr(config.inverter, "max_export_w", None)
                    or getattr(config.inverter, "max_ac_power_w", None))
    if export_limit and float(export_limit) > 0:
        export_ratio = np.clip(
            np.where(house_ok, np.maximum(surplus, 0.0) / float(export_limit), 0.0),
            0.0, 1.5)
        weights += 1.5 * np.clip((export_ratio - 0.6) / 0.4, 0.0, 1.0)

    # Preisextreme erhöhen den wirtschaftlichen Wert eines korrekten PV-Slots.
    if price_ok.any():
        p = price[price_ok]
        median = float(np.nanmedian(p))
        q10, q90 = np.nanquantile(p, [0.10, 0.90])
        spread = max(1.0, float(q90 - q10))
        importance = np.zeros(n, dtype=float)
        importance[price_ok] = np.clip(
            np.abs(price[price_ok] - median) / spread, 0.0, 2.0)
        weights += 0.75 * importance
        # Negativpreise sind besonders relevant, auch wenn §51 EEG deaktiviert
        # ist: Laden, Einspeisen und ggf. Abregeln konkurrieren hier direkt.
        weights += 2.0 * (price_ok & (price < 0.0))

    return weights, {
        "mode": "decision_weighted",
        "context_coverage_pct": round(context_pct, 1),
        "price_coverage_pct": round(price_pct, 1),
        "decision_slots": int(np.count_nonzero(weights >= 2.0)),
        "mean_weight": round(float(np.mean(weights)), 2),
    }


def solcast_source_ids(config) -> List[str]:
    sc = config.solcast
    if not (getattr(sc, "enabled", False) and sc.sources):
        return []
    return [s.resource_id for s in sc.sources]


def source_groups(config) -> dict:
    """Verfuegbare PV-Quellgruppen als {Name: [Archiv-IDs]}.

    Jede Gruppe braucht ein EIGENES Korrekturprofil - siehe
    ``calibration.pv_profile_for_source``."""
    from . import pvforecast
    gruppen = {}
    sc = solcast_source_ids(config)
    if sc:
        gruppen["solcast"] = sc
    if config.pv_model.arrays:
        pv = pvforecast.source_ids(config)
        if pv:
            gruppen["pvlib"] = pv
    return gruppen


def active_source(config) -> Optional[str]:
    """Name der Gruppe, die den Produktivbetrieb speist (Solcast vor pvlib)."""
    gruppen = source_groups(config)
    for name in ("solcast", "pvlib"):
        if name in gruppen:
            return name
    return None


def _grid(start, end, tz, slot_minutes) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start).tz_convert(tz),
                         pd.Timestamp(end).tz_convert(tz),
                         freq=f"{slot_minutes}min", inclusive="left")


def read_actual_pv(db: str, start, end, tz: str, slot_minutes: int) -> pd.Series:
    """Ist-PV (W) je Slot aus actuals.pv_w (Mittel je 15-min-Fenster)."""
    grid = _grid(start, end, tz, slot_minutes)
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    try:
        con = sqlite3.connect(db, timeout=10)
        rows = con.execute(
            "SELECT ts, pv_w FROM actuals "
            "WHERE ts >= ? AND ts < ? AND pv_w IS NOT NULL ORDER BY ts",
            (s_utc, e_utc)).fetchall()
        con.close()
    except Exception:
        rows = []
    if not rows:
        return pd.Series(index=grid, dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    s = pd.Series([float(r[1]) for r in rows], index=idx).tz_convert(tz)
    return s.resample(f"{slot_minutes}min").mean().reindex(grid)


def read_group_asof(db: str, sources: List[str], start, end, tz: str,
                    slot_minutes: int, which: str = "pv",
                    lead_hours: float = 0.0) -> pd.Series:
    """Rolling-Origin-Summe einer Quellgruppe: je Zielslot der jüngste
    Forecast mit issue_ts <= target_ts - lead_hours, über die Quellen summiert."""
    if not sources:
        return pd.Series(dtype="float64")
    col = _COL[which]
    grid = _grid(start, end, tz, slot_minutes)
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    placeholders = ",".join("?" * len(sources))
    # Maximale issue_ts je (source, target) unter der Lead-Schranke, pro Zeile
    # aus target_ts gerechnet. strftime statt datetime, weil die Schranke mit
    # issue_ts als STRING verglichen wird: datetime() liefert
    # "2026-05-24 10:00:00" (Leerzeichen, ohne Offset), issue_ts steht als
    # "2026-05-24T10:00:00+00:00" da. Beim Vergleich gewinnt dann das "T"
    # gegen das Leerzeichen, und jede Ausgabe mit GLEICHEM Datum fiel heraus -
    # die wirksame Vorlaufzeit war bis zu einen Tag groesser als angefordert.
    lead_sql = (
        f"strftime('%Y-%m-%dT%H:%M:%S+00:00', target_ts, '-{lead_hours} hours')"
        if lead_hours else "target_ts")
    try:
        con = sqlite3.connect(db, timeout=10)
        rows = con.execute(
            f"WITH latest AS ("
            f" SELECT source, target_ts, max(issue_ts) issue_ts"
            f" FROM pv_forecast_archive"
            f" WHERE source IN ({placeholders})"
            f" AND target_ts >= ? AND target_ts < ? AND issue_ts <= {lead_sql}"
            f" GROUP BY source, target_ts)"
            f" SELECT a.target_ts, sum(a.{col})"
            f" FROM pv_forecast_archive a JOIN latest l"
            f" ON a.source=l.source AND a.target_ts=l.target_ts"
            f" AND a.issue_ts=l.issue_ts"
            f" WHERE a.{col} IS NOT NULL GROUP BY a.target_ts ORDER BY a.target_ts",
            (*sources, s_utc, e_utc)).fetchall()
        con.close()
    except Exception as exc:
        log.debug("read_group_asof fehlgeschlagen: %s", exc)
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    return pd.Series([float(r[1]) for r in rows], index=idx).tz_convert(tz).reindex(grid)


def read_group_cache(db: str, sources: List[str], start, end, tz: str,
                     slot_minutes: int, which: str = "pv") -> pd.Series:
    """Übergangs-Fallback: Quellgruppe aus dem Live-Cache (pv_forecast),
    je Zielslot über die Quellen summiert. Kein echtes Rolling-Origin."""
    if not sources:
        return pd.Series(dtype="float64")
    col = _COL[which]
    grid = _grid(start, end, tz, slot_minutes)
    s_utc = pd.Timestamp(start).tz_convert("UTC").isoformat()
    e_utc = pd.Timestamp(end).tz_convert("UTC").isoformat()
    placeholders = ",".join("?" * len(sources))
    try:
        con = sqlite3.connect(db, timeout=10)
        rows = con.execute(
            f"SELECT ts, sum({col}) FROM pv_forecast"
            f" WHERE source IN ({placeholders}) AND ts >= ? AND ts < ?"
            f" AND {col} IS NOT NULL GROUP BY ts ORDER BY ts",
            (*sources, s_utc, e_utc)).fetchall()
        con.close()
    except Exception as exc:
        log.debug("read_group_cache fehlgeschlagen: %s", exc)
        rows = []
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True, format="ISO8601")
    return pd.Series([float(r[1]) for r in rows], index=idx).tz_convert(tz).reindex(grid)


def _pairs(forecast: pd.Series, actual: pd.Series, min_pv_w: float) -> pd.DataFrame:
    """Gepaarte Erzeugungs-Slots (Prognose + Ist), Nachtslots ausgeblendet."""
    df = pd.DataFrame({"pred": forecast, "actual": actual}).dropna()
    if df.empty:
        return df
    gen = df[(df[["pred", "actual"]].max(axis=1) >= min_pv_w)]
    return gen


def evaluate_group(db, sources, start, end, tz, slot_minutes,
                   min_pv_w=50.0, lead_hours=0.0, use_archive=True,
                   allow_cache=True, correction_profile=None) -> Optional[dict]:
    """WAPE/MAE/Bias einer Quellgruppe gegen die Ist-PV. Bevorzugt das Archiv;
    fällt bei fehlender Archivhistorie auf den Live-Cache zurück."""
    if not sources:
        return None
    actual = read_actual_pv(db, start, end, tz, slot_minutes)
    method = "archive"
    fc = read_group_asof(db, sources, start, end, tz, slot_minutes, "pv",
                         lead_hours) if use_archive else pd.Series(dtype="float64")
    if correction_profile is not None and not fc.empty:
        from .calibration import apply_pv_correction
        fc = apply_pv_correction(fc, correction_profile, tz)
    pairs = _pairs(fc, actual, min_pv_w)
    if len(pairs) < 8 and allow_cache:
        fc_cache = read_group_cache(db, sources, start, end, tz, slot_minutes, "pv")
        if correction_profile is not None and not fc_cache.empty:
            from .calibration import apply_pv_correction
            fc_cache = apply_pv_correction(fc_cache, correction_profile, tz)
        cache_pairs = _pairs(fc_cache, actual, min_pv_w)
        if len(cache_pairs) > len(pairs):
            method, fc, pairs = "cache", fc_cache, cache_pairs
    if len(pairs) < 8:
        return {"sources": sources, "method": method, "n": int(len(pairs)),
                "wape_pct": float("nan"), "mae_w": float("nan"),
                "bias_w": float("nan")}
    a = pairs["actual"].to_numpy()
    p = pairs["pred"].to_numpy()
    return {"sources": sources, "method": method, "n": int(len(pairs)),
            "wape_pct": round(_wape(a, p), 2), "mae_w": round(_mae(a, p), 1),
            "bias_w": round(bias_w(a, p), 1),
            "actual_kwh": round(float(a.sum()) * slot_minutes / 60.0 / 1000.0, 1)}


def compare_sources(config, lookback_days=30, now=None, min_pv_w=50.0,
                    lead_hours=0.0, allow_cache=True) -> dict:
    """pvlib- und Solcast-Gruppe gegen die Ist-PV auswerten + Empfehlung."""
    tz = config.general.timezone
    slot = config.general.slot_minutes
    db = config.e3dc_rscp.history_db_path
    now = pd.Timestamp(now).tz_convert(tz) if now is not None \
        else pd.Timestamp.now(tz=tz)
    end = now.floor(f"{slot}min")
    start = end - pd.Timedelta(days=lookback_days)

    groups = {}
    correction = None
    if config.calibration.enabled:
        try:
            from .calibration import load_profile
            correction = load_profile(config.calibration.pv_profile)
        except Exception:
            correction = None
    # Jede Gruppe mit IHREM eigenen Profil bewerten. Fremde Profile sind
    # schlechter als gar keins, und "korrigiert gegen roh" waere kein fairer
    # Vergleich - genau daran haengt die Quellen-Empfehlung.
    from .calibration import pv_profile_for_source
    aktiv = active_source(config)
    for name, ids in source_groups(config).items():
        groups[name] = evaluate_group(
            db, ids, start, end, tz, slot, min_pv_w, lead_hours,
            allow_cache=allow_cache,
            correction_profile=pv_profile_for_source(correction, name, aktiv))
    groups = {k: v for k, v in groups.items() if v is not None}
    # Für die Empfehlung und Anzeige beide Quellen auf exakt denselben Slots
    # samt EMS-Kontext bewerten. Nur wenn kein gemeinsamer Archivvergleich
    # möglich ist, bleiben die Einzelmetriken als Fallback stehen.
    common = _common_archive_metrics(config, lookback_days, now, min_pv_w)
    for name, metrics in common.items():
        if name in groups:
            groups[name].update(metrics)

    valid = {k: v for k, v in groups.items()
             if v["n"] >= 8 and not np.isnan(v["wape_pct"])}
    recommendation = None
    if len(valid) == 2:
        score_key = ("decision_score_pct"
                     if all(np.isfinite(valid[name].get(
                         "decision_score_pct", np.nan)) for name in valid)
                     else "wape_pct")
        best = min(valid, key=lambda k: valid[k][score_key])
        other = "pvlib" if best == "solcast" else "solcast"
        delta = valid[other][score_key] - valid[best][score_key]
        wape_delta = valid[other]["wape_pct"] - valid[best]["wape_pct"]
        recommendation = {"better": best,
                          "wape_delta_pct": round(wape_delta, 2),
                          "score_delta_pct": round(delta, 2),
                          "metric": score_key,
                          "meaningful": bool(delta >= 1.0)}
    return {"start": start, "end": end, "lookback_days": lookback_days,
            "lead_hours": lead_hours, "groups": groups,
            "recommendation": recommendation}


def _common_archive_metrics(config, lookback_days: int, now,
                            min_pv_w: float = 50.0) -> dict:
    """Beide Quellen auf exakt denselben Archiv-/Ist-Slots bewerten."""
    tz, slot = config.general.timezone, config.general.slot_minutes
    db = config.e3dc_rscp.history_db_path
    end = pd.Timestamp(now).tz_convert(tz).floor(f"{slot}min")
    start = end - pd.Timedelta(days=lookback_days)
    sc_ids = solcast_source_ids(config)
    pv_ids = pvforecast.source_ids(config) if config.pv_model.arrays else []
    if not sc_ids or not pv_ids:
        return {}
    actual = read_actual_pv(db, start, end, tz, slot)
    sc = read_group_asof(db, sc_ids, start, end, tz, slot)
    pv = read_group_asof(db, pv_ids, start, end, tz, slot)
    if config.calibration.enabled:
        # BEIDE Quellen mit ihrem eigenen Profil - sonst tritt eine korrigierte
        # gegen eine rohe Prognose an und der Vergleich misst nur, wer
        # kalibriert ist.
        try:
            from .calibration import (apply_pv_correction, load_profile,
                                      pv_profile_for_source)
            profile = load_profile(config.calibration.pv_profile)
            aktiv = active_source(config)
            p_sc = pv_profile_for_source(profile, "solcast", aktiv)
            p_pv = pv_profile_for_source(profile, "pvlib", aktiv)
            if p_sc and not sc.empty:
                sc = apply_pv_correction(sc, p_sc, tz)
            if p_pv and not pv.empty:
                pv = apply_pv_correction(pv, p_pv, tz)
        except Exception as exc:
            log.debug("PV-Korrekturprofil im Quellenvergleich nicht anwendbar: %s", exc)
    frame = pd.DataFrame({"actual": actual, "solcast": sc,
                          "pvlib": pv}).dropna()
    if not frame.empty:
        frame = frame[frame[["actual", "solcast", "pvlib"]].max(axis=1)
                      >= min_pv_w]
    if frame.empty:
        return {}
    try:
        from .local_history import read_house_load, read_spot
        frame["house"] = read_house_load(db, start, end, tz).reindex(frame.index)
        frame["spot"] = read_spot(db, start, end, tz, slot).reindex(frame.index)
    except Exception:
        frame["house"] = np.nan
        frame["spot"] = np.nan
    a = frame["actual"].to_numpy(dtype=float)
    weights, weight_info = _decision_weights(frame, config)
    actual_kwh = round(
        float(a.sum()) * slot / 60.0 / 1000.0, 1)
    out = {}
    for name in ("solcast", "pvlib"):
        pred = frame[name].to_numpy(dtype=float)
        out[name] = {"sources": sc_ids if name == "solcast" else pv_ids,
                     "method": "archive", "n": int(len(frame)),
                     "wape_pct": round(_wape(a, pred), 2),
                     "decision_score_pct": round(
                         _weighted_wape(a, pred, weights), 2),
                     "decision_mae_w": round(float(
                         np.sum(weights * np.abs(pred - a))
                         / max(1e-9, np.sum(weights))), 1),
                     "mae_w": round(_mae(a, pred), 1),
                     "bias_w": round(bias_w(a, pred), 1),
                     "actual_kwh": actual_kwh,
                     **weight_info}
    return out


def select_source(config, now=None) -> dict:
    """Produktive PV-Quelle anhand echter Rolling-Origin-Fehler auswählen.

    Cache-Auswertungen sind ausdrücklich ausgeschlossen, weil pvlib historische
    Wetterdaten nachträglich kennt und dadurch künstlich gut aussähe. Ein
    Wechsel erfolgt erst, wenn BEIDE Archive genügend gemeinsame Erfahrung
    besitzen und die Alternative den konfigurierten Mindestvorsprung erreicht.
    """
    cfg = config.pv_source_selection
    db = config.e3dc_rscp.history_db_path
    from .local_history import (read_pv_source_selection,
                                write_pv_source_selection)
    previous = read_pv_source_selection(db)
    default = "solcast" if config.solcast.enabled else "pvlib"
    selected = (previous.get("selected") if previous else default)
    available = {"solcast": bool(solcast_source_ids(config)),
                 "pvlib": bool(config.pv_model.arrays and
                               (config.pv_model.enabled or config.pv_model.shadow))}
    if not available.get(selected):
        selected = next((name for name, ok in available.items() if ok), default)
    groups = _common_archive_metrics(
        config, cfg.lookback_days,
        now or pd.Timestamp.now(tz=config.general.timezone))
    valid = {name: value for name, value in groups.items()
             if value and value.get("method") == "archive"
             and value.get("n", 0) >= cfg.min_samples
             and np.isfinite(value.get("wape_pct", np.nan))}
    decision_ready = (
        bool(getattr(cfg, "decision_weighted", True)) and len(valid) == 2
        and all(value.get("mode") == "decision_weighted"
                and np.isfinite(value.get("decision_score_pct", np.nan))
                for value in valid.values()))
    score_key = "decision_score_pct" if decision_ready else "wape_pct"
    score_label = "Entscheidungsscore" if decision_ready else "WAPE"
    reason = "feste Konfiguration"
    if not cfg.enabled:
        selected = default
    elif len(valid) < 2:
        samples = ", ".join(
            f"{name}: {value.get('n', 0)}" for name, value in groups.items())
        reason = (f"warte auf vergleichbare Archive ({samples})"
                  if groups else "warte auf Prognosearchive")
    else:
        challenger = "pvlib" if selected == "solcast" else "solcast"
        improvement = (valid[selected][score_key] -
                       valid[challenger][score_key])
        wape_detail = (
            f"; WAPE {selected} {valid[selected]['wape_pct']:.2f} %, "
            f"{challenger} {valid[challenger]['wape_pct']:.2f} %"
            if decision_ready else "")
        if improvement >= cfg.min_improvement_percent:
            selected = challenger
            reason = (f"{challenger} um {improvement:.2f} "
                      f"{score_label}-Punkte besser{wape_detail}")
        elif improvement > 0:
            reason = (
                f"{selected} bleibt aktiv; {challenger} ist nur "
                f"{improvement:.2f} {score_label}-Punkte besser "
                f"(Mindestvorsprung {cfg.min_improvement_percent:.2f})"
                f"{wape_detail}")
        elif improvement < 0:
            reason = (
                f"{selected} bleibt aktiv und ist "
                f"{abs(improvement):.2f} {score_label}-Punkte besser "
                f"als {challenger}{wape_detail}")
        else:
            reason = (f"{selected} bleibt aktiv; beide Quellen sind im "
                      f"{score_label} gleichauf{wape_detail}")
    record = write_pv_source_selection(
        db, now or pd.Timestamp.now(tz="UTC"), selected, reason,
        {"groups": groups, "lookback_days": cfg.lookback_days,
         "min_samples": cfg.min_samples, "selection_metric": score_key})
    record["groups"] = groups
    return record


def calibrate_band(config, lookback_days=60, now=None, min_pv_w=100.0,
                   low=0.10, high=0.90, lead_hours=0.0,
                   promotion_days: int = 0) -> Optional[dict]:
    """pvlib-p10/p90-Band aus echten Residuen kalibrieren.

    Aus den gepaarten (Punktprognose, Ist)-Erzeugungsslots das empirische
    Verhältnis r = Ist / Prognose bilden; das untere Quantil liefert
    p10_uncertainty = 1 - q(low), das obere p90_uncertainty = q(high) - 1
    (so, dass pv*(1-p10) das low-Quantil und pv*(1+p90) das high-Quantil der
    realen Erzeugung trifft). Ersetzt den heuristischen Festwert.
    """
    if not config.pv_model.arrays:
        return None
    tz = config.general.timezone
    slot = config.general.slot_minutes
    db = config.e3dc_rscp.history_db_path
    now = pd.Timestamp(now).tz_convert(tz) if now is not None \
        else pd.Timestamp.now(tz=tz)
    end = now.floor(f"{slot}min")
    start = end - pd.Timedelta(days=lookback_days)
    sources = pvforecast.source_ids(config)

    actual = read_actual_pv(db, start, end, tz, slot)
    fc = read_group_asof(db, sources, start, end, tz, slot, "pv", lead_hours)
    method = "archive"
    pairs = _pairs(fc, actual, min_pv_w)
    if len(pairs) < 20:
        fc_cache = read_group_cache(db, sources, start, end, tz, slot, "pv")
        cp = _pairs(fc_cache, actual, min_pv_w)
        if len(cp) > len(pairs):
            method, pairs = "cache", cp
    # Nur Slots mit belastbarer Punktprognose (Nenner) zählen fürs Verhältnis.
    pairs = pairs[pairs["pred"] >= min_pv_w]
    promotion = pd.DataFrame()
    training = pairs
    if promotion_days > 0 and not pairs.empty:
        promotion_start = end - pd.Timedelta(days=int(promotion_days))
        training = pairs[pairs.index < promotion_start]
        promotion = pairs[pairs.index >= promotion_start]
    if len(training) < 20:
        return {"method": method, "n": int(len(pairs)), "insufficient": True,
                "training_n": int(len(training)),
                "promotion_n": int(len(promotion)),
                "current_p10_uncertainty": config.pv_model.p10_uncertainty,
                "current_p90_uncertainty": config.pv_model.p90_uncertainty}
    ratio = (training["actual"] / training["pred"]).to_numpy()
    q_low = float(np.quantile(ratio, low))
    q_high = float(np.quantile(ratio, high))
    p10_unc = float(np.clip(1.0 - q_low, 0.0, 0.95))
    p90_unc = float(np.clip(q_high - 1.0, 0.0, 3.0))

    # Empirische Abdeckung des AKTUELLEN Bandes zum Vergleich.
    cur10 = config.pv_model.p10_uncertainty
    cur90 = config.pv_model.p90_uncertainty
    all_ratio = (pairs["actual"] / pairs["pred"]).to_numpy()
    below = float(np.mean(all_ratio < (1.0 - cur10))) * 100.0
    above = float(np.mean(all_ratio > (1.0 + cur90))) * 100.0
    promotion_ratio = (
        (promotion["actual"] / promotion["pred"]).to_numpy()
        if not promotion.empty else np.array([], dtype=float))
    return {"method": method, "n": int(len(pairs)),
            "training_n": int(len(training)),
            "promotion_n": int(len(promotion)),
            "recommended_p10_uncertainty": round(p10_unc, 3),
            "recommended_p90_uncertainty": round(p90_unc, 3),
            "current_p10_uncertainty": cur10, "current_p90_uncertainty": cur90,
            "current_below_p10_pct": round(below, 1),
            "current_above_p90_pct": round(above, 1),
            "target_low_pct": round(low * 100, 0),
            "target_high_pct": round(high * 100, 0),
            "_promotion_ratios": promotion_ratio}
