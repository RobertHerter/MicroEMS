"""Plausibilitäts-Grenzen für externe Eingaben (Defense-in-Depth).

Seit das EMS real steuert, darf ein einzelner Ausreißer einer externen Quelle
(Solcast/pvlib-PV, Energy-Charts-Preis) keinen kompletten Zyklus verzerren.
Vor dem Solve werden die Eingaben deshalb gegen harte Grenzen geprüft und
implausible Werte robust ersetzt (statt sie ungeprüft in die Optimierung zu
lassen). Ergänzt die bereits vorhandene Frische-Ampel um eine Gültigkeitsprüfung.

Bewusst konservativ: die Regeln greifen nur bei klar unmöglichen Werten
(negativer PV, negative Last, Preis-Spike vielfach über dem Median, PV über der
installierten Leistung). Bei sauberen Daten passiert nichts.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger("ems.sanity")


def _report(name: str, mask: pd.Series, detail: str) -> Optional[dict]:
    n = int(mask.sum())
    if not n:
        return None
    log.warning("Sanity-Grenze: %d Slot(e) in '%s' %s.", n, name, detail)
    return {"signal": name, "n": n, "detail": detail}


def sanitize_pv(pv: pd.Series, name: str, cap_w: Optional[float],
                reports: list) -> pd.Series:
    """Negative PV -> 0, oberhalb der plausiblen Maximalleistung -> gekappt."""
    if pv is None or pv.empty:
        return pv
    out = pv.copy()
    neg = out < 0.0
    r = _report(name, neg, "negativ -> auf 0 gesetzt")
    if r:
        reports.append(r)
    out = out.clip(lower=0.0)
    if cap_w and cap_w > 0.0:
        over = out > cap_w
        r = _report(name, over, f"über {cap_w:.0f} W -> gekappt")
        if r:
            reports.append(r)
        out = out.clip(upper=cap_w)
    return out


def sanitize_price(price: pd.Series, min_ct: float, max_ct: float,
                   spike_factor: float, reports: list) -> pd.Series:
    """Preise außerhalb [min, max] oder als Spike (Vielfaches des robusten
    Medians) durch den Median ersetzen. Negative Preise bleiben zulässig."""
    if price is None or price.empty:
        return price
    out = price.copy()
    finite = out[np.isfinite(out)]
    if finite.empty:
        return out
    med = float(np.median(finite))
    ref = max(abs(med), 5.0)                 # Median-Boden gegen Tiny-Median-Blowup
    bad = (out < min_ct) | (out > max_ct)
    if spike_factor and spike_factor > 0.0:
        bad = bad | (out.abs() > spike_factor * ref)
    bad = bad & np.isfinite(out)
    r = _report("Strompreis", bad,
                f"außerhalb [{min_ct:.0f}, {max_ct:.0f}] ct oder Spike "
                f"(>{spike_factor:g}x Median) -> Median {med:.1f} ct")
    if r:
        reports.append(r)
        out = out.mask(bad, med)
    return out


def sanitize_load(load: pd.Series, max_w: float, reports: list) -> pd.Series:
    """Negative Hauslast -> 0, absurde Spitzen -> optionaler Cap."""
    if load is None or load.empty:
        return load
    out = load.copy()
    neg = out < 0.0
    r = _report("Hauslast", neg, "negativ -> auf 0 gesetzt")
    if r:
        reports.append(r)
    out = out.clip(lower=0.0)
    if max_w and max_w > 0.0:
        over = out > max_w
        r = _report("Hauslast", over, f"über {max_w:.0f} W -> gekappt")
        if r:
            reports.append(r)
        out = out.clip(upper=max_w)
    return out


def _pv_cap_w(config) -> Optional[float]:
    """Plausible PV-Obergrenze (W): konfiguriert, sonst aus installierter kWp."""
    sn = config.sanity
    if sn.pv_max_w and sn.pv_max_w > 0.0:
        return float(sn.pv_max_w)
    arrays = getattr(config.pv_model, "arrays", None)
    if arrays:
        return sum(a.kwp for a in arrays) * 1000.0 * 1.15
    return None


def sanitize_inputs(config, price: pd.Series, pv: pd.Series,
                    pv10: Optional[pd.Series],
                    load: pd.Series) -> Tuple[pd.Series, pd.Series,
                                              Optional[pd.Series], pd.Series]:
    """Alle externen Eingaben vor dem Solve begrenzen. Rückgabe:
    (price, pv, pv10, load), zusätzlich config._sanity_reports (Liste) für
    Alarm/Dashboard."""
    sn = config.sanity
    reports: list = []
    cap = _pv_cap_w(config)
    price = sanitize_price(price, sn.price_min_ct, sn.price_max_ct,
                           sn.price_spike_median_factor, reports)
    pv = sanitize_pv(pv, "PV", cap, reports)
    if pv10 is not None:
        pv10 = sanitize_pv(pv10, "PV-p10", cap, reports)
    load = sanitize_load(load, sn.load_max_w, reports)
    config._sanity_reports = reports
    return price, pv, pv10, load
