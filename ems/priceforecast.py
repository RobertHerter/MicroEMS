"""Börsenpreis-Prognose für noch nicht veröffentlichte Slots.

Der Day-Ahead-Preis für morgen erscheint erst gegen 13:00. Bis dahin braucht der
Optimierer Schätzwerte. Bisher lieferte sie eine Ähnliche-Tage-Mittelung der
eigenen Preishistorie - ohne jeden Wettereingang, obwohl der Preis der
**Merit-Order über der Residuallast** folgt (deutsche Last minus Wind und Solar).

Dieses Modul lernt genau diese Übertragung: deutschlandweite Wetter-Indizes
(``ems/gridweather.py``) plus Kalender und ein Preisanker des letzten
veröffentlichten Tages -> Börsenpreis je Slot. Gelernt wird auf der eigenen
Spotpreis-Historie, also ohne externe Modelldateien.

Gemessen auf 60 Tagen rollierender Validierung (Anlage Herter, Juli 2026):
MAE 2,51 ct/kWh gegen 3,40 ct/kWh der Ähnliche-Tage-Schätzung, besser an 50 von
60 Tagen. Vor dem produktiven Einsatz prüft ``PriceModel`` das aber SELBST auf
einem Holdout nach - gewinnt die Ähnliche-Tage-Schätzung, wird sie benutzt
(gleiches Prinzip wie die PV-Quellenwahl in ``ems/pv_eval.py``).
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("ems.priceforecast")

_FEATURES = ("wind_index", "solar_index", "temp_index", "hour_sin", "hour_cos",
             "weekday", "holiday", "anchor_mean", "anchor_spread")


def _holiday_set(config) -> set:
    try:
        import holidays as hol
        years = range(pd.Timestamp.now().year - 2, pd.Timestamp.now().year + 3)
        return set(hol.Germany(
            years=years,
            subdiv=getattr(config.general, "holiday_subdivision", None)).keys())
    except Exception as exc:                                # pragma: no cover
        log.debug("Feiertagsliste nicht verfügbar (%s).", exc)
        return set()


def build_features(index: pd.DatetimeIndex, weather: pd.DataFrame,
                   anchor_mean: float, anchor_spread: float,
                   holidays: set) -> Optional[np.ndarray]:
    """Merkmalsmatrix für die Slots in ``index``.

    ``weather`` sind die Stundenwerte aus ``read_grid_weather`` (UTC-Index).
    Die Anker (Mittelwert und Spanne des letzten VOLLSTÄNDIG veröffentlichten
    Tages) tragen das Preisniveau: ohne sie könnte das Modell einer
    Gaspreis-Verschiebung oder dem Jahresgang nicht folgen, weil das Wetter
    darüber nichts sagt. Sie sind zur Prognosezeit bekannt.

    None, wenn Wetterwerte fehlen - dann darf nicht geraten werden.
    """
    if weather is None or weather.empty:
        return None
    # In UTC runden: lokal existiert an der Zeitumstellung eine doppelte Stunde.
    hours = index.tz_convert("UTC").floor("h")
    aligned = weather.reindex(hours)
    if aligned.isna().any().any():
        return None
    local = index.tz_convert(index.tz)
    hour = local.hour.to_numpy(dtype="float64") + \
        local.minute.to_numpy(dtype="float64") / 60.0
    return np.column_stack([
        aligned["wind_index"].to_numpy(dtype="float64"),
        aligned["solar_index"].to_numpy(dtype="float64"),
        aligned["temp_index"].to_numpy(dtype="float64"),
        np.sin(2.0 * np.pi * hour / 24.0),
        np.cos(2.0 * np.pi * hour / 24.0),
        local.dayofweek.to_numpy(dtype="float64"),
        np.array([1.0 if d in holidays else 0.0 for d in local.date]),
        np.full(len(index), float(anchor_mean)),
        np.full(len(index), float(anchor_spread)),
    ])


class PriceModel:
    """Gelerntes Preismodell mit Selbstprüfung gegen die Ähnliche-Tage-Schätzung.

    ``fit`` lernt auf der Spotpreis-Historie und bewertet sich auf einem
    Holdout der letzten Tage. ``predict`` liefert nur Werte, wenn das Modell
    dort gewonnen hat - sonst None, und der Aufrufer bleibt beim alten Schätzer.
    """

    def __init__(self, config):
        self.config = config
        self.model = None
        self.trained_at: Optional[pd.Timestamp] = None
        self.mae_model: Optional[float] = None
        self.mae_baseline: Optional[float] = None
        self.train_days = 0
        self.reason = "noch nicht trainiert"
        self._holidays = _holiday_set(config)

    # -- Trainingsdaten ---------------------------------------------------- #
    def _day_rows(self, spot: pd.Series, weather: pd.DataFrame):
        """Je Tag (Merkmale, Zielwerte). Ein Tag zählt nur mit vollständigem
        Vortag (Preisanker) und lückenlosem Wetter."""
        rows = []
        by_day = {day: frame for day, frame in spot.groupby(spot.index.date)}
        for day in sorted(by_day):
            today = by_day[day]
            prev = by_day.get(day - pd.Timedelta(days=1).to_pytimedelta())
            if prev is None or len(prev) < 90 or len(today) < 90:
                continue
            x = build_features(today.index, weather, float(prev.mean()),
                               float(prev.max() - prev.min()), self._holidays)
            if x is None:
                continue
            rows.append((pd.Timestamp(day), x,
                         today.to_numpy(dtype="float64")))
        return rows

    def _baseline(self, spot: pd.Series, start: pd.Timestamp, n: int):
        """Dieselbe Ähnliche-Tage-Schätzung, die ohne Modell benutzt würde."""
        from .forecast import LoadForecaster
        history = spot[spot.index < start]
        if history.empty:
            return None
        try:
            return LoadForecaster(self.config).forecast(
                history, start, n, clip_min=None,
                apply_correction=False).to_numpy(dtype="float64")
        except Exception as exc:                            # pragma: no cover
            log.debug("Baseline-Schätzung fehlgeschlagen (%s).", exc)
            return None

    # -- Training ---------------------------------------------------------- #
    def fit(self, spot: pd.Series, weather: pd.DataFrame,
            holdout_days: int = 14, min_train_days: int = 60) -> bool:
        """Modell lernen und gegen die Baseline prüfen. True = einsatzbereit."""
        try:
            from sklearn.ensemble import HistGradientBoostingRegressor
        except Exception as exc:                            # pragma: no cover
            self.reason = f"scikit-learn fehlt ({exc})"
            return False
        rows = self._day_rows(spot.dropna(), weather)
        self.train_days = len(rows)
        if len(rows) < min_train_days + holdout_days:
            self.reason = (f"zu wenig Historie ({len(rows)} Tage, nötig "
                           f"{min_train_days + holdout_days})")
            return False

        def make():
            # Bewusst konservativ und mit festem Seed: derselbe Input muss
            # denselben Plan ergeben (der Rest des Systems setzt das voraus).
            return HistGradientBoostingRegressor(
                max_iter=300, learning_rate=0.06, max_depth=6,
                min_samples_leaf=40, l2_regularization=1.0, random_state=42)

        # Holdout: die letzten Tage NICHT mittrainieren und dort beide
        # Verfahren vergleichen. Nur so ist die Wahl belastbar.
        cut = len(rows) - holdout_days
        x_train = np.vstack([r[1] for r in rows[:cut]])
        y_train = np.concatenate([r[2] for r in rows[:cut]])
        probe = make()
        probe.fit(x_train, y_train)
        errs_model, errs_base = [], []
        for day, x, y in rows[cut:]:
            pred = probe.predict(x)
            errs_model.append(float(np.mean(np.abs(pred - y))))
            start = pd.Timestamp(day).tz_localize(spot.index.tz)
            base = self._baseline(spot, start, len(y))
            if base is not None and len(base) == len(y):
                errs_base.append(float(np.mean(np.abs(base - y))))
        self.mae_model = round(float(np.mean(errs_model)), 2) if errs_model else None
        self.mae_baseline = round(float(np.mean(errs_base)), 2) if errs_base else None
        if self.mae_model is None:
            self.reason = "Holdout nicht bewertbar"
            return False
        if self.mae_baseline is not None and self.mae_model >= self.mae_baseline:
            self.reason = (f"Ähnliche Tage besser ({self.mae_baseline} ct gegen "
                           f"{self.mae_model} ct) - Modell nicht benutzt")
            self.model = None
            return False

        # Für den Einsatz auf ALLEN Tagen neu lernen (inkl. Holdout).
        final = make()
        final.fit(np.vstack([r[1] for r in rows]),
                  np.concatenate([r[2] for r in rows]))
        self.model = final
        self.trained_at = pd.Timestamp.now(tz="UTC")
        self.reason = (f"aktiv: MAE {self.mae_model} ct gegen "
                       f"{self.mae_baseline} ct (Ähnliche Tage), "
                       f"{self.train_days} Tage gelernt")
        return True

    # -- Anwendung --------------------------------------------------------- #
    def predict(self, index: pd.DatetimeIndex, weather: pd.DataFrame,
                anchor: pd.Series) -> Optional[pd.Series]:
        """Börsenpreis für ``index`` schätzen. None = Modell nicht anwendbar.

        ``anchor`` ist die bekannte Preisreihe (veröffentlichte Slots); daraus
        werden Niveau und Spanne des letzten vollständigen Tages gebildet.
        """
        if self.model is None or len(index) == 0:
            return None
        known = anchor.dropna()
        if known.empty:
            return None
        last_day = known[known.index.date == max(known.index.date)]
        if len(last_day) < 40:                  # halber Tag reicht nicht
            last_day = known
        x = build_features(index, weather, float(last_day.mean()),
                           float(last_day.max() - last_day.min()),
                           self._holidays)
        if x is None:
            return None
        try:
            values = self.model.predict(x)
        except Exception as exc:                            # pragma: no cover
            log.debug("Preismodell-Vorhersage fehlgeschlagen (%s).", exc)
            return None
        return pd.Series(values, index=index, dtype="float64")

    def status(self) -> dict:
        return {"active": self.model is not None, "reason": self.reason,
                "mae_model_ct": self.mae_model,
                "mae_baseline_ct": self.mae_baseline,
                "train_days": self.train_days,
                "trained_at": (None if self.trained_at is None
                               else str(self.trained_at))}
