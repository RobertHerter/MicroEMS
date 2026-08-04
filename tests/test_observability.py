"""Beobachtbarkeits-Auswertungen (ems/observability): Ersparnis über die Zeit
und Prognosegüte – reine Leser/Aggregatoren über die lokale Historie."""
from __future__ import annotations

import pandas as pd
import pytest

from ems.observability import (_metrics, _pv_nowcast_accuracy, _wape, battery_health,
                               calibration_maturity,
                               forecast_accuracy, forecast_analysis, savings_drivers,
                               savings_over_time)
from tests.test_synthetic import make_config


def _row(saved, imp=5.0, exp=1.0, meter=1.5, baseline=None, ok=True):
    baseline = baseline if baseline is not None else meter + saved
    return {"n_slots": 96,
            "meter": {"import_kwh": imp, "export_kwh": exp, "net_cost_eur": meter},
            "baseline": {"net_cost_eur": baseline, "end_soc_wh": None},
            "saved_eur": saved, "balance": {"ok": ok},
            "tracker": None, "computed_ts": None}


def test_wape_and_metrics():
    assert _wape([100.0, 100.0], [110.0, 90.0]) == 10.0
    assert _wape([0.0, 0.0], [1.0, 1.0]) == 0.0        # leere Basis -> 0
    m = _metrics([100.0, 200.0], [120.0, 180.0])
    assert m["n"] == 2 and m["bias_w"] == 0.0 and m["mae_w"] == 20.0
    assert _metrics([], [])["n"] == 0


def test_savings_over_time_aggregates_day_week_month(tmp_path):
    from ems.local_history import write_savings_validated
    db = str(tmp_path / "h.sqlite")
    # zwei Tage in derselben ISO-Woche + ein Tag im Folgemonat
    write_savings_validated(db, "2026-01-05", _row(1.0))   # Mo KW02
    write_savings_validated(db, "2026-01-06", _row(2.0))   # Di KW02
    write_savings_validated(db, "2026-02-02", _row(3.0))   # Feb
    out = savings_over_time(db)
    assert out["days"] == 3
    assert out["total_saved_eur"] == 6.0
    assert len(out["daily"]) == 3
    weekly = {w["period"]: w for w in out["weekly"]}
    assert weekly["2026-KW02"]["saved_eur"] == 3.0 and weekly["2026-KW02"]["days"] == 2
    monthly = {m["period"]: m for m in out["monthly"]}
    assert monthly["2026-01"]["saved_eur"] == 3.0
    assert monthly["2026-02"]["saved_eur"] == 3.0


def test_savings_over_time_empty(tmp_path):
    out = savings_over_time(str(tmp_path / "leer.sqlite"))
    assert out == {"days": 0, "total_saved_eur": 0.0,
                   "daily": [], "weekly": [], "monthly": []}


def test_battery_health_from_actuals(tmp_path):
    """Durchsatz/äquiv. Vollzyklen und Vollstand-Verweildauer aus den Ist-Werten."""
    from ems.local_history import write_actuals
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    cfg.calibration.pv_profile = str(tmp_path / "kalibrierung_profil.yaml")
    db = cfg.e3dc_rscp.history_db_path
    step = pd.Timedelta(minutes=cfg.general.slot_minutes)
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(step)
    for i in range(8):                              # 4x laden, SoC ab Slot 2 = 100 %
        write_actuals(db, now - step * (8 - i), {
            "battery_w": 2000.0 if i < 4 else 0.0,
            "soc_percent": 100.0 if i >= 2 else 50.0,
            "pv_w": 0.0, "house_load_w": 0.0, "grid_w": 0.0})
    h = battery_health(cfg, days=1)
    assert h["n"] == 8
    assert h["throughput_kwh"] > 0.0 and h["cycles_equiv"] >= 0.0
    assert h["soc_max_pct"] == 100.0 and h["time_full_pct"] == 75.0


