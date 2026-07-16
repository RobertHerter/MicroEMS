"""Kalibrierung: gleicht historische Ist-Werte mit den Vorhersagen ab und
erzeugt Korrekturfaktoren.

Auf dem Pi ausführen (mit DB-Zugang):
    python -m kalibrierung --config config.yaml --lookback-days 365 --test-days 60

Vergleicht:
  * PV-Vorhersage (Solcast, pv_forecast) vs. Ist-Erzeugung (pv_generation)
  * Hausverbrauchs-Modell (Ähnliche-Tage) vs. Ist-Verbrauch
    – out-of-sample (Modell aus Trainingszeitraum, geprüft auf Testzeitraum)

Datenquellen folgen derselben Weiche wie der Live-Betrieb (standalone-fähig):
  * Verbrauch: tiefe lokale RSCP-house_load (bei history_source), sonst InfluxDB.
  * Temperatur: Open-Meteo (bei weather.enabled), sonst InfluxDB.
  * PV-Prognose: kombinierte Solcast-Quellen + influx_hist (bei solcast.enabled),
    sonst InfluxDB.
  * Ist-PV: tiefe lokale actuals.pv_w (nach pv_actual_import.py), sonst InfluxDB.
So wird gegen exakt die Daten kalibriert, mit denen das Live-Modell rechnet.

Ausgabe:
  * Konsolen-Report (MAPE, Bias, Korrektur global/stündlich/monatlich)
  * kalibrierung.yaml mit empfohlenen Werten:
      influxdb.signals.pv_forecast.scale   (global)
      forecast.correction_factor           (global, Verbrauch)
    sowie stündliche/monatliche Faktoren zur Ansicht.
Es wird NICHTS in die Datenbank geschrieben.
"""
from __future__ import annotations

import argparse
from datetime import timedelta

import numpy as np
import pandas as pd
import yaml

from ems.config import load_config
from ems.forecast import LoadForecaster
from ems.influx import InfluxRepository


def _metrics(actual: np.ndarray, pred: np.ndarray) -> dict:
    mask = np.isfinite(actual) & np.isfinite(pred)
    a, p = actual[mask], pred[mask]
    if len(a) == 0:
        return {"n": 0}
    bias = float(np.mean(p - a))
    denom = np.where(np.abs(a) < 1e-6, np.nan, a)
    mape = float(np.nanmean(np.abs((p - a) / denom)) * 100)
    rmse = float(np.sqrt(np.mean((p - a) ** 2)))
    # nMAE = mittlerer Absolutfehler bezogen auf den Mittelwert (robuster als MAPE,
    # das bei Werten nahe 0 – z.B. PV nachts – explodiert).
    mean_a = float(np.mean(a))
    nmae = float(np.mean(np.abs(p - a)) / mean_a * 100) if mean_a > 1e-6 else float("nan")
    scale = float(a.sum() / p.sum()) if p.sum() > 0 else float("nan")
    corr = float(np.corrcoef(a, p)[0, 1]) if len(a) > 2 else float("nan")
    return {"n": int(len(a)), "bias_W": bias, "mape_pct": mape, "nmae_pct": nmae,
            "rmse_W": rmse, "scale_actual_over_pred": scale, "corr": corr}


def _factor_table(actual: pd.Series, pred: pd.Series, key) -> dict:
    """Korrekturfaktor sum(actual)/sum(pred) je Gruppe (Stunde/Monat)."""
    df = pd.DataFrame({"a": actual, "p": pred}).dropna()
    if df.empty:
        return {}
    g = df.groupby(key(df.index))
    out = {}
    for k, sub in g:
        if sub["p"].sum() > 1e-6:
            out[int(k)] = round(float(sub["a"].sum() / sub["p"].sum()), 3)
    return out


def _month_hour_table(actual: pd.Series, pred: pd.Series, tz: str) -> dict:
    """Korrekturfaktor sum(actual)/sum(pred) je (Monat, Stunde)."""
    df = pd.DataFrame({"a": actual, "p": pred}).dropna()
    if df.empty:
        return {}
    loc = df.index.tz_convert(tz)
    df["m"], df["h"] = loc.month, loc.hour
    out: dict = {}
    for (m, h), sub in df.groupby(["m", "h"]):
        if sub["p"].sum() > 1e-6:
            out.setdefault(int(m), {})[int(h)] = round(float(sub["a"].sum() / sub["p"].sum()), 3)
    return out


