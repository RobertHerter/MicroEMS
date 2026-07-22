"""Debug-Schnappschuss zum Reproduzieren eines Laufs.

save_snapshot() sichert je Zyklus alles, was zum REPRODUZIEREN nötig ist: die
Optimierer-Eingaben (PV/Last/Preis/Einspeisung/SoC), die modellrelevante
Konfiguration (ohne Zugangsdaten), den Plan, die Verstöße der Planprüfung und
die SoC-Drift - als eine JSON-Datei.

Der Debug-Button im Dashboard lädt diese Datei herunter und öffnet das
Mailprogramm vorausgefüllt (Anhang manuell) - kein Server-SMTP nötig. Mit dem
JSON lässt sich der Fehler offline exakt nachstellen (backtest / Optimizer).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, is_dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .config import Config

log = logging.getLogger("ems.debug")


def _safe_config(config: Config) -> dict:
    """Nur die modellrelevanten Parameter – KEINE Zugangsdaten (InfluxDB/MQTT/
    SMTP)."""
    def d(obj):
        return {k: (str(v) if not isinstance(v, (int, float, bool, type(None), dict, list)) else v)
                for k, v in asdict(obj).items()} if is_dataclass(obj) else {}
    return {
        "general": d(config.general),
        "house_battery": d(config.house_battery),
        "inverter": d(config.inverter),
        "vehicle": d(config.vehicle),
        "feed_in": d(config.feed_in),
        "optimization": d(config.optimization),
        "forecast": d(config.forecast),
    }


def save_snapshot(config: Config, now, inputs, result, violations,
                  drift_mae: Optional[float] = None) -> str:
    """Schreibt den Debug-Schnappschuss (atomar). Rückgabe: Pfad."""
    path = config.report.snapshot_path
    t = result.table

    def arr(a):
        return [None if (x is None or (isinstance(x, float) and np.isnan(x)))
                else round(float(x), 3) for x in np.asarray(a)]

    snap = {
        "generated": pd.Timestamp(now).isoformat(),
        "status": result.status,
        "infeasible": bool(result.infeasible),
        "solver_hit_limit": bool(result.solver_hit_limit),
        "total_cost_eur": round(result.total_cost_ct / 100.0, 3),
        "car_target_shortfall_wh": round(result.car_target_shortfall_wh, 1),
        "drift_soc_mae_pp": (round(drift_mae, 2) if drift_mae is not None else None),
        "violations": [
            {"rule": v.rule, "severity": v.severity, "count": v.count,
             "detail": v.detail,
             "slots": [s.isoformat() for s in v.slots[:20]]}
            for v in (violations or [])
        ],
        "config": _safe_config(config),
        "inputs": {
            "index": [ts.isoformat() for ts in inputs.index],
            "house_load_w": arr(inputs.house_load_w),
            "pv_w": arr(inputs.pv_w),
            "pv10_w": arr(inputs.pv10_w) if inputs.pv10_w is not None else None,
            "price_ct_kwh": arr(inputs.price_ct_kwh),
            "spot_price_ct_kwh": (arr(inputs.spot_price_ct_kwh)
                                   if inputs.spot_price_ct_kwh is not None else None),
            "feedin_ct_kwh": arr(inputs.feedin_ct_kwh),
            "initial_house_soc_wh": round(float(inputs.initial_house_soc_wh), 1),
            "initial_car_soc_wh": (round(float(inputs.initial_car_soc_wh), 1)
                                   if inputs.initial_car_soc_wh is not None else None),
            "car_present": bool(inputs.car_present),
            # Für exakt reproduzierbare Re-Solves (Solver-Laufzeit-Analyse) müssen
            # auch die Eingänge des Pool-/Thermomodells mit rein.
            "ambient_temp_c": (arr(inputs.ambient_temp_c)
                               if inputs.ambient_temp_c is not None else None),
            "solar_w_m2": (arr(inputs.solar_w_m2)
                            if inputs.solar_w_m2 is not None else None),
            "load_state": (inputs.load_state or None),
            "load_feedback": (inputs.load_feedback or None),
        },
        "plan": {c: arr(t[c]) for c in t.columns
                 if pd.api.types.is_numeric_dtype(t[c])},
        "plan_mode": [str(m) for m in t["mode"]] if "mode" in t.columns else None,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)
    return path
