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

    # Modell aus Trainingszeitraum, ohne bestehende Korrektur, out-of-sample prüfen
    cfg.forecast.correction_factor = 1.0
    fc = LoadForecaster(cfg)
    horizon = len(actual_test)
    pred = fc.forecast(train, test_start, horizon, hist_temp=temp, fut_temp=temp)
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
    args = ap.parse_args()

    cfg = load_config(args.config)
    repo = InfluxRepository(cfg)
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(f"{cfg.general.slot_minutes}min")
    try:
        print(f"Kalibrierung über {args.lookback_days} Tage (Test: letzte {args.test_days} Tage) ...")
        pv = calibrate_pv(repo, cfg, now, args.lookback_days)
        load = calibrate_load(repo, cfg, now, args.lookback_days, args.test_days)
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

    out = {
        "generated": now.isoformat(),
        "lookback_days": args.lookback_days,
        "test_days": args.test_days,
        "pv_forecast": pv,
        "load_forecast": load,
        "empfohlene_config": {
            "influxdb.signals.pv_forecast.scale": pv["suggested_scale"] if pv else None,
            "forecast.correction_factor": load["suggested_correction_factor"] if load else None,
        },
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=False)
    print(f"\nDetails in {args.output} geschrieben.")

    # Anwendbares PV-Profil (zeitabhängige Korrektur Monat x Stunde) schreiben.
    # Wird von der Pipeline genutzt, wenn calibration.enabled=true (config).
    if pv:
        profile = {
            "generated": now.isoformat(),
            "pv_global": round(pv["metrics"].get("scale_actual_over_pred", 1.0), 4),
            "pv_month_hour": pv.get("month_hour", {}),
            "pv_hour": pv.get("hourly", {}),
            "pv_month": pv.get("monthly", {}),
        }
        with open("kalibrierung_profil.yaml", "w", encoding="utf-8") as fh:
            yaml.safe_dump(profile, fh, allow_unicode=True, sort_keys=True)
        print("PV-Korrekturprofil (Monat x Stunde) -> kalibrierung_profil.yaml")

    print("\nAnwenden:")
    print("  A) Einfach/global: forecast.correction_factor (Verbrauch) und ggf.")
    print("     influxdb.signals.pv_forecast.scale (PV) in config.yaml setzen.")
    print("  B) PV zeitabhängig (empfohlen wg. starkem Tages-/Monatsmuster):")
    print("     pv_forecast.scale = 1.0 lassen und in config.yaml setzen:")
    print("       calibration:\n         enabled: true\n         pv_profile: ./kalibrierung_profil.yaml")
    print("  Danach Dienst neu starten.")


if __name__ == "__main__":
    main()