def _pv_forecast_hist(cfg, repo, start, now):
    """PV-Prognose wie im Live-Betrieb: kombinierte Solcast-Quellen (lokal) bei
    solcast.enabled, sonst InfluxDB. (str-Quelle mit zurückgeben.)"""
    if cfg.solcast.enabled:
        from ems import solcast
        return (solcast.read_pv_signal(cfg, repo, "pv_forecast", start, now),
                "lokal (Solcast Ost+West / influx_hist)")
    return repo.read_slots("pv_forecast", start, now), "InfluxDB (pv_forecast)"


def _pv_actual_hist(cfg, repo, start, now):
    """Ist-PV: tiefe lokale actuals.pv_w (nach pv_actual_import), sonst InfluxDB."""
    from ems.local_history import read_actual
    s = read_actual(cfg.e3dc_rscp.history_db_path, "pv_w", start, now,
                    cfg.general.timezone)
    if not s.empty:
        return s, "lokal (E3DC actuals.pv_w)"
    if repo.signal_available("pv_generation"):
        return repo.read_slots("pv_generation", start, now), "InfluxDB (pv_generation)"
    return pd.Series(dtype="float64"), "—"


def calibrate_pv(repo, cfg, now, lookback_days):
    start = now - timedelta(days=lookback_days)
    fcast, fsrc = _pv_forecast_hist(cfg, repo, start, now)
    actual, asrc = _pv_actual_hist(cfg, repo, start, now)
    if fcast is None or fcast.empty or actual.empty:
        return None
    idx = actual.index.intersection(fcast.index)
    a, p = actual.reindex(idx), fcast.reindex(idx)
    # nur Tagstunden (Vorhersage > 50 W) bewerten
    day = p > 50.0
    a_d, p_d = a[day], p[day]
    m = _metrics(a_d.values, p_d.values)
    hourly = _factor_table(a_d, p_d, lambda i: i.tz_convert(cfg.general.timezone).hour)
    monthly = _factor_table(a_d, p_d, lambda i: i.tz_convert(cfg.general.timezone).month)
    month_hour = _month_hour_table(a_d, p_d, cfg.general.timezone)
    sig = cfg.influxdb.signals.get("pv_forecast")
    cur_scale = sig.scale if sig else 1.0
    return {"metrics": m, "hourly": hourly, "monthly": monthly, "month_hour": month_hour,
            "current_scale": cur_scale, "forecast_source": fsrc, "actual_source": asrc,
            "suggested_scale": round(cur_scale * m.get("scale_actual_over_pred", 1.0), 4)}


def _load_hist(repo, cfg, start, now):
    """Verbrauchs-Historie über dieselbe Weiche wie der Live-Forecaster:
    lokale RSCP-house_load bei history_source, sonst InfluxDB-house_consumption."""
    if cfg.e3dc_rscp.history_source:
        from ems.local_history import read_house_load
        return read_house_load(cfg.e3dc_rscp.history_db_path, start, now,
                               cfg.general.timezone)
    return repo.read_slots("house_consumption", start, now)


def _temp_hist(repo, cfg, start, now):
    """Temperatur wie im Live-Betrieb: Open-Meteo-Cache bei weather.enabled,
    sonst InfluxDB. None, wenn nichts da."""
    if cfg.weather.enabled:
        from ems.local_history import read_temperature
        try:
            t = read_temperature(cfg.e3dc_rscp.history_db_path, start, now,
                                  cfg.general.timezone, f"{cfg.general.slot_minutes}min")
            return t if not t.empty else None
        except Exception:
            return None
    if repo.signal_available("temperature"):
        try:
            return repo.read_slots("temperature", start, now)
        except Exception:
            return None
    return None


def _mae(a: np.ndarray, p: np.ndarray) -> float:
    m = np.isfinite(a) & np.isfinite(p)
    return float(np.mean(np.abs(p[m] - a[m]))) if m.any() else float("nan")


