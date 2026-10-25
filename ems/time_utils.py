"""Feste Betriebsraster auf der eindeutigen Zeitachse statt lokaler Wanduhr.

Beide 02-Uhr-Stunden im Herbst bleiben getrennt; im Fruehjahr entstehen
keine nicht existierenden Slots. Kalendertage weiterhin mit normalize()
und DateOffset bestimmen (ein lokaler Tag kann 23 oder 25 Stunden haben).
"""
import pandas as pd


def _round_time(value, freq, method):
    stamp = value if isinstance(value, pd.DatetimeIndex) else pd.Timestamp(value)
    tz = stamp.tz
    axis = stamp.tz_convert("UTC") if tz is not None else stamp
    rounded = getattr(axis, method)(freq)
    return rounded.tz_convert(tz) if tz is not None else rounded


def floor_time(value, freq):
    return _round_time(value, freq, "floor")


def ceil_time(value, freq):
    return _round_time(value, freq, "ceil")


def ambiguous_wall_time(value):
    """Kann eine API ohne UTC-Offset diesen lokalen Zeitpunkt unterscheiden?"""
    stamp = pd.Timestamp(value)
    return stamp.tz is not None and pd.isna(
        stamp.tz_localize(None).tz_localize(stamp.tz, ambiguous="NaT"))