def test_savings_drivers_from_actuals(tmp_path):
    """#5: Treiber (Eigenverbrauch/Autarkie) aus den Ist-Werten."""
    from ems.local_history import write_actuals
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    db = cfg.e3dc_rscp.history_db_path
    step = pd.Timedelta(minutes=cfg.general.slot_minutes)
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(step)
    for i in range(4):                     # PV 2000, Last 500, Einspeisung 1000 W
        write_actuals(db, now - step * (4 - i), {
            "pv_w": 2000.0, "house_load_w": 500.0, "grid_w": -1000.0,
            "battery_w": 0.0, "soc_percent": 80.0})
    d = savings_drivers(cfg, days=1)
    assert d["n"] == 4
    assert d["pv_kwh"] == pytest.approx(2.0) and d["export_kwh"] == pytest.approx(1.0)
    assert d["self_consumed_kwh"] == pytest.approx(1.0)
    assert d["self_consumption_pct"] == pytest.approx(50.0)
    assert d["import_kwh"] == pytest.approx(0.0)
    assert d["autarky_pct"] == pytest.approx(100.0)   # kein Netzbezug


def test_battery_health_empty(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "leer.sqlite")
    h = battery_health(cfg, days=7)
    assert h["n"] == 0 and h["throughput_kwh"] is None


def test_forecast_accuracy_daily_roundtrip(tmp_path):
    """#3: Tages-Prognosegüte persistieren und als aufsteigenden Trend lesen."""
    from ems.local_history import (latest_forecast_accuracy_day,
                                   read_forecast_accuracy,
                                   write_forecast_accuracy)
    db = str(tmp_path / "h.sqlite")
    write_forecast_accuracy(db, "2026-07-21", {
        "pv": {"wape_pct": 12.0, "bias_w": -5.0, "n": 40},
        "load": {"wape_pct": 20.0, "bias_w": 3.0, "n": 96}})
    write_forecast_accuracy(db, "2026-07-22", {
        "pv": {"wape_pct": 9.0, "bias_w": 1.0, "n": 42},
        "load": {"wape_pct": 18.0, "bias_w": 2.0, "n": 96}})
    trend = read_forecast_accuracy(db, days=30)
    assert [t["day"] for t in trend] == ["2026-07-21", "2026-07-22"]  # aufsteigend
    assert trend[-1]["pv_wape"] == 9.0 and trend[0]["load_wape"] == 20.0
    assert latest_forecast_accuracy_day(db) == "2026-07-22"
    # idempotent je Tag
    write_forecast_accuracy(db, "2026-07-22", {
        "pv": {"wape_pct": 7.0, "n": 42}, "load": {"wape_pct": 17.0, "n": 96}})
    assert len(read_forecast_accuracy(db, days=30)) == 2


def test_forecast_accuracy_graceful_on_empty_history(tmp_path):
    """Ohne Historie liefert forecast_accuracy leere, aber wohlgeformte Metriken
    (n=0) statt zu werfen."""
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    out = forecast_accuracy(cfg, days=7)
    assert out["days"] == 7
    assert out["pv"]["n"] == 0 and out["load"]["n"] == 0
    assert out["pv_nowcast"]["n"] == 0


def test_pv_nowcast_challenger_compares_same_vintage(tmp_path):
    from ems.local_history import (write_actuals,
                                   write_optimizer_forecast_archive)

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    cfg.forecast.intraday_pv_operational_slots = 4
    origin = pd.Timestamp("2026-07-28 10:00", tz=cfg.general.timezone)
    issue = origin + pd.Timedelta(seconds=23)
    index = pd.date_range(origin, periods=8, freq="15min")
    write_optimizer_forecast_archive(
        cfg.e3dc_rscp.history_db_path, issue, {
            "pv_w": pd.Series(1000.0, index=index),
            "pv_without_nowcast_w": pd.Series(1200.0, index=index),
        })
    for stamp in index[:4]:
        write_actuals(cfg.e3dc_rscp.history_db_path, stamp, {
            "pv_w": 1000.0, "house_load_w": 500.0, "grid_w": 0.0,
            "battery_w": 0.0, "soc_percent": 50.0,
        })

    result = _pv_nowcast_accuracy(
        cfg, origin - pd.Timedelta(hours=1), origin + pd.Timedelta(hours=1))
    assert result["n"] == 4
    assert result["productive"]["wape_pct"] == 0.0
    assert result["without_nowcast"]["wape_pct"] == 20.0
    assert result["improvement_wape_pp"] == 20.0
    # Die Arithmetik bleibt sichtbar, der Sieger wird bei 4 Paaren aber noch
    # nicht ausgerufen ("keine Aussage ohne Stichprobe", ems/quality.MIN_SAMPLES).
    assert result["sufficient"] is False
    assert result["winner"] == "unklar" and "sammelt noch" in result["reason"]