def _wape(a: np.ndarray, p: np.ndarray) -> float:
    """WAPE (%): Summe |Fehler| / Summe Ist - robust gegen Werte nahe 0
    (im Gegensatz zu MAPE) und direkt als 'Anteil verfehlter Energie' lesbar."""
    m = np.isfinite(a) & np.isfinite(p)
    a, p = a[m], p[m]
    if len(a) == 0 or np.abs(a).sum() < 1e-9:
        return float("nan")
    return float(np.abs(p - a).sum() / np.abs(a).sum() * 100)


_DAYPARTS = [(0, 6, "Nacht 00-06"), (6, 12, "Morgen 06-12"),
             (12, 18, "Mittag 12-18"), (18, 24, "Abend 18-24")]
_SEASONS = {12: "Winter", 1: "Winter", 2: "Winter",
            3: "Fruehling", 4: "Fruehling", 5: "Fruehling",
            6: "Sommer", 7: "Sommer", 8: "Sommer",
            9: "Herbst", 10: "Herbst", 11: "Herbst"}


def _segment_metrics(df: pd.DataFrame, tz: str) -> dict:
    """MAE/WAPE gesamt + je Tageszeit, Werktag/WE und Saison."""
    a, p = df["a"].values, df["p"].values
    loc = df.index.tz_convert(tz)
    out = {"gesamt": {"n": int(len(df)), "mae_W": round(_mae(a, p), 1),
                      "wape_pct": round(_wape(a, p), 2)}}
    for lo, hi, name in _DAYPARTS:
        m = (loc.hour >= lo) & (loc.hour < hi)
        if m.any():
            out[name] = {"mae_W": round(_mae(a[m], p[m]), 1),
                         "wape_pct": round(_wape(a[m], p[m]), 2)}
    for name, m in (("Werktag", loc.weekday < 5), ("Wochenende", loc.weekday >= 5)):
        if m.any():
            out[name] = {"mae_W": round(_mae(a[m], p[m]), 1),
                         "wape_pct": round(_wape(a[m], p[m]), 2)}
    seasons = pd.Series([_SEASONS[mth] for mth in loc.month], index=df.index)
    for name in ("Winter", "Fruehling", "Sommer", "Herbst"):
        m = (seasons == name).values
        if m.any():
            out[name] = {"mae_W": round(_mae(a[m], p[m]), 1),
                         "wape_pct": round(_wape(a[m], p[m]), 2)}
    return out


def _issue_time_archive_reader(cfg):
    """Liefert die am historischen Origin zuletzt bekannten Zukunftsreihen.

    Ein Fold gilt erst ab 90 % Abdeckung für alle aktivierten lokalen
    Prognosequellen als kompatibel mit dem Live-Modell. Dadurch wird während
    der Anlaufphase automatisch weiter der leakage-freie Fallback verwendet.
    """
    from ems.local_history import (read_pv_forecast_asof,
                                   read_weather_forecast_asof)

    db = cfg.e3dc_rscp.history_db_path
    tz = cfg.general.timezone
    slot_minutes = cfg.general.slot_minutes
    freq = f"{slot_minutes}min"
    use_weather = bool(cfg.weather.enabled)
    use_pv = bool(cfg.solcast.enabled)

    def read(origin, end):
        expected = max(1, int(round(
            (pd.Timestamp(end) - pd.Timestamp(origin)).total_seconds()
            / (slot_minutes * 60))))
        futures = {"temp": None, "pv": None, "complete": False}
        checks = []
        if use_weather:
            futures["temp"] = read_weather_forecast_asof(
                db, origin, origin, end, tz, freq, "temp")
            checks.append(futures["temp"].notna().sum() >= 0.9 * expected)
        if use_pv:
            futures["pv"] = read_pv_forecast_asof(
                db, origin, origin, end, tz, slot_minutes,
                cfg.solcast.combine, "pv")
            checks.append(futures["pv"].notna().sum() >= 0.9 * expected)
        futures["complete"] = bool(checks and all(checks))
        return futures

    return read


