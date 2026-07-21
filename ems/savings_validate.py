"""Ersparnis gegen die echten E3DC-Zählerwerte gegenprüfen.

Das laufende Ersparnis-Tracking (ems/savings.py) leitet die Ist-Kosten aus dem
Momentan-Signal ``grid_power`` (actuals.grid_w) ab. Dieses Modul rechnet dieselbe
Woche unabhängig aus den GEMESSENEN 15-min-Energieaggregaten der E3DC-Historie
nach (Wh je Fenster: Netzbezug/-einspeisung, PV, Last, Akku) und

  1. bestätigt anhand der Energiebilanz, dass die Vorzeichen stimmen
     (PV + Akku-Entladung + Netzbezug == Last + Akku-Ladung + Einspeisung),
  2. beziffert die reale Ersparnis (metered) gegen die "Ohne-EMS"-Baseline
     (natural_battery_step auf den gemessenen PV/Last-Werten),
  3. vergleicht die aus actuals.grid_w integrierten Kosten/Energien mit den
     Zählerwerten und schlägt Alarm bei einem Vorzeichen-/Integrationsfehler.

``reconcile`` ist rein (kein I/O) und wird direkt getestet; die RSCP-/DB-/Preis-
Beschaffung erledigt savings_check.py.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .optimizer import natural_battery_step

METER_COLUMNS = ("pv_wh", "load_wh", "bat_in_wh", "bat_out_wh",
                 "grid_import_wh", "grid_export_wh")


def _eur(import_wh, export_wh, price_ct, feedin_ct):
    """Netto-Netzkosten (EUR) eines Slots: Bezug*Preis - Einspeisung*Vergütung."""
    return (import_wh / 1000.0 * price_ct - export_wh / 1000.0 * feedin_ct) / 100.0


def reconcile(meter: pd.DataFrame, price: pd.Series, feedin: pd.Series, cfg,
              actual_grid_w: Optional[pd.Series] = None,
              soc0_wh: Optional[float] = None) -> dict:
    """Zähler-Wahrheit gegen Baseline und gegen das Tracking-Signal prüfen.

    meter: DataFrame (Index = Slot-Zeitstempel) mit METER_COLUMNS in Wh je Slot.
    price/feedin: ct/kWh, auf meter.index ausgerichtet.
    actual_grid_w: optional das im Tracking genutzte grid_w (W, +Bezug/-Einspeisung).
    soc0_wh: Start-SoC der Baseline-Simulation (sonst min_soc).
    """
    hb = cfg.house_battery
    dt = cfg.general.dt_hours
    df = meter.reindex(columns=METER_COLUMNS).astype(float)
    idx = df.index
    price = price.reindex(idx).astype(float)
    feedin = feedin.reindex(idx).astype(float)
    if cfg.feed_in.zero_at_negative_price:
        feedin = feedin.where(price >= 0.0, 0.0)

    valid = df.notna().all(axis=1) & price.notna() & feedin.notna()
    df, price, feedin = df[valid], price[valid], feedin[valid]
    idx = df.index
    if df.empty:
        return {"n_slots": 0, "insufficient": True}

    # 1) Energiebilanz-Residuum (Wh): sollte ~0 sein -> Vorzeichen konsistent.
    resid = (df["pv_wh"] + df["bat_out_wh"] + df["grid_import_wh"]
             - df["bat_in_wh"] - df["grid_export_wh"] - df["load_wh"])
    energy_scale = max(1.0, float(df["load_wh"].mean()))

    # 2) Metered Ist-Kosten + "Ohne-EMS"-Baseline auf denselben PV/Last-Werten.
    m_imp_wh = df["grid_import_wh"].to_numpy()
    m_exp_wh = df["grid_export_wh"].to_numpy()
    m_cost = float(np.sum([_eur(i, e, p, f) for i, e, p, f in
                           zip(m_imp_wh, m_exp_wh, price, feedin)]))

    soc = float(soc0_wh) if soc0_wh is not None else hb.min_soc_wh
    b_cost = 0.0
    b_imp_kwh = b_exp_kwh = 0.0
    pv_w = (df["pv_wh"] / dt).to_numpy()       # Wh/Slot -> mittlere Leistung (W)
    load_w = (df["load_wh"] / dt).to_numpy()
    for k in range(len(idx)):
        soc, _c, _d, imp_w, exp_w = natural_battery_step(
            soc, pv_w[k], load_w[k], hb, dt,
            max_export_w=cfg.inverter.max_export_w)
        imp_wh, exp_wh = imp_w * dt, exp_w * dt
        b_cost += _eur(imp_wh, exp_wh, float(price.iloc[k]), float(feedin.iloc[k]))
        b_imp_kwh += imp_wh / 1000.0
        b_exp_kwh += exp_wh / 1000.0

    saved = b_cost - m_cost
    out = {
        "n_slots": int(len(idx)),
        "start": idx[0], "end": idx[-1],
        "balance": {
            "mean_abs_wh": round(float(resid.abs().mean()), 1),
            "max_abs_wh": round(float(resid.abs().max()), 1),
            "mean_signed_wh": round(float(resid.mean()), 1),
            "ok": bool(resid.abs().mean() < 0.05 * energy_scale)},
        "meter": {
            "pv_kwh": round(float(df["pv_wh"].sum()) / 1000.0, 2),
            "load_kwh": round(float(df["load_wh"].sum()) / 1000.0, 2),
            "import_kwh": round(float(m_imp_wh.sum()) / 1000.0, 2),
            "export_kwh": round(float(m_exp_wh.sum()) / 1000.0, 2),
            "net_cost_eur": round(m_cost, 2)},
        "baseline": {
            "import_kwh": round(b_imp_kwh, 2),
            "export_kwh": round(b_exp_kwh, 2),
            "net_cost_eur": round(b_cost, 2)},
        "saved_eur": round(saved, 2),
        # Bezug auf den BETRAG der Baseline-Netzkosten: bei einem Netto-
        # Exporteur (Baseline < 0, also Einnahmen) behält die Ersparnis so ihr
        # korrektes Vorzeichen (+ = besser als ohne EMS).
        "saved_pct": (round(100.0 * saved / abs(b_cost), 1)
                      if abs(b_cost) > 1e-6 else None),
    }

    # 3) Gegen das Tracking-Signal (actuals.grid_w) abgleichen.
    if actual_grid_w is not None:
        g = actual_grid_w.reindex(idx).astype(float)
        g = g.where(g.notna(), 0.0).to_numpy()
        a_imp_wh = np.maximum(0.0, g) * dt
        a_exp_wh = np.maximum(0.0, -g) * dt
        a_cost = float(np.sum([_eur(i, e, p, f) for i, e, p, f in
                               zip(a_imp_wh, a_exp_wh, price, feedin)]))
        # Passt die aktuelle Zuordnung besser als die vertauschte? (Vorzeichen)
        fit_direct = (abs(a_imp_wh.sum() - m_imp_wh.sum())
                      + abs(a_exp_wh.sum() - m_exp_wh.sum()))
        fit_swapped = (abs(a_imp_wh.sum() - m_exp_wh.sum())
                       + abs(a_exp_wh.sum() - m_imp_wh.sum()))
        out["tracker"] = {
            "import_kwh": round(float(a_imp_wh.sum()) / 1000.0, 2),
            "export_kwh": round(float(a_exp_wh.sum()) / 1000.0, 2),
            "net_cost_eur": round(a_cost, 2),
            "cost_delta_eur": round(a_cost - m_cost, 2),
            "sign_ok": bool(fit_direct <= fit_swapped)}
    return out