def test_forecast_analysis_heatmap_and_vintages(tmp_path):
    """Produktions-Snapshots werden nach Vorlauf gruppiert und als mehrere
    Erstellungsstände desselben Zieltags ausgegeben."""
    from ems.config import PvArray, SolcastSource
    from ems.local_history import (write_house_load,
                                   write_optimizer_forecast_archive,
                                   write_pv_actual,
                                   write_pv_forecast_archive)

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    cfg.solcast.sources = [
        SolcastSource(api_key="test", resource_id="solcast-test")]
    cfg.pv_model.arrays = [
        PvArray(name="Dach", kwp=10.0, tilt=25.0, azimuth=180.0)]
    tz = cfg.general.timezone
    day = pd.Timestamp("2026-07-28", tz=tz)
    end = day + pd.DateOffset(days=1)
    index = pd.date_range(day, end, freq="15min", inclusive="left")
    write_pv_actual(cfg.e3dc_rscp.history_db_path, {
        stamp.tz_convert("UTC").isoformat(): 1000.0 for stamp in index})
    write_house_load(cfg.e3dc_rscp.history_db_path, {
        stamp.tz_convert("UTC").isoformat(): 500.0 for stamp in index})
    forecasts = {
        "pv_w": pd.Series(1100.0, index=index),
        "house_load_w": pd.Series(550.0, index=index),
    }
    write_optimizer_forecast_archive(
        cfg.e3dc_rscp.history_db_path, day - pd.Timedelta(hours=12), forecasts)
    write_optimizer_forecast_archive(
        cfg.e3dc_rscp.history_db_path, day + pd.Timedelta(hours=6), forecasts)
    source_map = {
        stamp.tz_convert("UTC").isoformat(): (1200.0, 900.0, 1400.0)
        for stamp in index}
    write_pv_forecast_archive(
        cfg.e3dc_rscp.history_db_path, "solcast-test",
        day - pd.Timedelta(hours=12), source_map)
    pvlib_map = {
        stamp: (1050.0, 800.0, 1250.0) for stamp in source_map}
    write_pv_forecast_archive(
        cfg.e3dc_rscp.history_db_path, "pvmodel:Dach",
        day - pd.Timedelta(hours=12), pvlib_map)

    result = forecast_analysis(
        cfg, days=30, target_day="2026-07-28", now=end)
    assert result["heatmaps"]["pv"]["samples"] > 0
    assert result["heatmaps"]["load"]["samples"] > 0
    assert any(value == 10.0 for row in result["heatmaps"]["pv"]["wape"]
               for value in row if value is not None)
    vintages = result["vintages"]
    assert vintages["day"] == "2026-07-28"
    assert len(vintages["series"]) == 2
    assert vintages["actual"]["pv_w"][0] == 1000.0
    assert vintages["series"][0]["load_w"][0] == 550.0
    comparison = result["day_comparison"]
    assert comparison["pv_actual_w"][0] == 1000.0
    assert comparison["solcast_w"][0] == 1200.0
    assert comparison["pvlib_w"][0] == 1050.0
    assert comparison["load_forecast_w"][0] == 550.0


def test_forecast_analysis_empty_history(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "leer.sqlite")
    cfg.calibration.pv_profile = str(tmp_path / "kalibrierung_profil.yaml")
    out = forecast_analysis(
        cfg, target_day="2026-07-28",
        now=pd.Timestamp("2026-07-28 12:00", tz=cfg.general.timezone))
    assert out["heatmaps"]["pv"]["samples"] == 0
    assert out["vintages"]["series"] == []