def validate_forecast_series(cfg, hist: pd.Series, temp, pv, now,
                             folds: int = 12, horizon_hours: int = 48,
                             min_train_days: int = 60,
                             archive_reader=None):
    """Rolling-Origin-Backtest der Verbrauchsprognose, wie der Live-Betrieb
    sie nutzt: `folds` Startpunkte gleichmäßig über die Historie verteilt
    (Saison-Abdeckung), je Fold `horizon_hours` voraus mit ALLEM Wissen bis
    zum Startpunkt - einmal je Methode (ml UND similar_days, dieselben
    Folds). Korrekturen (correction_factor/Stundenprofil) sind neutralisiert:
    gemessen wird das rohe Modell, nicht die Kalibrierung.

    Rückgabe: {"folds", "horizon_hours", "methods": {m: Segment-Metriken},
    "empfehlung": Methode mit kleinerem Gesamt-WAPE} oder None (zu wenig
    Daten). Der einfache Hold-out in calibrate_load bleibt daneben bestehen
    (er liefert die Korrekturfaktoren); diese Validierung beantwortet die
    Modellwahl-Frage."""
    tz = cfg.general.timezone
    horizon_slots = int(round(
        horizon_hours * 60.0 / cfg.general.slot_minutes))
    hist = hist.dropna()
    if hist.empty:
        return None
    methods = ["similar_days"]
    try:
        import sklearn  # noqa: F401
        methods.insert(0, "ml")
    except ImportError:
        print("  [Hinweis] scikit-learn fehlt - nur similar_days validiert.")
    freq = pd.Timedelta(minutes=cfg.general.slot_minutes)
    horizon = pd.Timedelta(hours=horizon_hours)
    # Letzter Origin muss den VOLLEN frei wählbaren Horizont vor `now` haben.
    # horizon_hours//24 schnitt zuvor z.B. einen 36-h-Fold auf 24 h Ist ab.
    end = (pd.Timestamp(now).tz_convert(tz) - horizon).floor(freq)
    first = (hist.index.min().tz_convert(tz)
             + pd.Timedelta(days=min_train_days)).ceil(freq)
    usable_slots = int((end - first) / freq) if end >= first else 0
    folds = max(1, min(folds, usable_slots + 1)) if usable_slots >= 0 and end >= first else 0
    if folds < 1 or end < first:
        return None
    if folds == 1:
        origins = [end]
    else:
        step = (end - first) / (folds - 1)
        origins = [(first + k * step).floor(freq) for k in range(folds)]
        # Rundung aufs Slotraster darf keine doppelten Folds erzeugen.
        origins = list(dict.fromkeys(origins))

    orig_method = cfg.forecast.method
    orig_corr = cfg.forecast.correction_factor
    collected = {m: [] for m in methods}
    used = 0
    archived_folds = 0
    min_train_slots = int(np.ceil(
        min_train_days * 24 * cfg.general.slots_per_hour * 0.9))
    min_actual_slots = int(np.ceil(horizon_slots * 0.9))
    try:
        cfg.forecast.correction_factor = 1.0
        for origin in origins:
            train = hist[hist.index < origin]
            actual = hist[(hist.index >= origin)
                          & (hist.index < origin
                             + pd.Timedelta(hours=horizon_hours))]
            if len(train) < min_train_slots or len(actual) < min_actual_slots:
                continue
            used += 1
            archived = (archive_reader(origin, origin + horizon)
                        if archive_reader is not None else None)
            archive_complete = bool(archived and archived.get("complete"))
            if archive_complete:
                archived_folds += 1
                hist_temp = (temp[temp.index < origin]
                             if temp is not None else None)
                hist_pv = (pv[pv.index < origin] if pv is not None else None)
                fut_temp = archived.get("temp")
                fut_pv = archived.get("pv")
            else:
                hist_temp = fut_temp = hist_pv = fut_pv = None
            for method in methods:
                cfg.forecast.method = method
                fc = LoadForecaster(cfg)
                fc.load_hourly = None
                pred = fc.forecast(train, origin, horizon_slots,
                                   hist_temp=hist_temp, fut_temp=fut_temp,
                                   hist_pv=hist_pv, fut_pv=fut_pv)
                idx = actual.index.intersection(pred.index)
                if len(idx):
                    collected[method].append(pd.DataFrame(
                        {"a": actual.reindex(idx).values,
                         "p": pred.reindex(idx).values}, index=idx))
    finally:
        cfg.forecast.method = orig_method
        cfg.forecast.correction_factor = orig_corr

    compatible = bool(used and archived_folds == used)
    mode = ("issue_time_archive" if compatible else
            "partial_issue_time_archive" if archived_folds else
            "disabled_no_issue_time_archive")
    res = {"folds": used, "horizon_hours": horizon_hours,
           "archive_folds": archived_folds, "exogenous_mode": mode,
           "correction_profile_compatible": compatible, "methods": {}}
    combined = {}
    for method, frames in collected.items():
        if frames:
            combined[method] = pd.concat(frames)
            res["methods"][method] = _segment_metrics(combined[method], tz)
    if not res["methods"]:
        return None
    res["empfehlung"] = min(
        combined, key=lambda m: _wape(combined[m]["a"].values,
                                      combined[m]["p"].values))
    best = combined[res["empfehlung"]]
    # Produktive Korrektur ebenfalls out-of-fold bestimmen. Der alte einzelne
    # 60-Tage-Horizont verlor beim ML nach Tag 7 sein lag_7d-Feature und konnte
    # die Stundenfaktoren dadurch systematisch aufblasen.
    res["global_correction"] = round(
        float(best["a"].sum() / best["p"].sum()), 4)
    res["hourly_correction"] = _factor_table(
        best["a"], best["p"],
        lambda i: i.tz_convert(cfg.general.timezone).hour)
    return res


