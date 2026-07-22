"""Tarifmodell: Spotpreis (Energy-Charts) -> Endkunden-Bezugspreis (ct/kWh brutto).

Aufbau des Bezugspreises je Slot (dynamischer Tarif):

    netto[t]  = spot[t] * (1 + Aufschlag%/100) + Marge_ct
                + Steuern&Abgaben_ct + Netzentgelt[t]_ct
    brutto[t] = netto[t] * (1 + MwSt%/100)

Alle Komponenten außer dem Spot sind netto (ohne MwSt); die MwSt wird auf die
Gesamtsumme geschlagen (so wird sie in D auch auf Stromsteuer/Netzentgelt fällig).

Netzentgelt-Modi:
  * "included" – bereits in Aufschlag/Abgaben enthalten -> 0.
  * "static"   – konstanter ct/kWh-Betrag.
  * "14a"      – §14a EnWG zeitvariabel: Fensterliste (Uhrzeit/Monat/Datum/
                 Wochentag), erstes passendes Fenster gewinnt, sonst Default.

Bei fester Tarifart (`type: fixed`) entfällt der Spot; der Preis ist konstant
`fixed_ct_kwh` (brutto).
"""
from __future__ import annotations

import logging

import pandas as pd

from . import local_history

log = logging.getLogger("ems.tariff")


def _md(s: str):
    """"MM-DD" -> (Monat, Tag)."""
    m, d = s.split("-")
    return int(m), int(d)


def _window_matches(ts: pd.Timestamp, w) -> bool:
    """Passt der (lokale) Zeitstempel in das §14a-Fenster? Nicht gesetzte
    Filter zählen als 'egal'."""
    if w.hours is not None and ts.hour not in w.hours:
        return False
    if w.weekdays is not None and ts.weekday() not in w.weekdays:
        return False
    if w.months is not None and ts.month not in w.months:
        return False
    if w.date_from and w.date_to:
        lo, hi, cur = _md(w.date_from), _md(w.date_to), (ts.month, ts.day)
        if lo <= hi:
            if not (lo <= cur <= hi):
                return False
        else:  # Wrap über den Jahreswechsel (z.B. 11-01 .. 02-28)
            if not (cur >= lo or cur <= hi):
                return False
    return True


def grid_fee_series(index: pd.DatetimeIndex, tariff) -> pd.Series:
    """Netzentgelt (ct/kWh netto) je Slot gemäß Modus."""
    if tariff.grid_fee_mode == "included":
        return pd.Series(0.0, index=index)
    if tariff.grid_fee_mode == "static":
        return pd.Series(tariff.grid_fee_ct_kwh, index=index)
    # "14a": erstes passendes Fenster gewinnt, sonst Default.
    vals = []
    for ts in index:
        fee = tariff.grid_fee_default_ct_kwh
        for w in tariff.grid_fee_windows:
            if _window_matches(ts, w):
                fee = w.ct_kwh
                break
        vals.append(fee)
    return pd.Series(vals, index=index, dtype="float64")


def apply_tariff(spot_ct: pd.Series, config) -> pd.Series:
    """Spotpreis-Serie (ct/kWh netto) -> Bezugspreis (ct/kWh brutto).
    NaN-Slots (fehlende/unveröffentlichte Preise) bleiben NaN."""
    t = config.tariff
    if t.type == "fixed":
        return pd.Series(t.fixed_ct_kwh, index=spot_ct.index, dtype="float64")
    net_energy = spot_ct * (1.0 + t.markup_percent / 100.0) + t.markup_ct_kwh
    grid_fee = grid_fee_series(spot_ct.index, t)
    net = net_energy + t.levies_ct_kwh + grid_fee
    return net * (1.0 + t.vat_percent / 100.0)


def read_price_signal(config, repo, start, end) -> pd.Series:
    """Bezugspreis [start, end) (ct/kWh brutto) auf dem Slot-Raster.

    Weiche für den Standalone-Betrieb: bei tariff.enabled aus dem lokalen
    Spot-Speicher (Energy-Charts) über das Tarifmodell, sonst aus der InfluxDB
    (Signal electricity_price, dort bereits als Bezugspreis geführt).
    """
    if not config.tariff.enabled:
        return repo.read_slots("electricity_price", start, end, fill=False)
    tz = config.general.timezone
    slot_min = config.general.slot_minutes
    if config.tariff.type == "fixed":
        grid = pd.date_range(pd.Timestamp(start).tz_convert(tz),
                             pd.Timestamp(end).tz_convert(tz),
                             freq=f"{slot_min}min", inclusive="left")
        return pd.Series(config.tariff.fixed_ct_kwh, index=grid, dtype="float64")
    spot = local_history.read_spot(config.e3dc_rscp.history_db_path,
                                   start, end, tz, slot_min)
    if spot.empty:
        return spot
    return apply_tariff(spot, config)


def read_spot_signal(config, repo, start, end) -> pd.Series:
    """Unveränderter Börsenpreis (ct/kWh) für rechtliche Preisregeln.

    Er darf nicht aus dem Endkundenpreis zurückgerechnet werden, weil Aufschläge,
    Netzentgelte und MwSt dessen Vorzeichen verändern können. Ohne Rohpreis
    bleibt die Serie leer und die Negativpreisregel wird sicher ausgesetzt.
    """
    if config.tariff.enabled and config.tariff.type == "dynamic":
        return local_history.read_spot(
            config.e3dc_rscp.history_db_path, start, end,
            config.general.timezone, config.general.slot_minutes)
    if (not config.tariff.enabled
            and repo.signal_available("electricity_spot_price")):
        try:
            return repo.read_slots(
                "electricity_spot_price", start, end, fill=False)
        except (KeyError, ValueError):
            # Einige optionale Signalquellen melden das Fehlen erst beim Lesen.
            pass
    return pd.Series(dtype="float64")
