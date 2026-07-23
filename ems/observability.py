"""Auswertungen fürs Dashboard: Ersparnis über die Zeit und Prognosegüte.

Beide Funktionen sind reine Leser/Aggregatoren über die lokale Historie (kein
RSCP/MQTT, keine Steuerung) und damit direkt testbar. Sie werden on-demand von
den Dashboard-Endpoints aufgerufen, nicht im Steuer-Zyklus.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


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
            "bias_w": round(float(np.mean(p - a)), 1),
            "mae_w": round(float(np.mean(np.abs(p - a))), 1)}


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
    return {"days": int(days), "pv": pv, "load": _load_accuracy(config, start, now)}
