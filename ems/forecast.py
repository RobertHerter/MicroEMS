"""Hausverbrauchs-Prognose per Ähnliche-Tage-Mittelung.

Für jeden zukünftigen 15-min-Slot werden aus der Historie die Werte des
gleichen Tagesslots gesammelt und nach Ähnlichkeit gewichtet gemittelt.
Ähnlichkeits-Kriterien: gleicher Wochentag, gleicher Tagestyp
(Werktag/Wochenende/Feiertag), gleicher Monat, gleiche Jahreszeit.

Ist für einen Slot zu wenig Historie vorhanden, greift eine Fallback-Kaskade
(nur Tagesslot -> Gesamtmittel).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from .config import Config

log = logging.getLogger("ems.forecast")


def _season(month: np.ndarray) -> np.ndarray:
    """Meteorologische Jahreszeit: 0=Winter,1=Frühling,2=Sommer,3=Herbst."""
    return ((month % 12) // 3).astype(int)


def _daytype(weekday: np.ndarray, is_holiday: np.ndarray) -> np.ndarray:
    """0=Werktag, 1=Wochenende, 2=Feiertag."""
    dt = np.where(weekday >= 5, 1, 0)
    dt = np.where(is_holiday, 2, dt)
    return dt.astype(int)


class LoadForecaster:
    def __init__(self, config: Config):
        self.cfg = config
        self.fc = config.forecast
        self.slot_minutes = config.general.slot_minutes
        self.slots_per_day = 24 * config.general.slots_per_hour
        self._holidays = self._build_holidays()

    def _build_holidays(self):
        import holidays

        subdiv = self.fc.holiday_subdivision
        try:
            return holidays.country_holidays(
                self.fc.holiday_country,
                subdiv=subdiv,
                years=range(datetime.now().year - 5, datetime.now().year + 2),
            )
        except Exception as exc:  # pragma: no cover
            log.warning("Feiertage konnten nicht geladen werden (%s) – ignoriere Feiertage.", exc)
            return {}

    # ------------------------------------------------------------------ #
    def _features(self, index: pd.DatetimeIndex) -> pd.DataFrame:
        local = index.tz_convert(self.cfg.general.timezone)
        slot_of_day = (local.hour * 60 + local.minute) // self.slot_minutes
        weekday = local.weekday.values
        dates = local.normalize()
        is_holiday = np.array([d.date() in self._holidays for d in dates])
        month = local.month.values
        return pd.DataFrame(
            {
                "slot_of_day": np.asarray(slot_of_day, dtype=int),
                "weekday": weekday.astype(int),
                "is_holiday": is_holiday,
                "daytype": _daytype(weekday, is_holiday),
                "month": month.astype(int),
                "season": _season(month),
            },
            index=index,
        )

    def forecast(
        self,
        history: pd.Series,
        start: datetime,
        horizon_slots: int,
    ) -> pd.Series:
        """Erzeugt die Verbrauchsprognose (W) für `horizon_slots` ab `start`.

        history: Verbrauch (W) auf Slot-Raster, tz-aware Index.
        """
        freq = f"{self.slot_minutes}min"
        future_index = pd.date_range(
            start=pd.Timestamp(start).tz_convert("UTC").floor(freq),
            periods=horizon_slots,
            freq=freq,
            tz="UTC",
        )

        history = history.dropna()
        if history.empty:
            log.warning("Keine Historie für Verbrauchsprognose – gebe 0 W zurück.")
            return pd.Series(0.0, index=future_index.tz_convert(self.cfg.general.timezone))

        hist_feat = self._features(history.index)
        hist_feat["value"] = history.values

        fut_feat = self._features(future_index)

        # Vorberechnung: Historie nach Tagesslot gruppieren (dict of DataFrames)
        groups = {sod: grp for sod, grp in hist_feat.groupby("slot_of_day")}
        overall_mean = float(history.mean())

        w_wd = self.fc.weight_same_weekday
        w_dt = self.fc.weight_same_daytype
        w_mo = self.fc.weight_same_month
        w_se = self.fc.weight_same_season

        preds = np.empty(len(fut_feat), dtype="float64")
        for i, (_, f) in enumerate(fut_feat.iterrows()):
            grp = groups.get(int(f["slot_of_day"]))
            if grp is None or len(grp) < 1:
                preds[i] = overall_mean
                continue

            # Basisgewicht 1 + Zuschläge für Übereinstimmungen
            w = np.ones(len(grp))
            w += w_wd * (grp["weekday"].values == f["weekday"])
            w += w_dt * (grp["daytype"].values == f["daytype"])
            w += w_mo * (grp["month"].values == f["month"])
            w += w_se * (grp["season"].values == f["season"])

            vals = grp["value"].values
            # Fallback: zu wenig "ähnliche" Stichproben -> reines Slot-Mittel
            strongly_similar = (grp["daytype"].values == f["daytype"])
            if strongly_similar.sum() < self.fc.min_samples:
                preds[i] = float(np.average(vals, weights=w))
            else:
                sw = w * (1 + 2 * strongly_similar)  # ähnliche Tage stärker gewichten
                preds[i] = float(np.average(vals, weights=sw))

        # Globaler Korrekturfaktor aus der Kalibrierung (kalibrierung.py)
        preds = preds * float(getattr(self.fc, "correction_factor", 1.0))
        result = pd.Series(preds, index=future_index).clip(lower=0.0)
        return result.tz_convert(self.cfg.general.timezone)


def load_history(repo, config: Config, now: datetime) -> pd.Series:
    """Lädt die Verbrauchs-Historie über den konfigurierten Zeitraum."""
    start = now - timedelta(days=config.forecast.lookback_days)
    return repo.read_slots("house_consumption", start, now)