def test_calibration_maturity_reports_samples_coverage_and_active_values(tmp_path):
    import yaml

    from ems.local_history import (write_calibration_snapshot,
                                   write_pv_source_selection)

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    cfg.calibration.pv_profile = str(tmp_path / "kalibrierung_profil.yaml")
    report = {
        "generated": "2026-07-26T03:00:00+02:00",
        "pv_forecast": {
            "metrics": {"n": 900, "nmae_pct": 20.0, "corr": 0.88},
            "monthly": {"7": 0.91}},
        "pv_band": {
            "n": 96, "current_below_p10_pct": 18.0,
            "current_above_p90_pct": 17.0,
            "recommended_p10_uncertainty": 0.7,
            "recommended_p90_uncertainty": 0.5},
        "load_forecast": {"metrics": {"n": 30000, "nmae_pct": 30.0}},
        "forecast_validation": {
            "archive_folds": 2, "archive_min_folds": 6,
            "archive_weight": 0.333, "global_correction": 1.02,
            "empfehlung": "similar_days"},
        "calibration_competition": {
            "promotion_days": 14, "min_samples": 96,
            "min_improvement_percent": 1.0,
            "signals": {
                "pv_correction": {
                    "n": 128, "promote": True, "status": "promoted",
                    "reason": "WAPE verbessert sich um 4,0 %"},
                "load_correction": {
                    "n": 128, "promote": False, "status": "held",
                    "reason": "Verbesserung zu klein"},
                "pv_band": {
                    "n": 48, "promote": False, "status": "insufficient",
                    "reason": "zu wenig Prüfdaten"},
            },
        },
    }
    (tmp_path / "kalibrierung.yaml").write_text(
        yaml.safe_dump(report), encoding="utf-8")
    (tmp_path / "kalibrierung_profil.yaml").write_text(
        yaml.safe_dump({"generated": report["generated"], "pv_global": 0.91}),
        encoding="utf-8")
    previous = {
        **report,
        "generated": "2026-07-19T03:00:00+02:00",
        "empfohlene_config": {
            "pv_model.p10_uncertainty": 0.65,
            "pv_model.p90_uncertainty": 0.45},
    }
    write_calibration_snapshot(
        cfg.e3dc_rscp.history_db_path, previous["generated"], previous,
        {"generated": previous["generated"], "pv_global": 0.95})
    now = pd.Timestamp("2026-07-29 12:00", tz=cfg.general.timezone)
    write_pv_source_selection(
        cfg.e3dc_rscp.history_db_path, now - pd.Timedelta(days=8), "solcast",
        "solcast aktiv", {"groups": {
            "solcast": {"n": 120, "wape_pct": 20.0,
                        "decision_score_pct": 18.0,
                        "context_coverage_pct": 90.0},
            "pvlib": {"n": 120, "wape_pct": 25.0,
                      "decision_score_pct": 24.0,
                      "context_coverage_pct": 90.0}}, "min_samples": 96})
    write_pv_source_selection(
        cfg.e3dc_rscp.history_db_path, now, "solcast", "solcast bleibt aktiv",
        {"groups": {
            "solcast": {"n": 128, "wape_pct": 20.0,
                        "decision_score_pct": 18.0,
                        "context_coverage_pct": 90.0},
            "pvlib": {"n": 128, "wape_pct": 25.0,
                      "decision_score_pct": 24.0,
                      "context_coverage_pct": 90.0}}, "min_samples": 96})

    out = calibration_maturity(cfg, now)
    cards = {card["key"]: card for card in out["cards"]}
    assert cards["calibration_competition"]["state"] == "Prüfdaten im Aufbau"
    assert cards["calibration_competition"]["values"][0]["value"] == "PV"
    assert cards["calibration_competition"]["values"][1]["value"] == "Last"
    assert cards["pv_correction"]["confidence_pct"] == 33.0
    assert cards["pv_correction"]["values"][0]["value"] == 0.91
    assert cards["pv_band"]["confidence_pct"] == 50.0
    assert cards["load_correction"]["state"] == "Hybridprofil 2/6 Folds"
    assert cards["source_selection"]["confidence_pct"] == 57.0
    assert cards["source_selection"]["reason"] == "solcast bleibt aktiv"
    assert cards["source_selection"]["values"][1]["value"]["solcast"] == 18.0
    assert len(out["history"]) == 2
    assert out["history"][0]["pv_factor"] == 0.95
    assert out["history"][1]["pv_factor"] == 0.91
    assert out["history"][1]["changes"][0]["label"] == "PV-Faktor"


