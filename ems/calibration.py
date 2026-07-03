"""Anwendung von Kalibrier-Profilen (aus kalibrierung.py).

Ein Profil (YAML) enthält zeitabhängige Korrekturfaktoren, die auf die
PV-Vorhersage angewandt werden – je (Monat, Stunde), mit Fallback auf Stunde,
Monat und globalen Faktor. So werden systematische Tages-/Jahreszeit-Fehler
der Solcast-Vorhersage korrigiert, nicht nur ein pauschaler Skalar.

Profil-Struktur:
    pv_global: 0.87
    pv_month_hour:        # Faktor je Monat -> Stunde (Ist/Vorhersage)
      1: {8: 1.1, 9: 1.0, ...}
      2: {...}
    pv_hour: {5: 2.1, 6: 1.4, ...}     # Fallback je Stunde
    pv_month: {1: 0.73, ...}           # Fallback je Monat
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger("ems.calibration")


def load_profile(path: str) -> Optional[dict]:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        log.warning("Kalibrier-Profil %s nicht gefunden – keine Korrektur angewandt.", path)
        return None
    except Exception as exc:  # pragma: no cover
        log.warning("Kalibrier-Profil %s nicht lesbar (%s).", path, exc)
        return None


def _get(d: dict, key):
    """Holt Wert aus dict, egal ob Schlüssel int oder str ist."""
    if d is None:
        return None
    if key in d:
        return d[key]
    return d.get(str(key))


def apply_pv_correction(series: pd.Series, profile: Optional[dict], tz: str) -> pd.Series:
    """Multipliziert die PV-Vorhersage je Slot mit dem passenden Korrekturfaktor.

    Reihenfolge: (Monat,Stunde) -> Stunde -> Monat -> global -> 1.0.
    """
    if profile is None or series.empty:
        return series
    mh = profile.get("pv_month_hour") or {}
    ph = profile.get("pv_hour") or {}
    pm = profile.get("pv_month") or {}
    pg = float(profile.get("pv_global", 1.0))
    local = series.index.tz_convert(tz)
    factors = []
    for ts in local:
        f = None
        month_tab = _get(mh, ts.month)
        if isinstance(month_tab, dict):
            f = _get(month_tab, ts.hour)
        if f is None:
            f = _get(ph, ts.hour)
        if f is None:
            f = _get(pm, ts.month)
        if f is None:
            f = pg
        factors.append(float(f))
    corrected = series * pd.Series(factors, index=series.index)
    return corrected.clip(lower=0.0)