def _print_validation(res):
    print("\n" + "=" * 62 + "\nPrognose-Validierung (Rolling-Origin, "
          f"{res['folds']} Folds x {res['horizon_hours']} h, roh ohne "
          "Korrekturen)\n" + "-" * 62)
    methods = list(res["methods"])
    head = "  {:<14}".format("") + "".join(f"{m:>22}" for m in methods)
    print(head)
    segs = []
    for m in methods:                       # Reihenfolge des ersten Vorkommens
        for s in res["methods"][m]:
            if s not in segs:
                segs.append(s)
    for s in segs:
        cells = []
        for m in methods:
            v = res["methods"][m].get(s)
            cells.append(f"{v['wape_pct']:6.1f}% {v['mae_W']:7.0f}W"
                         if v else " " * 15)
        print(f"  {s:<14}" + "".join(f"{c:>22}" for c in cells))
    print(f"  -> Empfehlung forecast.method: '{res['empfehlung']}' "
          f"(kleinster Gesamt-WAPE)")
    print(f"  -> Rolling-Origin-Korrekturfaktor: "
          f"{res.get('global_correction', 1.0):.3f}")
    if not res.get("correction_profile_compatible", False):
        print("     (nur Diagnose; nicht live angewandt, da Origin-Archive für "
              "Wetter/PV fehlen)")


def calibrate_load(repo, cfg, now, lookback_days, test_days):
    use_local = cfg.e3dc_rscp.history_source
    if not use_local and not repo.signal_available("house_consumption"):
        return None
    start = now - timedelta(days=lookback_days)
    hist = _load_hist(repo, cfg, start, now).dropna()
    if hist.empty:
        return None
    if len(hist) < 96 * (test_days + 14):
        print("  [Hinweis] Wenig Historie – Ergebnis nur eingeschränkt aussagekräftig.")
    test_start = now - timedelta(days=test_days)
    train = hist[hist.index < test_start]
    actual_test = hist[hist.index >= test_start]
    if train.empty or actual_test.empty:
        return None
    # Temperatur (falls vorhanden) in den Backtest einbeziehen -> prüft das
    # temperaturgewichtete Modell, konsistent zum Live-Betrieb (gleiche Quelle).
    temp = _temp_hist(repo, cfg, start, now)
    pv, _ = _pv_forecast_hist(cfg, repo, start, now)

    # Modell aus Trainingszeitraum, ohne bestehende Korrektur, out-of-sample
    # prüfen - auch das Stunden-Profil neutralisieren, sonst würde ein
    # früherer Kalibrierlauf in die eigene Messung einfließen (Zirkelschluss).
    cfg.forecast.correction_factor = 1.0
    fc = LoadForecaster(cfg)
    fc.load_hourly = None
    horizon = len(actual_test)
    pred = fc.forecast(train, test_start, horizon, hist_temp=temp, fut_temp=temp, hist_pv=pv, fut_pv=pv)
    idx = actual_test.index.intersection(pred.index)
    a, p = actual_test.reindex(idx), pred.reindex(idx)
    m = _metrics(a.values, p.values)
    hourly = _factor_table(a, p, lambda i: i.tz_convert(cfg.general.timezone).hour)

    def daytype(i):
        loc = i.tz_convert(cfg.general.timezone)
        return (loc.weekday >= 5).astype(int)  # 0=Werktag,1=WE
    daytype_tab = _factor_table(a, p, daytype)

    # Fehler nach Temperatur-Bereich (zeigt, ob die Temperatur-Gewichtung greift)
    temp_bins = {}
    if temp is not None:
        tt = pd.Series(temp).reindex(idx)
        df = pd.DataFrame({"a": a.values, "p": p.values, "t": tt.values}).dropna()
        if not df.empty:
            bins = pd.cut(df["t"], bins=[-50, 0, 5, 10, 15, 20, 25, 50])
            for b, sub in df.groupby(bins, observed=True):
                if sub["p"].sum() > 1e-6:
                    temp_bins[str(b)] = round(float(sub["a"].sum() / sub["p"].sum()), 3)

    return {"metrics": m, "hourly": hourly, "daytype": daytype_tab,
            "temp_used": temp is not None, "by_temperature": temp_bins,
            "load_source": ("lokal (RSCP house_load)" if use_local
                            else "InfluxDB (house_consumption)"),
            "temp_source": ("Open-Meteo (lokal)" if (temp is not None and cfg.weather.enabled)
                            else "InfluxDB" if temp is not None else "—"),
            "suggested_correction_factor": round(m.get("scale_actual_over_pred", 1.0), 4)}