def _variant_snapshots(tmp_path, ml_gap_slots=0):
    """Historie mit archivierten Prognose-Vorstufen aufbauen.

    ``ml_gap_slots`` streicht dem ml-Kandidaten die ersten n Slots - damit
    lässt sich prüfen, dass eine Lücke in EINER Variante alle anderen auf
    denselben Zielslots mitschrumpfen lässt.
    """
    from ems.local_history import (write_house_load,
                                   write_optimizer_forecast_archive)

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    tz = cfg.general.timezone
    day = pd.Timestamp("2026-07-28", tz=tz)
    end = day + pd.DateOffset(days=1)
    index = pd.date_range(day, end, freq="15min", inclusive="left")
    write_house_load(cfg.e3dc_rscp.history_db_path, {
        stamp.tz_convert("UTC").isoformat(): 500.0 for stamp in index})
    ml = pd.Series(525.0, index=index)          # 5 % - bestes Einzelmodell
    if ml_gap_slots:
        ml.iloc[:ml_gap_slots] = float("nan")
    forecasts = {
        "house_load_w": pd.Series(550.0, index=index),              # 10 %
        "house_load_base_w": pd.Series(600.0, index=index),          # 20 %
        "house_load_candidate_ml_w": ml,
        "house_load_candidate_similar_days_w":
            pd.Series(650.0, index=index),                           # 30 %
    }
    for offset in (pd.Timedelta(hours=-12), pd.Timedelta(hours=6)):
        write_optimizer_forecast_archive(
            cfg.e3dc_rscp.history_db_path, day + offset, forecasts)
    return cfg, end


def test_heatmap_variants_expose_baselines_behind_the_final_forecast(tmp_path):
    """Die Heatmap zeigt das Endergebnis, bietet aber die Vorstufen an."""
    cfg, end = _variant_snapshots(tmp_path)
    result = forecast_analysis(cfg, days=30, target_day="2026-07-28", now=end)
    load = result["heatmaps"]["load"]

    assert load["wape_overall"] == 10.0          # unverändert: house_load_w
    variants = load["variants"]
    assert [v["key"] for v in variants] == [
        "final", "base", "ml", "similar_days"]
    assert [v["wape_overall"] for v in variants] == [10.0, 20.0, 5.0, 30.0]
    # Das Endergebnis ist hier schlechter als sein bester Kandidat - genau der
    # Fall, den der Vergleich sichtbar machen soll.
    assert variants[2]["wape_overall"] < variants[0]["wape_overall"]


def test_heatmap_variants_are_scored_on_identical_slots(tmp_path):
    """Fehlt EINER Variante ein Wert, fällt der Zielslot bei ALLEN raus.

    Sonst verglichen die Zahlen verschiedene Stichproben und wären wertlos.
    """
    cfg, end = _variant_snapshots(tmp_path, ml_gap_slots=48)
    result = forecast_analysis(cfg, days=30, target_day="2026-07-28", now=end)
    load = result["heatmaps"]["load"]
    variants = load["variants"]

    samples = {v["key"]: v["samples"] for v in variants}
    assert len(set(samples.values())) == 1, samples
    # ... und die Paarung kostet wirklich etwas: die ungepaarte Karte oben
    # bleibt vollständig, der Vergleich darunter schrumpft.
    assert 0 < variants[0]["samples"] < load["samples"]
    # Die WAPE-Werte bleiben exakt, weil auf den verbliebenen Slots dieselben
    # Konstanten stehen - eine verschobene Stichprobe würde sie verzerren.
    assert [v["wape_overall"] for v in variants] == [10.0, 20.0, 5.0, 30.0]


