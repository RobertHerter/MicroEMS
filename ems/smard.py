"""SMARD-Day-Ahead-Preise für DE-LU als Ausweichquelle.

Quelle: Bundesnetzagentur, https://www.smard.de/home/marktdaten/
Chart-Reihe 4169, Region DE-LU, quarterhour: [Unix-Millisekunden, EUR/MWh].
Rückgabe wie energycharts.fetch_spot: {UTC-ISO: ct/kWh netto}.
Nur vollständige lokale Markttage werden übernommen, damit der Cache keine
inneren Datenlücken mit gehaltenen Preisen als echte Werte ausweist.
"""
from __future__ import annotations

from bisect import bisect_right
import json
import logging
import math
import urllib.request

import pandas as pd

log = logging.getLogger("ems.smard")
_BASE = "https://www.smard.de/app/chart_data/4169/DE-LU"


def _get(url: str, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def fetch_spot(bidding_zone: str, start_date, end_date) -> dict[str, float]:
    """Inklusive Datumsgrenzen, Markttage in Europe/Berlin (auch 23/25 h).

    Der Index liefert Wochenanfänge; keine feste Wochenlänge in UTC annehmen.
    Unveröffentlichte Tage (null/fehlend) bleiben leer und damit schätzbar.
    Andere Gebotszonen werden nicht auf deutsche Preise abgebildet.
    """
    if bidding_zone != "DE-LU":
        raise ValueError(f"SMARD-Preisfallback unterstützt nur DE-LU, nicht {bidding_zone}")
    start = pd.Timestamp(str(start_date)[:10], tz="Europe/Berlin")
    stop = pd.Timestamp(str(end_date)[:10], tz="Europe/Berlin") + pd.DateOffset(days=1)
    if start >= stop:
        raise ValueError("SMARD: Startdatum liegt nach Enddatum")
    lo, hi = int(start.timestamp() * 1000), int(stop.timestamp() * 1000)
    weeks = sorted(set(_get(f"{_BASE}/index_quarterhour.json")["timestamps"]))
    first = max(0, bisect_right(weeks, lo) - 1)
    values: dict[int, float] = {}
    for week in weeks[first:]:
        if week >= hi:
            break
        payload = _get(f"{_BASE}/4169_DE-LU_quarterhour_{week}.json")
        for stamp, price in payload["series"]:
            if not lo <= stamp < hi or price is None:
                continue
            if (isinstance(stamp, bool) or not isinstance(stamp, int)
                    or stamp % 900000 or isinstance(price, bool)
                    or not isinstance(price, (int, float)) or not math.isfinite(price)):
                raise ValueError("SMARD: ungültiger Zeitstempel oder Preis")
            value = price / 10.0
            if stamp in values and values[stamp] != value:
                raise ValueError("SMARD: widersprüchliche Preise für denselben Slot")
            values[stamp] = value

    result: dict[str, float] = {}
    for day in pd.date_range(start, stop, freq="D", inclusive="left"):
        grid = pd.date_range(day, day + pd.DateOffset(days=1),
                             freq="15min", inclusive="left").tz_convert("UTC")
        stamps = [int(t.timestamp() * 1000) for t in grid]
        count = sum(stamp in values for stamp in stamps)
        if count != len(stamps):
            if count:
                log.warning("SMARD: %s unvollständig (%d/%d Slots), Tag übersprungen.",
                            day.date(), count, len(stamps))
            continue
        result.update({t.isoformat(): values[stamp] for t, stamp in zip(grid, stamps)})
    return result
