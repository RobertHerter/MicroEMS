"""Tests für die PV-Quellenauswertung (pvlib vs. Solcast, Bandkalibrierung)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems import local_history, pv_eval
from ems.config import PvArray, SolcastSource
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
NOW = pd.Timestamp("2026-06-15 00:00", tz=TZ)


def _cfg(db):
    cfg = make_config()
    cfg.general.timezone = TZ
    cfg.general.slot_minutes = 15
    cfg.e3dc_rscp.history_db_path = db
    cfg.solcast.enabled = True
    cfg.solcast.sources = [SolcastSource(api_key="k", resource_id="sc1")]
    cfg.pv_model.shadow = True
    cfg.pv_model.arrays = [PvArray(name="Ost", kwp=5.0, tilt=20.0, azimuth=90.0)]
    return cfg


def _daytime_grid(days):
    start = (NOW - pd.Timedelta(days=days)).floor("15min")
    grid = pd.date_range(start, NOW, freq="15min", tz=TZ, inclusive="left")
    return grid[(grid.hour >= 8) & (grid.hour < 18)]


def _actual_pv(grid):
    hour = grid.hour + grid.minute / 60.0
    return np.clip(4000.0 * np.exp(-((hour - 13.0) ** 2) / 8.0), 0.0, None)


def _seed(db, days=6, solcast_bias=1.35, pvlib_bias=1.03):
    """actuals + Archiv beider Quellen füllen. pvlib nahe am Ist, Solcast
    deutlich daneben (Skalierungsfehler)."""
    grid = _daytime_grid(days)
    actual = _actual_pv(grid)
    # Ist-PV
    con = local_history._con(db)
    for ts, w in zip(grid, actual):
        con.execute("INSERT OR REPLACE INTO actuals(ts, pv_w) VALUES(?, ?)",
                    (ts.tz_convert("UTC").isoformat(), float(w)))
    con.commit()
    con.close()
    # Archiv: je Zielslot ein Snapshot, issue 1 h vor dem Slot
    def arch(source, values):
        for ts, w in zip(grid, values):
            issue = (ts - pd.Timedelta(hours=1)).tz_convert("UTC")
            local_history.write_pv_forecast_archive(
                db, source, issue, {ts.tz_convert("UTC").isoformat():
                                    (float(w), float(w) * 0.65, float(w) * 1.15)})
    arch("sc1", actual * solcast_bias)
    arch("pvmodel:Ost", actual * pvlib_bias)
    return grid, actual


def test_compare_recommends_more_accurate_source(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    _seed(db)
    cfg = _cfg(db)
    res = pv_eval.compare_sources(cfg, lookback_days=6, now=NOW)
    g = res["groups"]
    assert g["pvlib"]["method"] == "archive" and g["pvlib"]["n"] > 50
    assert g["solcast"]["n"] > 50
    # pvlib (3 % Bias) muss besser sein als Solcast (35 % Bias)
    assert g["pvlib"]["wape_pct"] < g["solcast"]["wape_pct"]
    rec = res["recommendation"]
    assert rec is not None and rec["better"] == "pvlib" and rec["meaningful"]


def test_auto_selection_switches_only_from_real_archives(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    _seed(db)
    cfg = _cfg(db)
    cfg.pv_source_selection.enabled = True
    cfg.pv_source_selection.lookback_days = 6
    cfg.pv_source_selection.min_samples = 50
    cfg.pv_source_selection.min_improvement_percent = 2.0
    selected = pv_eval.select_source(cfg, NOW)
    assert selected["selected"] == "pvlib"
    assert "WAPE-Punkte besser" in selected["reason"]


def test_auto_selection_ignores_optimistic_cache(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    grid, actual = _seed(db)
    # pvlib-Archiv entfernen; ein perfekter Live-Cache darf nicht umschalten.
    con = local_history._con(db)
    con.execute("DELETE FROM pv_forecast_archive WHERE source='pvmodel:Ost'")
    for ts, value in zip(grid, actual):
        u = ts.tz_convert("UTC").isoformat()
        con.execute("INSERT OR REPLACE INTO pv_forecast VALUES(?,?,?,?,?)",
                    ("pvmodel:Ost", u, float(value), float(value), float(value)))
    con.commit()
    con.close()
    cfg = _cfg(db)
    cfg.pv_source_selection.enabled = True
    cfg.pv_source_selection.lookback_days = 6
    cfg.pv_source_selection.min_samples = 50
    selected = pv_eval.select_source(cfg, NOW)
    assert selected["selected"] == "solcast"
    assert "warte auf Prognosearchive" in selected["reason"]


def test_compare_falls_back_to_cache_without_archive(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    grid = _daytime_grid(6)
    actual = _actual_pv(grid)
    con = local_history._con(db)
    for ts, w in zip(grid, actual):
        u = ts.tz_convert("UTC").isoformat()
        con.execute("INSERT OR REPLACE INTO actuals(ts, pv_w) VALUES(?, ?)",
                    (u, float(w)))
        # nur Live-Cache, KEIN Archiv
        con.execute("INSERT OR REPLACE INTO pv_forecast(source, ts, pv_w, pv10_w,"
                    " pv90_w) VALUES('pvmodel:Ost', ?, ?, ?, ?)",
                    (u, float(w) * 1.02, float(w) * 0.7, float(w) * 1.2))
    con.commit()
    con.close()
    cfg = _cfg(db)
    cfg.solcast.enabled = False           # nur pvlib
    res = pv_eval.compare_sources(cfg, lookback_days=6, now=NOW)
    assert res["groups"]["pvlib"]["method"] == "cache"
    assert res["groups"]["pvlib"]["n"] > 50


def test_calibrate_band_from_residuals(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    grid, actual = _seed(db, pvlib_bias=1.0)
    cfg = _cfg(db)
    band = pv_eval.calibrate_band(cfg, lookback_days=6, now=NOW, min_pv_w=100.0)
    assert band["method"] == "archive" and not band.get("insufficient")
    # empfohlene Werte sind endlich und in sinnvollen Grenzen
    assert 0.0 <= band["recommended_p10_uncertainty"] <= 0.95
    assert 0.0 <= band["recommended_p90_uncertainty"] <= 3.0
    # und weichen vom Heuristik-Festwert ab (Ist == Prognose -> Band ~0)
    assert band["recommended_p10_uncertainty"] < cfg.pv_model.p10_uncertainty
