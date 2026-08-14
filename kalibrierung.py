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
Jeder Lauf wird zusätzlich als unveränderlicher Kalibrierungsstand in der
lokalen History-Datenbank archiviert. Das beeinflusst weder Prognose noch
Steuerung, ermöglicht aber die Änderungshistorie im Dashboard.
"""
from __future__ import annotations

import argparse
from datetime import timedelta

import numpy as np
import pandas as pd
import yaml

from ems.config import load_config
from ems.quality import BOUNDS, bias_w, enough
from ems.forecast import LoadForecaster
from ems.influx import InfluxRepository
from ems import pv_eval


def _metrics(actual: np.ndarray, pred: np.ndarray) -> dict:
    mask = np.isfinite(actual) & np.isfinite(pred)
    a, p = actual[mask], pred[mask]
    if len(a) == 0:
        return {"n": 0}
    bias = bias_w(a, p)
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


def _cell_ok(sub: pd.DataFrame) -> bool:
    """Traegt diese Profilzelle genug Messwerte, um angewandt zu werden?

    Ohne Gate entstand ein voll wirksamer Faktor schon aus einem einzigen Slot.
    Beim Monatswechsel hiess das: der erste Tag des neuen Monats bestimmt dessen
    Korrektur - genau dort, wo das Profil saisonal umschwenken soll und die
    Datenlage am duennsten ist. Faellt eine Zelle weg, greift die Kette
    (Monat x Stunde -> Stunde -> Monat -> global) eine Ebene hoeher.
    """
    return sub["p"].sum() > 1e-6 and enough("profile_cell", len(sub))


def _factor_table(actual: pd.Series, pred: pd.Series, key) -> dict:
    """Korrekturfaktor sum(actual)/sum(pred) je Gruppe (Stunde/Monat)."""
    df = pd.DataFrame({"a": actual, "p": pred}).dropna()
    if df.empty:
        return {}
    g = df.groupby(key(df.index))
    out = {}
    for k, sub in g:
        if _cell_ok(sub):
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
        if _cell_ok(sub):
            out.setdefault(int(m), {})[int(h)] = round(
                float(sub["a"].sum() / sub["p"].sum()), 3)
    return out


_PV_KEYS = ("pv_global", "pv_month_hour", "pv_hour", "pv_month")


def _pv_factors(res: dict) -> dict:
    """Korrekturfaktoren eines Kalibrierlaufs im Profilformat."""
    return {
        "pv_global": round(
            res.get("fit_metrics", {}).get("scale_actual_over_pred", 1.0), 4),
        "pv_month_hour": res.get("month_hour", {}),
        "pv_hour": res.get("hourly", {}),
        "pv_month": res.get("monthly", {}),
    }


def _pv_source_summary(res: dict, rolle: str, entscheid: dict) -> dict:
    """Kompakte Guete je Quellgruppe fuer den Bericht.

    Ohne das beantwortet kalibrierung.yaml nur "wie gut ist die produktive
    Quelle" - die Schattenquelle liesse sich nur ueber einen Handaufruf von
    pv_eval.compare_sources beurteilen, obwohl der Wochenlauf sie ohnehin
    vermisst hat.
    """
    fm = res.get("fit_metrics") or {}
    return {
        "rolle": rolle,
        "forecast_source": res.get("forecast_source"),
        "n": fm.get("n"),
        "nmae_pct": fm.get("nmae_pct"),
        "rmse_W": fm.get("rmse_W"),
        "bias_W": fm.get("bias_W"),
        "corr": fm.get("corr"),
        "scale_actual_over_pred": fm.get("scale_actual_over_pred"),
        "hourly": res.get("hourly"),
        "monthly": res.get("monthly"),
        "promotion": {k: entscheid.get(k)
                      for k in ("promote", "status", "reason", "n")},
    }


def pv_source_groups(cfg) -> dict:
    """Verfuegbare PV-Quellgruppen als {Name: [Archiv-IDs]}. Jede bekommt ein
    eigenes Korrekturprofil, sonst tritt im Quellenvergleich eine korrigierte
    gegen eine rohe Prognose an."""
    return pv_eval.source_groups(cfg)


def _pv_forecast_hist(cfg, repo, start, now, source=None):
    """PV-Prognose wie im Live-Betrieb. Ohne ``source`` die AKTIVE Quelle
    (Solcast ODER pvlib), sonst genau die benannte Gruppe - damit sich jede
    einzeln kalibrieren laesst."""
    from ems import pvforecast, solcast
    if source:
        ids = pv_source_groups(cfg).get(source) or []
        if not ids:
            return None, f"{source} (keine Quellen)"
        return (pv_eval.read_group_asof(
            cfg.e3dc_rscp.history_db_path, ids, start, now,
            cfg.general.timezone, cfg.general.slot_minutes, "pv"),
            f"lokal ({source})")
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


def calibrate_pv(repo, cfg, now, lookback_days, promotion_days=14,
                 source=None):
    start = now - timedelta(days=lookback_days)
    fcast, fsrc = _pv_forecast_hist(cfg, repo, start, now, source=source)
    actual, asrc = _pv_actual_hist(cfg, repo, start, now)
    if fcast is None or fcast.empty or actual.empty:
        return None
    idx = actual.index.intersection(fcast.index)
    a, p = actual.reindex(idx), fcast.reindex(idx)
    # nur Tagstunden (Vorhersage > 50 W) bewerten
    day = p > 50.0
    a_d, p_d = a[day], p[day]
    m = _metrics(a_d.values, p_d.values)
    promotion_start = now - timedelta(days=max(1, int(promotion_days)))
    fit = a_d.index < promotion_start
    a_fit, p_fit = a_d[fit], p_d[fit]
    if len(a_fit) < 20:
        a_fit, p_fit = a_d.iloc[0:0], p_d.iloc[0:0]
    fit_metrics = _metrics(a_fit.values, p_fit.values)
    hourly = _factor_table(
        a_fit, p_fit, lambda i: i.tz_convert(cfg.general.timezone).hour)
    monthly = _factor_table(
        a_fit, p_fit, lambda i: i.tz_convert(cfg.general.timezone).month)
    month_hour = _month_hour_table(a_fit, p_fit, cfg.general.timezone)
    sig = cfg.influxdb.signals.get("pv_forecast")
    cur_scale = sig.scale if sig else 1.0
    promotion_frame = pd.DataFrame({
        "actual": a_d[~fit], "raw": p_d[~fit]}).dropna()
    return {"metrics": m, "fit_metrics": fit_metrics,
            "hourly": hourly, "monthly": monthly, "month_hour": month_hour,
            "current_scale": cur_scale, "forecast_source": fsrc, "actual_source": asrc,
            "suggested_scale": round(
                cur_scale * fit_metrics.get("scale_actual_over_pred", 1.0), 4),
            "promotion_samples": int(len(promotion_frame)),
            "_promotion_frame": promotion_frame}


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
        low, high = BOUNDS["hourly_correction"]
        productive_hourly[hour] = round(min(high, max(low, factor)), 3)
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


def calibrate_load(repo, cfg, now, lookback_days, test_days,
                   promotion_days=14):
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

    promotion_start = now - timedelta(days=max(1, int(promotion_days)))
    promotion_frame = pd.DataFrame({
        "actual": a[a.index >= promotion_start],
        "raw": p[p.index >= promotion_start]}).dropna()
    return {"metrics": m, "hourly": hourly, "daytype": daytype_tab,
            "temp_used": temp is not None, "by_temperature": temp_bins,
            "load_source": ("lokal (RSCP house_load)" if use_local
                            else "InfluxDB (house_consumption)"),
            "temp_source": ("Open-Meteo (lokal)" if (temp is not None and cfg.weather.enabled)
                            else "InfluxDB" if temp is not None else "—"),
            "suggested_correction_factor": round(
                m.get("scale_actual_over_pred", 1.0), 4),
            "promotion_samples": int(len(promotion_frame)),
            "_promotion_frame": promotion_frame}


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
    ap.add_argument("--dry-run", action="store_true",
                    help="vollständig rechnen, aber weder Profil/Report/Overrides "
                         "noch Kalibrierungshistorie verändern")
    args = ap.parse_args()

    cfg = load_config(args.config)
    previous_report = {}
    previous_profile = {}
    # Vor dem Überschreiben den bislang produktiven Stand als Ausgangspunkt
    # sichern. Beim ersten Lauf nach dem Upgrade entsteht so bereits ein
    # echter Vorher/Nachher-Vergleich statt erst ab der übernächsten Woche.
    try:
        from ems.local_history import write_calibration_snapshot
        with open(args.output, encoding="utf-8") as fh:
            previous_report = yaml.safe_load(fh) or {}
        try:
            with open(cfg.calibration.pv_profile, encoding="utf-8") as fh:
                previous_profile = yaml.safe_load(fh) or {}
        except OSError:
            previous_profile = {}
        previous_generated = (
            previous_report.get("generated")
            or previous_profile.get("generated"))
        if previous_generated and not args.dry_run:
            write_calibration_snapshot(
                cfg.e3dc_rscp.history_db_path, previous_generated,
                previous_report, previous_profile)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Hinweis: bisheriger Kalibrierungsstand nicht archiviert: {exc}")

    repo = InfluxRepository(cfg)
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(f"{cfg.general.slot_minutes}min")
    validation = None
    try:
        print(f"Kalibrierung über {args.lookback_days} Tage (Test: letzte {args.test_days} Tage) ...")
        pv = calibrate_pv(
            repo, cfg, now, args.lookback_days,
            cfg.calibration.promotion_days)
        # Auch die NICHT produktive Quelle kalibrieren. Sonst tritt sie im
        # Quellenvergleich roh gegen eine korrigierte Prognose an, und die
        # Umschaltempfehlung misst Kalibrierung statt Prognosequalitaet.
        pv_aktiv = pv_eval.active_source(cfg)
        pv_schatten = {}
        for _name in pv_eval.source_groups(cfg):
            if _name == pv_aktiv:
                continue
            _res = calibrate_pv(
                repo, cfg, now, args.lookback_days,
                cfg.calibration.promotion_days, source=_name)
            if _res:
                pv_schatten[_name] = _res
        load = calibrate_load(
            repo, cfg, now, args.lookback_days, args.test_days,
            cfg.calibration.promotion_days)
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

    # --- Champion-/Challenger-Entscheidung für Punktprognosen -------------
    from ems.calibration import apply_pv_correction
    from ems.calibration_competition import (
        apply_load_profile, compare_point_forecasts)

    cc = cfg.calibration
    candidate_profile = {"generated": now.isoformat()}
    if pv:
        candidate_profile.update(_pv_factors(pv))
    rolling_hourly = (validation or {}).get("hourly_correction")
    if rolling_hourly:
        candidate_profile["load_hourly"] = {
            int(h): round(float(min(1.8, max(0.6, factor))), 3)
            for h, factor in rolling_hourly.items()}
        candidate_profile["load_global"] = float(
            validation.get("global_correction", 1.0))
        candidate_profile["load_profile_source"] = validation.get(
            "correction_profile_source", "historical_bootstrap")
        candidate_profile["load_archive_folds"] = int(validation.get(
            "archive_folds", 0))
        candidate_profile["load_archive_min_folds"] = int(validation.get(
            "archive_min_folds", 6))
        candidate_profile["load_archive_weight"] = float(validation.get(
            "archive_weight", 0.0))

    def _point_decision(frame, champion, challenger, available):
        if frame is None or frame.empty:
            return {
                "n": 0, "promote": False, "status": "insufficient",
                "reason": "keine unabhängigen Prüfdaten"}
        decision = compare_point_forecasts(
            frame["actual"], champion, challenger, cfg.general.timezone,
            min_samples=cc.promotion_min_samples,
            min_improvement_percent=cc.promotion_min_improvement_percent,
            max_segment_degradation_pct=(
                cc.promotion_max_segment_degradation_pct),
            max_bias_increase_w=cc.promotion_max_bias_increase_w,
            champion_available=available)
        if not cc.champion_challenger_enabled:
            decision.update(
                promote=True, status="disabled",
                reason="Champion-/Challenger-Prüfung deaktiviert")
        return decision

    pv_frame = pv.pop("_promotion_frame", None) if pv else None
    pv_available = any(
        key in previous_profile
        for key in ("pv_global", "pv_month_hour", "pv_hour", "pv_month"))
    pv_competition = {
        "n": 0, "promote": False, "status": "insufficient",
        "reason": "kein PV-Challenger"}
    if (pv_frame is not None and not pv_frame.empty and pv
            and int(pv.get("fit_metrics", {}).get("n", 0))
            >= cc.promotion_min_samples):
        pv_competition = _point_decision(
            pv_frame,
            apply_pv_correction(
                pv_frame["raw"], previous_profile, cfg.general.timezone),
            apply_pv_correction(
                pv_frame["raw"], candidate_profile, cfg.general.timezone),
            pv_available)
    elif pv:
        pv_competition["reason"] = (
            f"Challenger-Training "
            f"{int(pv.get('fit_metrics', {}).get('n', 0))}/"
            f"{cc.promotion_min_samples} Slots")

    load_frame = load.pop("_promotion_frame", None) if load else None
    load_available = bool(previous_profile.get("load_hourly"))
    load_competition = {
        "n": 0, "promote": False, "status": "insufficient",
        "reason": "kein Last-Challenger"}
    if (load_frame is not None and not load_frame.empty
            and candidate_profile.get("load_hourly")):
        load_competition = _point_decision(
            load_frame,
            apply_load_profile(
                load_frame["raw"], previous_profile, cfg.general.timezone),
            apply_load_profile(
                load_frame["raw"], candidate_profile, cfg.general.timezone),
            load_available)

    # --- Korrekturprofil je Quellgruppe -----------------------------------
    # Jede Quelle bekommt ihr eigenes Profil und ihre eigene Champion-/
    # Challenger-Pruefung. Ueber Kreuz angewandt verschlechtern die Faktoren:
    # pvlib mit Solcasts Profil kam auf WAPE 22,2 statt 16,0 roh.
    vorher_je_quelle = dict(previous_profile.get("pv_sources") or {})
    quellen_profile = dict(vorher_je_quelle)
    quellen_wettbewerb = {}
    if pv_aktiv and pv_aktiv not in quellen_profile:
        flach = {k: previous_profile[k]
                 for k in _PV_KEYS if k in previous_profile}
        if flach:
            quellen_profile[pv_aktiv] = flach
    if pv and pv_aktiv:
        # Die produktive Quelle hat ihre Pruefung oben schon hinter sich.
        quellen_wettbewerb[pv_aktiv] = pv_competition
        if pv_competition.get("promote") or pv_aktiv not in quellen_profile:
            quellen_profile[pv_aktiv] = _pv_factors(pv)
    for _name, _res in pv_schatten.items():
        _kandidat = _pv_factors(_res)
        _rahmen = _res.pop("_promotion_frame", None)
        _champion = vorher_je_quelle.get(_name)
        _entscheid = {"n": 0, "promote": False, "status": "insufficient",
                      "reason": "kein PV-Challenger"}
        if (_rahmen is not None and not _rahmen.empty
                and int(_res.get("fit_metrics", {}).get("n", 0))
                >= cc.promotion_min_samples):
            _entscheid = _point_decision(
                _rahmen,
                apply_pv_correction(_rahmen["raw"], _champion,
                                    cfg.general.timezone),
                apply_pv_correction(_rahmen["raw"], _kandidat,
                                    cfg.general.timezone),
                bool(_champion))
        quellen_wettbewerb[_name] = _entscheid
        if _entscheid.get("promote") or not _champion:
            quellen_profile[_name] = _kandidat

    pv_quellen_bericht = {}
    if pv and pv_aktiv:
        pv_quellen_bericht[pv_aktiv] = _pv_source_summary(
            pv, "produktiv", quellen_wettbewerb.get(pv_aktiv) or {})
    for _name, _res in pv_schatten.items():
        pv_quellen_bericht[_name] = _pv_source_summary(
            _res, "Schattenmodell", quellen_wettbewerb.get(_name) or {})

    competition = {
        "enabled": bool(cc.champion_challenger_enabled),
        "promotion_days": int(cc.promotion_days),
        "min_samples": int(cc.promotion_min_samples),
        "min_improvement_percent": float(
            cc.promotion_min_improvement_percent),
        "signals": {
            "pv_correction": pv_competition,
            "load_correction": load_competition,
        },
        "pv_sources": quellen_wettbewerb,
    }

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
            from ems.config import save_override
            band = pv_eval.calibrate_band(
                cfg, lookback_days=args.band_lookback_days,
                promotion_days=cc.promotion_days)
        except Exception as exc:
            print(f"\nPV-Bandkalibrierung fehlgeschlagen: {exc}")
    band_applied = {}
    if band and not band.get("insufficient"):
        from ems.calibration_competition import compare_intervals

        print(f"\nPV-Band (pvlib) aus {band['n']} Residuen ({band['method']}):")
        print(f"     Abdeckung aktuell: {band['current_below_p10_pct']} % unter p10 "
              f"(Ziel {band['target_low_pct']:.0f} %), "
              f"{band['current_above_p90_pct']} % über p90.")
        proposed = {}
        for key, rkey in (("p10_uncertainty", "recommended_p10_uncertainty"),
                          ("p90_uncertainty", "recommended_p90_uncertainty")):
            old = float(getattr(cfg.pv_model, key))
            new = round(old + 0.5 * (band[rkey] - old), 3)      # 50 % gedämpft
            proposed[key] = new
        ratios = band.pop("_promotion_ratios", [])
        if int(band.get("training_n", 0)) < cc.promotion_min_samples:
            band_competition = {
                "n": int(band.get("promotion_n", 0)), "promote": False,
                "status": "insufficient",
                "reason": (
                    f"Challenger-Training {int(band.get('training_n', 0))}/"
                    f"{cc.promotion_min_samples} Residuen")}
        else:
            band_competition = compare_intervals(
                ratios,
                1.0 - float(cfg.pv_model.p10_uncertainty),
                1.0 + float(cfg.pv_model.p90_uncertainty),
                1.0 - proposed["p10_uncertainty"],
                1.0 + proposed["p90_uncertainty"],
                min_samples=cc.promotion_min_samples,
                min_improvement_percent=cc.promotion_min_improvement_percent)
        if not cc.champion_challenger_enabled:
            band_competition.update(
                promote=True, status="disabled",
                reason="Champion-/Challenger-Prüfung deaktiviert")
        competition["signals"]["pv_band"] = band_competition
        for key, rkey in (("p10_uncertainty", "recommended_p10_uncertainty"),
                          ("p90_uncertainty", "recommended_p90_uncertainty")):
            old, new = float(getattr(cfg.pv_model, key)), proposed[key]
            if band_competition["promote"] and not args.dry_run:
                save_override(args.config, f"pv_model.{key}", new)
                band_applied[key] = new
            verdict = (
                "würde übernommen" if args.dry_run and band_competition["promote"]
                else "übernommen" if band_competition["promote"]
                else "gehalten")
            print(f"  -> pv_model.{key}: {old} -> {new} "
                  f"(Ziel {band[rkey]}, gedämpft; {verdict})")
    elif band:
        print(f"\nPV-Band: zu wenig getrennte Trainingsresiduen "
              f"({band.get('training_n', 0)} Training / "
              f"{band.get('promotion_n', 0)} Holdout; "
              f"{band.get('n', 0)} gesamt) – Festwert bleibt.")
        band.pop("_promotion_ratios", None)
        competition["signals"]["pv_band"] = {
            "n": int(band.get("promotion_n", 0)), "promote": False,
            "status": "insufficient",
            "reason": "zu wenig getrennte Trainings-/Prüfresiduen"}
    else:
        competition["signals"]["pv_band"] = {
            "n": 0, "promote": False, "status": "insufficient",
            "reason": "kein PV-Band-Challenger"}

    # Aktives Profil komponentenweise zusammensetzen. Ein abgelehnter
    # Challenger lässt exakt die betreffenden Champion-Schlüssel unangetastet.
    profile = {
        key: value for key, value in previous_profile.items()
        if key.startswith("pv_") or key.startswith("load_")
    }
    if pv_competition.get("promote"):
        for key in ("pv_global", "pv_month_hour", "pv_hour", "pv_month"):
            if key in candidate_profile:
                profile[key] = candidate_profile[key]
    if load_competition.get("promote"):
        for key in (
                "load_hourly", "load_global", "load_profile_source",
                "load_archive_folds", "load_archive_min_folds",
                "load_archive_weight"):
            if key in candidate_profile:
                profile[key] = candidate_profile[key]
    if quellen_profile:
        profile["pv_sources"] = quellen_profile
    profile["generated"] = now.isoformat()
    profile["calibration_competition"] = competition

    print("\n" + "=" * 62 + "\nChampion-/Challenger-Kalibrierung\n"
          + "-" * 62)
    labels = {
        "pv_correction": "PV-Korrektur",
        "load_correction": "Lastkorrektur",
        "pv_band": "PV-Band",
    }
    for name, decision in competition["signals"].items():
        verdict = (
            "ÜBERNEHMEN" if decision.get("promote")
            else "CHAMPION HALTEN" if decision.get("status") == "held"
            else "NOCH NICHT BEWERTBAR")
        print(f"  {labels.get(name, name):<16} {verdict:<20} "
              f"n={int(decision.get('n', 0)):>4} · "
              f"{decision.get('reason', '–')}")
    if quellen_wettbewerb:
        print("  PV-Korrektur je Quelle (Grundlage des Quellenvergleichs):")
        for name, decision in quellen_wettbewerb.items():
            verdict = (
                "ÜBERNEHMEN" if decision.get("promote")
                else "CHAMPION HALTEN" if decision.get("status") == "held"
                else "NOCH NICHT BEWERTBAR")
            rolle = " (produktiv)" if name == pv_aktiv else ""
            faktor = (quellen_profile.get(name) or {}).get("pv_global", "–")
            print(f"    {name + rolle:<14} {verdict:<20} "
                  f"n={int(decision.get('n', 0)):>4} · global={faktor}")

    out = {
        "generated": now.isoformat(),
        "lookback_days": args.lookback_days,
        "test_days": args.test_days,
        "pv_forecast": pv,
        "pv_forecast_sources": pv_quellen_bericht,
        "load_forecast": load,
        "forecast_validation": validation,
        "pv_band": band,
        "calibration_competition": competition,
        "empfohlene_config": {
            "influxdb.signals.pv_forecast.scale": pv["suggested_scale"] if pv else None,
            "forecast.correction_factor": (
                validation.get("global_correction")
                if validation
                else load["suggested_correction_factor"] if load else None),
            **{f"pv_model.{k}": v for k, v in band_applied.items()},
        },
    }
    if not args.dry_run:
        with open(args.output, "w", encoding="utf-8") as fh:
            yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=False)
        print(f"\nDetails in {args.output} geschrieben.")
    else:
        print(f"\nDry-run: {args.output}, Profil und Overrides nicht verändert.")

    # Anwendbares Profil (zeitabhängige Korrekturen) schreiben. Wird von der
    # Pipeline genutzt, wenn calibration.enabled=true (config).
    if (pv or load or previous_profile) and not args.dry_run:
        with open(cfg.calibration.pv_profile, "w", encoding="utf-8") as fh:
            yaml.safe_dump(profile, fh, allow_unicode=True, sort_keys=True)
        promoted = [
            name for name, value in competition["signals"].items()
            if value.get("promote")]
        print("Korrekturprofil (Champion-/Challenger-geprüft) -> "
              f"kalibrierung_profil.yaml; übernommen: "
              f"{', '.join(promoted) if promoted else 'kein Challenger'}")

    # Den vollständigen Wochenstand nach erfolgreicher Dateiausgabe dauerhaft
    # archivieren. Ein Ausfall der reinen Diagnosehistorie darf die eigentliche
    # Kalibrierung nicht fehlschlagen lassen.
    try:
        from ems.local_history import write_calibration_snapshot
        if not args.dry_run:
            write_calibration_snapshot(
                cfg.e3dc_rscp.history_db_path, now, out, profile)
            print("Kalibrierungsstand mit Erstellungszeit archiviert.")
    except Exception as exc:
        print(f"Warnung: Kalibrierungshistorie konnte nicht archiviert werden: {exc}")

    print("\nAnwenden:")
    print("  Zeitabhängig (empfohlen): calibration.enabled=true + pv_profile=")
    print("  ./kalibrierung_profil.yaml in config.yaml - PV (Monat x Stunde)")
    print("  und Last (je Stunde) werden dann automatisch angewandt;")
    print("  forecast.correction_factor wird bei vorhandenem Last-Profil")
    print("  ignoriert (kein Doppel-Faktor). Danach Dienst neu starten.")


if __name__ == "__main__":
    main()
