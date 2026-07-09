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

# Zwischenspeicher für trainierte ML-Modelle je Trainingsdaten-Fingerprint.
# run_once ruft forecast() mehrfach pro Zyklus mit DERSELBEN Historie auf
# (Haupt-Prognose + Dashboard) - so wird nur einmal trainiert statt mehrfach.
_ML_CACHE: dict = {}


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

    def _forecast_ml(self, history, hist_feat, fut_feat, future_index,
                     hist_pv, fut_pv) -> np.ndarray:
        """ML-Prognose (HistGradientBoostingRegressor) mit PV- und 7-Tage-Lag-
        Feature. Wirft bei fehlendem sklearn / untauglichen Daten -> der Aufrufer
        fällt dann auf 'similar_days' zurück. Modell wird je Trainingsdaten-
        Fingerprint gecacht (mehrere forecast()-Aufrufe pro Zyklus)."""
        from sklearn.ensemble import HistGradientBoostingRegressor

        hist_feat, fut_feat = hist_feat.copy(), fut_feat.copy()
        # Feature: PV-Prognose (Helligkeits-/Aktivitäts-Proxy)
        hist_feat["pv"] = (pd.Series(hist_pv).reindex(history.index).values
                           if hist_pv is not None else np.nan)
        fut_feat["pv"] = (pd.Series(fut_pv).reindex(future_index).values
                          if fut_pv is not None else np.nan)
        # Feature: 7-Tage-Lag PER ZEITSTEMPEL (lückenrobust; positionsbasiertes
        # shift wäre bei fehlenden Slots - z.B. lokale house_load - falsch, und
        # inkonsistent zum Serving-Pfad).
        lag_td = pd.Timedelta(days=7)
        hist_feat["lag_7d"] = history.reindex(history.index - lag_td).values
        fut_feat["lag_7d"] = history.reindex(future_index - lag_td).values

        drop_cols = [c for c in ("value", "recency") if c in hist_feat.columns]
        X_train = hist_feat.drop(columns=drop_cols)
        # komplett-NaN-Spalten entfernen (z.B. fehlende Temp/PV -> HGBR-Fehler)
        nan_cols = X_train.columns[X_train.isna().all()].tolist()
        if nan_cols:
            X_train = X_train.drop(columns=nan_cols)
        y_train = hist_feat["value"].values
        w_train = hist_feat["recency"].values if "recency" in hist_feat.columns else None

        valid = ~np.isnan(y_train)
        if not valid.all():
            X_train, y_train = X_train[valid], y_train[valid]
            if w_train is not None:
                w_train = w_train[valid]
        if len(X_train) == 0 or len(X_train.columns) == 0:
            raise ValueError("keine tauglichen ML-Trainingsdaten")

        fp = (len(X_train), tuple(X_train.columns), str(X_train.index[0]),
              str(X_train.index[-1]), round(float(np.nansum(y_train)), 3))
        cached = _ML_CACHE.get(fp)
        if cached is not None:
            model, train_cols = cached
        else:
            cat = [X_train.columns.get_loc(c) for c in
                   ("slot_of_day", "weekday", "is_holiday", "daytype", "month", "season")
                   if c in X_train.columns]
            model = HistGradientBoostingRegressor(categorical_features=cat,
                                                  max_iter=150, random_state=42)
            model.fit(X_train, y_train, sample_weight=w_train)
            train_cols = list(X_train.columns)
            _ML_CACHE[fp] = (model, train_cols)
            if len(_ML_CACHE) > 4:                 # nur die letzten paar behalten
                _ML_CACHE.pop(next(iter(_ML_CACHE)))
        return model.predict(fut_feat.reindex(columns=train_cols))

    def forecast(
        self,
        history: pd.Series,
        start: datetime,
        horizon_slots: int,
        clip_min=0.0,
        apply_correction: bool = True,
        hist_temp: "pd.Series | None" = None,
        fut_temp: "pd.Series | None" = None,
        hist_pv: "pd.Series | None" = None,
        fut_pv: "pd.Series | None" = None,
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
        if use_temp:
            fut_feat["temp"] = fut_temp_arr
        else:
            fut_feat["temp"] = np.nan

        method = getattr(self.fc, "method", "similar_days")
        preds = None
        if method == "ml":
            try:
                preds = self._forecast_ml(history, hist_feat, fut_feat,
                                          future_index, hist_pv, fut_pv)
            except ImportError:
                log.error("scikit-learn nicht installiert - Fallback auf 'similar_days'.")
            except Exception as exc:
                log.error("ML-Prognose fehlgeschlagen (%s) - Fallback auf 'similar_days'.",
                          exc, exc_info=True)

        if preds is None:
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


def dampen_estimated(price: pd.Series, estimated: pd.Series,
                     damping: float) -> pd.Series:
    """Geschätzte (unsichere) Preis-Slots zur Mitte stauchen.

    p' = m + (p - m) * (1 - damping), m = Mittel der geschätzten Slots.
    Echte Börsenpreise bleiben unverändert. Verhindert, dass der Optimierer
    auf prognostizierte Preistäler/-spitzen, die es real evtl. nicht gibt,
    harte Entscheidungen (Netzladen, Arbitrage) baut.
    """
    if damping <= 0.0:
        return price
    est = estimated.reindex(price.index).fillna(False).astype(bool)
    if not est.any():
        return price
    m = float(price[est].mean())
    out = price.copy()
    out[est] = m + (price[est] - m) * (1.0 - float(damping))
    return out


def load_history(repo, config: Config, now: datetime) -> pd.Series:
    """Lädt die Verbrauchs-Historie über den konfigurierten Zeitraum.

    Quelle: standardmäßig InfluxDB (house_consumption). Ist
    e3dc_rscp.history_source aktiv, kommt sie aus der lokalen SQLite (per RSCP
    aus dem E3DC gefüllt) - Schritt Richtung Standalone.

    fill=False: Datenlücken bleiben NaN und werden von der Prognose übersprungen,
    statt als interpolierte Kunstwerte in die Ähnliche-Tage-Mittelung einzugehen.
    """
    start = now - timedelta(days=config.forecast.lookback_days)
    if config.e3dc_rscp.history_source:
        from .local_history import read_house_load
        return read_house_load(config.e3dc_rscp.history_db_path, start, now,
                               config.general.timezone)
    return repo.read_slots("house_consumption", start, now, fill=False)
