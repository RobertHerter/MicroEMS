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
    """PV-Prognose wie im Live-Betrieb: kombinierte lokale Quellen (Solcast ODER
    freies pvlib-Modell) bei aktiver Quelle, sonst InfluxDB. (str-Quelle mit
    zurückgeben.) So wird die aktive Prognose gegen die realen Ertragsdaten
    (actuals.pv_w) kalibriert - egal ob Solcast oder pvlib."""
    from ems import solcast, pvforecast
    if cfg.solcast.enabled:
        return (solcast.read_pv_signal(cfg, repo, "pv_forecast", start, now),
                "lokal (Solcast-Quellen / influx_hist)")
    if pvforecast.enabled(cfg):
        return (solcast.read_pv_signal(cfg, repo, "pv_forecast", start, now),
                "lokal (pvlib-Modell / Open-Meteo)")
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
    from ems import pvforecast
    use_weather = bool(cfg.weather.enabled)
    use_pv = bool(cfg.solcast.enabled) or pvforecast.enabled(cfg)

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
    sie nutzt zwei leakage-freie Ebenen: `folds` Startpunkte gleichmäßig über
    die Historie verteilt als konservativen Bootstrap ohne Zukunfts-Wetter/PV
    sowie bis zu 12 wöchentliche Origins mit echten Issue-Time-Snapshots.
    Letztere ersetzen den Bootstrap schrittweise. Korrekturen
    (correction_factor/Stundenprofil) sind neutralisiert: gemessen wird das
    rohe Modell, nicht die Kalibrierung.

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
    archived = {m: [] for m in methods}
    used = 0
    archived_folds = 0
    min_train_slots = int(np.ceil(
        min_train_days * 24 * cfg.general.slots_per_hour * 0.9))
    min_actual_slots = int(np.ceil(horizon_slots * 0.9))

    def evaluate(origin, futures=None, target=None):
        """Einen Origin leakage-frei auswerten.

        Historische Bootstrap-Folds laufen bewusst ohne Zukunfts-Wetter/PV.
        Nur echte Issue-Time-Snapshots dürfen die produktionsnahe Sammlung
        speisen.
        """
        train = hist[hist.index < origin]
        actual = hist[(hist.index >= origin)
                      & (hist.index < origin + horizon)]
        if len(train) < min_train_slots or len(actual) < min_actual_slots:
            return False
        hist_temp = hist_pv = fut_temp = fut_pv = None
        if futures is not None:
            hist_temp = temp[temp.index < origin] if temp is not None else None
            hist_pv = pv[pv.index < origin] if pv is not None else None
            fut_temp, fut_pv = futures.get("temp"), futures.get("pv")
        bucket = collected if target is None else target
        for method in methods:
            cfg.forecast.method = method
            fc = LoadForecaster(cfg)
            fc.load_hourly = None
            pred = fc.forecast(
                train, origin, horizon_slots,
                hist_temp=hist_temp, fut_temp=fut_temp,
                hist_pv=hist_pv, fut_pv=fut_pv)
            idx = actual.index.intersection(pred.index)
            if len(idx):
                bucket[method].append(pd.DataFrame(
                    {"a": actual.reindex(idx).values,
                     "p": pred.reindex(idx).values}, index=idx))
        return True

    try:
        cfg.forecast.correction_factor = 1.0
        # 1) Sofort nutzbarer Langzeit-Bootstrap: saisonal verteilte Origins,
        # aber ohne rückblickend bekannte Zukunfts-Wetter-/PV-Werte.
        for origin in origins:
            used += int(evaluate(origin))

        # 2) Echte Produktions-Folds ausschließlich innerhalb des vorhandenen
        # Archivs: wöchentlich rückwärts ab dem jüngsten vollständigen Horizont.
        # So wächst die Evidenz jede Woche, statt erst nach 670 Tagen alle über
        # die Langzeithistorie verteilten Origins abzudecken.
        if archive_reader is not None:
            for k in range(min(12, len(origins))):
                origin = (end - pd.Timedelta(days=7 * k)).floor(freq)
                if origin < first:
                    break
                futures = archive_reader(origin, origin + horizon)
                if not (futures and futures.get("complete")):
                    continue
                if evaluate(origin, futures=futures, target=archived):
                    archived_folds += 1
    finally:
        cfg.forecast.method = orig_method
        cfg.forecast.correction_factor = orig_corr

    min_archive_folds = min(6, max(1, len(origins)))
    compatible = archived_folds >= min_archive_folds
    archive_weight = min(1.0, archived_folds / float(min_archive_folds))
    mode = ("issue_time_archive" if compatible else
            "hybrid_issue_time_archive" if archived_folds else
            "historical_bootstrap")
    res = {"folds": used, "horizon_hours": horizon_hours,
           "archive_folds": archived_folds, "exogenous_mode": mode,
           "archive_min_folds": min_archive_folds,
           "archive_weight": round(archive_weight, 3),
           "correction_profile_compatible": compatible, "methods": {},
           "archive_methods": {}}
    combined = {}
    for method, frames in collected.items():
        if frames:
            combined[method] = pd.concat(frames)
            res["methods"][method] = _segment_metrics(combined[method], tz)
    if not res["methods"]:
        return None
    bootstrap_method = min(
        combined, key=lambda m: _wape(combined[m]["a"].values,
                                      combined[m]["p"].values))
    archive_combined = {}
    for method, frames in archived.items():
        if frames:
            archive_combined[method] = pd.concat(frames)
            res["archive_methods"][method] = _segment_metrics(
                archive_combined[method], tz)
    archive_method = (min(
        archive_combined,
        key=lambda m: _wape(archive_combined[m]["a"].values,
                            archive_combined[m]["p"].values))
        if archive_combined else None)
    res["bootstrap_empfehlung"] = bootstrap_method
    res["archive_empfehlung"] = archive_method
    res["empfehlung"] = archive_method if compatible else bootstrap_method

    bootstrap_best = combined[bootstrap_method]
    bootstrap_global = float(
        bootstrap_best["a"].sum() / bootstrap_best["p"].sum())
    bootstrap_hourly = _factor_table(
        bootstrap_best["a"], bootstrap_best["p"],
        lambda i: i.tz_convert(cfg.general.timezone).hour)
    archive_global = bootstrap_global
    archive_hourly = {}
    if archive_method is not None:
        archive_best = archive_combined[archive_method]
        archive_global = float(
            archive_best["a"].sum() / archive_best["p"].sum())
        archive_hourly = _factor_table(
            archive_best["a"], archive_best["p"],
            lambda i: i.tz_convert(cfg.general.timezone).hour)

    # Startprofil konservativ zur globalen Korrektur schrumpfen. Echte
    # Archivdaten ersetzen es linear bis zum sechsten vollständigen Wochenfold.
    productive_hourly = {}
    for hour in range(24):
        boot_raw = float(bootstrap_hourly.get(hour, bootstrap_global))
        boot = bootstrap_global + 0.5 * (boot_raw - bootstrap_global)
        real = float(archive_hourly.get(hour, archive_global))
        factor = (1.0 - archive_weight) * boot + archive_weight * real
        productive_hourly[hour] = round(min(1.8, max(0.6, factor)), 3)
    productive_global = ((1.0 - archive_weight) * bootstrap_global
                         + archive_weight * archive_global)
    res["bootstrap_global_correction"] = round(bootstrap_global, 4)
    res["archive_global_correction"] = (
        round(archive_global, 4) if archive_method is not None else None)
    res["global_correction"] = round(productive_global, 4)
    res["bootstrap_hourly_correction"] = bootstrap_hourly
    res["archive_hourly_correction"] = archive_hourly
    res["hourly_correction"] = productive_hourly
    res["correction_profile_source"] = (
        "issue_time_archive" if compatible else
        "hybrid" if archived_folds else "historical_bootstrap")
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
    print(f"  -> Archiv-Folds: {res.get('archive_folds', 0)}/"
          f"{res.get('archive_min_folds', 0)}, Anteil echte Archive "
          f"{100 * res.get('archive_weight', 0.0):.0f} %")
    print(f"  -> Produktives Korrekturprofil: "
          f"{res.get('correction_profile_source', 'unbekannt')}")


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
    ap.add_argument("--band-lookback-days", type=int, default=60,
                    help="Fenster für die pvlib-p10/p90-Bandkalibrierung")
    ap.add_argument("--no-band", action="store_true",
                    help="pvlib-p10/p90-Band NICHT automatisch anpassen")
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

    # --- pvlib-Unsicherheitsband (p10/p90) aus echten Residuen --------------
    # Ersetzt den heuristischen Festwert. Die empfohlenen Werte werden GEDÄMPFT
    # (50 %) ins Overlay (config_overrides.yaml) geschrieben, damit ein einzelner
    # Wochenausschlag das Band nicht überschießt; config.yaml bleibt unberührt.
    band = None
    if cfg.pv_model.arrays and not args.no_band:
        try:
            from ems import pv_eval
            from ems.config import save_override
            band = pv_eval.calibrate_band(cfg, lookback_days=args.band_lookback_days)
        except Exception as exc:
            print(f"\nPV-Bandkalibrierung fehlgeschlagen: {exc}")
    band_applied = {}
    if band and not band.get("insufficient"):
        print(f"\nPV-Band (pvlib) aus {band['n']} Residuen ({band['method']}):")
        print(f"     Abdeckung aktuell: {band['current_below_p10_pct']} % unter p10 "
              f"(Ziel {band['target_low_pct']:.0f} %), "
              f"{band['current_above_p90_pct']} % über p90.")
        for key, rkey in (("p10_uncertainty", "recommended_p10_uncertainty"),
                          ("p90_uncertainty", "recommended_p90_uncertainty")):
            old = float(getattr(cfg.pv_model, key))
            new = round(old + 0.5 * (band[rkey] - old), 3)      # 50 % gedämpft
            save_override(args.config, f"pv_model.{key}", new)
            band_applied[key] = new
            print(f"  -> pv_model.{key}: {old} -> {new} (Ziel {band[rkey]}, gedämpft)")
    elif band:
        print(f"\nPV-Band: zu wenig Residuen (n={band['n']}) – Festwert bleibt.")

    out = {
        "generated": now.isoformat(),
        "lookback_days": args.lookback_days,
        "test_days": args.test_days,
        "pv_forecast": pv,
        "load_forecast": load,
        "forecast_validation": validation,
        "pv_band": band,
        "empfohlene_config": {
            "influxdb.signals.pv_forecast.scale": pv["suggested_scale"] if pv else None,
            "forecast.correction_factor": (
                validation.get("global_correction")
                if validation
                else load["suggested_correction_factor"] if load else None),
            **{f"pv_model.{k}": v for k, v in band_applied.items()},
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
        # Hybridprofil: saisonal verteilte, leakage-freie Bootstrap-Folds
        # liefern sofort gedämpfte Startwerte. Echte wöchentliche Issue-Time-
        # Folds ersetzen sie bis zum sechsten Fold linear. Der alte einzelne
        # 365-Tage-Hold-out bleibt als Quelle gesperrt, weil das ML-Modell dort
        # nach Tag 7 sein lag_7d-Feature verliert und Faktoren aufblasen kann.
        rolling_hourly = (validation or {}).get("hourly_correction")
        if rolling_hourly:
            # Geclippt auf [0.6, 1.8] gegen Ausreißer-Stunden.
            profile["load_hourly"] = {
                int(h): round(float(min(1.8, max(0.6, f))), 3)
                for h, f in rolling_hourly.items()}
            profile["load_profile_source"] = validation.get(
                "correction_profile_source", "historical_bootstrap")
            profile["load_archive_folds"] = int(validation.get(
                "archive_folds", 0))
            profile["load_archive_min_folds"] = int(validation.get(
                "archive_min_folds", 6))
            profile["load_archive_weight"] = float(validation.get(
                "archive_weight", 0.0))
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
