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
        clip_min=0.0,
        apply_correction: bool = True,
        hist_temp: "pd.Series | None" = None,
        fut_temp: "pd.Series | None" = None,
    ) -> pd.Series:
        """Ähnliche-Tage-Prognose für `horizon_slots` ab `start`.

        Generisch nutzbar: für Verbrauch (W, clip_min=0, mit Korrekturfaktor) und
        für den Strompreis (clip_min=None, ohne Korrekturfaktor – Preise können
        negativ sein), wenn Folgetag-Werte noch fehlen.
        history: Werte auf Slot-Raster, tz-aware Index.
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
            log.warning("Keine Historie für Prognose – gebe 0 zurück.")
            return pd.Series(0.0, index=future_index.tz_convert(self.cfg.general.timezone))

        hist_feat = self._features(history.index)
        hist_feat["value"] = history.values

        # Temperatur (optional) an Historie/Zukunft anhängen
        w_tp = float(getattr(self.fc, "weight_same_temp", 0.0) or 0.0)
        sigma = float(getattr(self.fc, "temp_sigma", 4.0) or 4.0)
        use_temp = w_tp > 0 and hist_temp is not None and fut_temp is not None
        if hist_temp is not None:
            hist_feat["temp"] = pd.Series(hist_temp).reindex(history.index).values
        else:
            hist_feat["temp"] = np.nan
        fut_temp_arr = (pd.Series(fut_temp).reindex(future_index).values
                        if fut_temp is not None else np.full(len(future_index), np.nan))

        # Rezenz-Gewichtung: jüngere Historie zählt mehr (exponentieller Abfall
        # mit Halbwertszeit half_life_days). So schlagen Verhaltensänderungen
        # (neue Geräte, Wärmepumpe, Homeoffice) zeitnah durch, statt dass ein
        # Tag von vor 2 Jahren gleich viel wiegt wie letzte Woche. 0 = aus.
        hl = float(getattr(self.fc, "half_life_days", 0.0) or 0.0)
        if hl > 0:
            age_days = np.maximum(
                (pd.Timestamp(start).tz_convert("UTC") - history.index.tz_convert("UTC"))
                .total_seconds() / 86400.0, 0.0)
            hist_feat["recency"] = np.power(0.5, np.asarray(age_days) / hl)
        else:
            hist_feat["recency"] = 1.0

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
            # Rezenz multiplikativ (1.0, falls half_life_days = 0)
            w = w * grp["recency"].values

            # Temperatur-Ähnlichkeit: Tage mit ähnlicher Temperatur höher gewichten
            if use_temp:
                tf = fut_temp_arr[i]
                th = grp["temp"].values
                if np.isfinite(tf):
                    gk = np.exp(-((th - tf) ** 2) / (2.0 * sigma * sigma))
                    gk = np.where(np.isnan(th), 0.0, gk)  # fehlende Temp -> neutral
                    w = w * (1.0 + w_tp * gk)

            vals = grp["value"].values
            # Fallback: zu wenig "ähnliche" Stichproben -> reines Slot-Mittel
            strongly_similar = (grp["daytype"].values == f["daytype"])
            if strongly_similar.sum() < self.fc.min_samples:
                preds[i] = float(np.average(vals, weights=w))
            else:
                sw = w * (1 + 2 * strongly_similar)  # ähnliche Tage stärker gewichten
                preds[i] = float(np.average(vals, weights=sw))

        # Globaler Korrekturfaktor aus der Kalibrierung (nur Verbrauch)
        if apply_correction:
            preds = preds * float(getattr(self.fc, "correction_factor", 1.0))
        result = pd.Series(preds, index=future_index)
        if clip_min is not None:
            result = result.clip(lower=clip_min)
        return result.tz_convert(self.cfg.general.timezone)


def intraday_ratio(actual: pd.Series, predicted: pd.Series,
                   min_mean: float = 0.0, min_samples: int = 4,
                   max_factor: float = 1.5):
    """Ist/Prognose-Verhältnis über das jüngste Zeitfenster.

    Fängt Tagesabweichungen (Besuch, Homeoffice, Wetter), die das
    Ähnliche-Tage-Modell nicht sehen kann. None, wenn zu wenig Daten oder die
    Prognose zu klein für ein stabiles Verhältnis ist (z.B. PV nachts).
    Das Ergebnis ist auf [1/max_factor, max_factor] begrenzt.
    """
    df = pd.DataFrame({"a": actual, "p": predicted}).dropna()
    if len(df) < min_samples:
        return None
    mean_a, mean_p = float(df["a"].mean()), float(df["p"].mean())
    if mean_p <= min_mean or mean_a < 0.0:
        return None
    return float(np.clip(mean_a / mean_p, 1.0 / max_factor, max_factor))


def intraday_factor_series(ratio, index: pd.DatetimeIndex, now,
                           decay_hours: float = 6.0) -> pd.Series:
    """Korrekturfaktor je Zukunfts-Slot: volle Korrektur jetzt, exponentiell
    abklingend mit der Lead-Time (Halbwertszeit decay_hours) - weit voraus
    gilt wieder das Ähnliche-Tage-Modell."""
    if ratio is None:
        return pd.Series(1.0, index=index)
    lead_h = np.maximum(
        (index - pd.Timestamp(now)).total_seconds() / 3600.0, 0.0)
    w = np.power(0.5, np.asarray(lead_h) / max(decay_hours, 0.1))
    return pd.Series(1.0 + (float(ratio) - 1.0) * w, index=index)


def load_history(repo, config: Config, now: datetime) -> pd.Series:
    """Lädt die Verbrauchs-Historie über den konfigurierten Zeitraum.

    fill=False: Datenlücken (Sensor-/DB-Ausfälle) bleiben NaN und werden von
    der Prognose übersprungen, statt als interpolierte Kunstwerte in die
    Ähnliche-Tage-Mittelung einzufließen.
    """
    start = now - timedelta(days=config.forecast.lookback_days)
    return repo.read_slots("house_consumption", start, now, fill=False)