def test_heatmap_without_archived_baselines_has_no_variant_switch(tmp_path):
    """Ältere Historie ohne Vorstufen: kein Umschalter, keine leere Auswahl."""
    from ems.local_history import (write_house_load,
                                   write_optimizer_forecast_archive)

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    tz = cfg.general.timezone
    day = pd.Timestamp("2026-07-28", tz=tz)
    end = day + pd.DateOffset(days=1)
    index = pd.date_range(day, end, freq="15min", inclusive="left")
    write_house_load(cfg.e3dc_rscp.history_db_path, {
        stamp.tz_convert("UTC").isoformat(): 500.0 for stamp in index})
    write_optimizer_forecast_archive(
        cfg.e3dc_rscp.history_db_path, day - pd.Timedelta(hours=12),
        {"house_load_w": pd.Series(550.0, index=index)})

    load = forecast_analysis(
        cfg, days=30, target_day="2026-07-28", now=end)["heatmaps"]["load"]
    assert load["samples"] > 0
    assert "variants" not in load


# --------------------------------------------------------------------------- #
# Haltedauer eines Prognosewerts in der Tagesdarstellung
# --------------------------------------------------------------------------- #
def _stamps(minutes, n=6):
    idx = pd.date_range("2026-07-28 06:00", periods=n,
                        freq=f"{minutes}min", tz="Europe/Berlin")
    return pd.Series(range(n), index=idx, dtype="float64")


def test_hold_follows_the_source_cadence_not_a_fixed_number():
    """Solcast taktet viertelstuendlich, pvlib stuendlich.

    Ein fester 30-min-Deckel liess die stuendliche Quelle nur zwei von vier
    Slots belegen - im Diagramm sah das aus wie eine gestrichelte Linie.
    """
    from ems.observability import _source_hold_slots

    assert _source_hold_slots(_stamps(15), 15) == 0      # schon dicht
    assert _source_hold_slots(_stamps(30), 15) == 1
    assert _source_hold_slots(_stamps(60), 15) == 3      # deckt die Stunde ab


def test_hold_is_capped_so_real_outages_stay_visible():
    """Wer seltener als stuendlich liefert, bekommt eine sichtbare Luecke -
    eine durchgezogene Linie waere dort eine Behauptung ueber Daten, die es
    nicht gibt."""
    from ems.observability import MAX_HOLD_MINUTES, _source_hold_slots

    deckel = MAX_HOLD_MINUTES // 15 - 1
    assert _source_hold_slots(_stamps(180), 15) == deckel
    assert _source_hold_slots(_stamps(360), 15) == deckel


def test_hold_needs_two_points_to_infer_a_cadence():
    from ems.observability import _source_hold_slots

    assert _source_hold_slots(pd.Series(dtype="float64"), 15) == 0
    assert _source_hold_slots(_stamps(60, n=1), 15) == 0


def test_day_comparison_draws_an_hourly_source_without_holes(tmp_path):
    """Integrationsprobe: eine stuendlich archivierte pvlib-Quelle muss im
    Tagesverlauf jeden Slot belegen."""
    from ems.config import PvArray
    from ems.local_history import write_house_load, write_pv_forecast_archive

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    cfg.solcast.sources = []
    cfg.pv_model.arrays = [
        PvArray(name="Dach", kwp=10.0, tilt=25.0, azimuth=180.0)]
    tz = cfg.general.timezone
    day = pd.Timestamp("2026-07-28", tz=tz)
    end = day + pd.DateOffset(days=1)
    viertel = pd.date_range(day, end, freq="15min", inclusive="left")
    write_house_load(cfg.e3dc_rscp.history_db_path, {
        stamp.tz_convert("UTC").isoformat(): 500.0 for stamp in viertel})
    # NUR stuendliche Prognosewerte - so archiviert pvlib wirklich.
    stuendlich = pd.date_range(day, end, freq="60min", inclusive="left")
    write_pv_forecast_archive(
        cfg.e3dc_rscp.history_db_path, "pvmodel:Dach",
        day - pd.Timedelta(hours=12),
        {stamp.tz_convert("UTC").isoformat(): (1000.0, 800.0, 1200.0)
         for stamp in stuendlich})

    d = forecast_analysis(cfg, days=30, target_day="2026-07-28",
                          now=end)["day_comparison"]
    werte = d["pvlib_w"]
    assert len(werte) == 96
    assert all(v is not None for v in werte), "Loecher zwischen den Stundenwerten"
