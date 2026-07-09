"""Day-Ahead-Börsenpreis direkt von Energy-Charts (kostenlos, kein Key).

Ersetzt für den Standalone-Betrieb das InfluxDB-Signal `electricity_price`.
Energy-Charts liefert den reinen Spotpreis (EPEX Day-Ahead) der Gebotszone in
EUR/MWh; daraus wird über das Tarifmodell (ems/tariff.py) der Endkunden-
Bezugspreis (ct/kWh brutto) berechnet.

  * Recent: viertelstündliche Auflösung (900 s).
  * Ältere Zeiträume: teils stündlich – beim Auslesen auf das Slot-Raster
    gehalten (ems/local_history.read_spot).

Rückgabe stets {UTC-ISO-Zeitstempel: ct/kWh Spot} (netto, ohne Steuern/Abgaben).
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Dict

import pandas as pd

log = logging.getLogger("ems.energycharts")

_PRICE = "https://api.energy-charts.info/price"


def _get(url: str, params: dict, timeout: float = 30.0) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=timeout) as r:
        return json.load(r)


def _to_map(payload: dict) -> Dict[str, float]:
    """Energy-Charts price -> {UTC-ISO: ct/kWh}. EUR/MWh -> /10 = ct/kWh."""
    secs = payload.get("unix_seconds", []) or []
    prices = payload.get("price", []) or []
    out: Dict[str, float] = {}
    for s, p in zip(secs, prices):
        if p is None:
            continue
        ts = pd.Timestamp(int(s), unit="s", tz="UTC").isoformat()
        out[ts] = float(p) / 10.0
    return out


def fetch_spot(bidding_zone: str, start_date, end_date) -> Dict[str, float]:
    """Spotpreis [start_date, end_date] der Gebotszone (z.B. 'DE-LU').

    start_date/end_date: 'YYYY-MM-DD' oder etwas, das str() darauf abbildet.
    """
    d = _get(_PRICE, {
        "bzn": bidding_zone,
        "start": str(start_date)[:10],
        "end": str(end_date)[:10]})
    return _to_map(d)