def _print_block(title, res):
    print("\n" + "=" * 62 + f"\n{title}\n" + "-" * 62)
    if res is None:
        print("  (Signal nicht verfügbar – übersprungen)")
        return
    m = res["metrics"]
    if m.get("n", 0) == 0:
        print("  (keine überlappenden Daten)")
        return
    print(f"  Datenpunkte: {m['n']}")
    print(f"  nMAE:        {m.get('nmae_pct', float('nan')):6.1f} %  (Fehler bezogen auf Mittelwert)")
    print(f"  MAPE:        {m['mape_pct']:6.1f} %  (bei Werten nahe 0 – z.B. PV nachts – überzeichnet)")
    print(f"  Bias:        {m['bias_W']:+8.0f} W (Vorhersage - Ist)")
    print(f"  RMSE:        {m['rmse_W']:8.0f} W")
    print(f"  Korrelation: {m['corr']:6.2f}")
    print(f"  Global-Faktor Ist/Vorhersage: {m['scale_actual_over_pred']:.3f}")


def main():
    ap = argparse.ArgumentParser(description="EMS Vorhersage-Kalibrierung")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--lookback-days", type=int, default=365)
    ap.add_argument("--test-days", type=int, default=60)
    ap.add_argument("--output", default="kalibrierung.yaml")
    ap.add_argument("--val-folds", type=int, default=12,
                    help="Rolling-Origin-Folds der Prognose-Validierung "
                         "(0 = überspringen)")
    ap.add_argument("--val-horizon-h", type=int, default=48,
                    help="Prognosehorizont je Fold in Stunden (wie live)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    repo = InfluxRepository(cfg)
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(f"{cfg.general.slot_minutes}min")
    validation = None
    try:
        print(f"Kalibrierung über {args.lookback_days} Tage (Test: letzte {args.test_days} Tage) ...")
        pv = calibrate_pv(repo, cfg, now, args.lookback_days)
        load = calibrate_load(repo, cfg, now, args.lookback_days, args.test_days)
        if args.val_folds > 0:
            print(f"Prognose-Validierung ({args.val_folds} Folds x "
                  f"{args.val_horizon_h} h, ml vs. similar_days) ...")
            start = now - timedelta(days=args.lookback_days)
            v_hist = _load_hist(repo, cfg, start, now).dropna()
            v_temp = _temp_hist(repo, cfg, start, now)
            v_pv, _ = _pv_forecast_hist(cfg, repo, start, now)
            validation = validate_forecast_series(
                cfg, v_hist, v_temp, v_pv, now,
                folds=args.val_folds, horizon_hours=args.val_horizon_h,
                archive_reader=_issue_time_archive_reader(cfg))
    finally:
        repo.close()

    _print_block("PV-Vorhersage (Solcast) vs. Ist-Erzeugung", pv)
    if pv:
        print(f"     Quelle Prognose: {pv.get('forecast_source', '?')} | "
              f"Ist: {pv.get('actual_source', '?')}")
        print(f"  -> Empfehlung  pv_forecast.scale = {pv['suggested_scale']}  "
              f"(aktuell {pv['current_scale']})")
        print(f"     stündliche Faktoren: {pv['hourly']}")
        print(f"     monatliche Faktoren: {pv['monthly']}")
    _print_block("Hausverbrauch (Modell) vs. Ist-Verbrauch (out-of-sample)", load)
    if load:
        print(f"  -> Empfehlung  forecast.correction_factor = {load['suggested_correction_factor']}")
        print(f"     Quelle Verbrauch: {load.get('load_source', '?')} | "
              f"Temperatur: {load.get('temp_source', '?')}")
        print(f"     Temperatur im Modell genutzt: {load.get('temp_used', False)}")
        print(f"     stündliche Faktoren: {load['hourly']}")
        print(f"     Werktag/Wochenende:  {load['daytype']}")
        if load.get("by_temperature"):
            print(f"     Faktor nach Temperaturbereich: {load['by_temperature']}")
    if validation:
        _print_validation(validation)

    out = {
        "generated": now.isoformat(),
        "lookback_days": args.lookback_days,
        "test_days": args.test_days,
        "pv_forecast": pv,
        "load_forecast": load,
        "forecast_validation": validation,
        "empfohlene_config": {
            "influxdb.signals.pv_forecast.scale": pv["suggested_scale"] if pv else None,
            "forecast.correction_factor": (
                validation.get("global_correction")
                if validation and validation.get("correction_profile_compatible")
                else load["suggested_correction_factor"] if load else None),
        },
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=False)
    print(f"\nDetails in {args.output} geschrieben.")

    # Anwendbares Profil (zeitabhängige Korrekturen) schreiben. Wird von der
    # Pipeline genutzt, wenn calibration.enabled=true (config).
    if pv or load:
        profile = {"generated": now.isoformat()}
        if pv:
            profile.update({
                "pv_global": round(pv["metrics"].get("scale_actual_over_pred", 1.0), 4),
                "pv_month_hour": pv.get("month_hour", {}),
                "pv_hour": pv.get("hourly", {}),
                "pv_month": pv.get("monthly", {}),
            })
        # NUR das archiv-kompatible Rolling-Profil darf produktiv werden. Der
        # Hold-out ist als Quelle GESPERRT: sein 365-Tage-Horizont verliert
        # beim ML nach Tag 7 das lag_7d-Feature und bläst die Faktoren
        # systematisch auf (real gemessen: Hold-out-Stundenfaktoren im Mittel
        # x1,44 / nachts bis x1,66, während die Rolling-Diagnose global x1,06
        # ergab - eine produktive 40-%-Überkorrektur). Ohne kompatibles
        # Rolling-Profil bleibt load_hourly leer -> die Pipeline nutzt den
        # globalen forecast.correction_factor, bis das issue_time-Archiv die
        # ehrliche Stunden-Korrektur freischaltet.
        rolling_hourly = ((validation or {}).get("hourly_correction")
                          if (validation or {}).get("correction_profile_compatible")
                          else None)
        if rolling_hourly:
            # Geclippt auf [0.6, 1.8] gegen Ausreißer-Stunden.
            profile["load_hourly"] = {
                int(h): round(float(min(1.8, max(0.6, f))), 3)
                for h, f in rolling_hourly.items()}
        with open("kalibrierung_profil.yaml", "w", encoding="utf-8") as fh:
            yaml.safe_dump(profile, fh, allow_unicode=True, sort_keys=True)
        print("Korrekturprofil (PV Monat x Stunde, Last je Stunde) -> "
              "kalibrierung_profil.yaml")

    print("\nAnwenden:")
    print("  Zeitabhängig (empfohlen): calibration.enabled=true + pv_profile=")
    print("  ./kalibrierung_profil.yaml in config.yaml - PV (Monat x Stunde)")
    print("  und Last (je Stunde) werden dann automatisch angewandt;")
    print("  forecast.correction_factor wird bei vorhandenem Last-Profil")
    print("  ignoriert (kein Doppel-Faktor). Danach Dienst neu starten.")


if __name__ == "__main__":
    main()
