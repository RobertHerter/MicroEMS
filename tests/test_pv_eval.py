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
    assert g["pvlib"]["n"] == g["solcast"]["n"]
    assert g["pvlib"]["actual_kwh"] == g["solcast"]["actual_kwh"]
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


def test_auto_selection_reason_names_active_source_when_it_is_better(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    _seed(db, solcast_bias=1.03, pvlib_bias=1.08)
    cfg = _cfg(db)
    cfg.pv_source_selection.enabled = True
    cfg.pv_source_selection.lookback_days = 6
    cfg.pv_source_selection.min_samples = 50
    cfg.pv_source_selection.min_improvement_percent = 2.0
    selected = pv_eval.select_source(cfg, NOW)
    assert selected["selected"] == "solcast"
    assert "solcast bleibt aktiv und ist" in selected["reason"]
    assert "besser als pvlib" in selected["reason"]
    assert "-5.00" not in selected["reason"]


def test_auto_selection_reason_explains_marginal_challenger(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    _seed(db, solcast_bias=1.03, pvlib_bias=1.02)
    cfg = _cfg(db)
    cfg.pv_source_selection.enabled = True
    cfg.pv_source_selection.lookback_days = 6
    cfg.pv_source_selection.min_samples = 50
    cfg.pv_source_selection.min_improvement_percent = 2.0
    selected = pv_eval.select_source(cfg, NOW)
    assert selected["selected"] == "solcast"
    assert "pvlib ist nur 1.00 WAPE-Punkte besser" in selected["reason"]
    assert "Mindestvorsprung 2.00" in selected["reason"]


def test_decision_weighting_prefers_source_in_ems_critical_slots(tmp_path):
    """Eine Quelle darf trotz minimal besserer Energie-WAPE verlieren, wenn
    ihre Fehler gerade an Negativpreis-/Netzübergangs-Slots liegen."""
    db = str(tmp_path / "hist.sqlite")
    cfg = _cfg(db)
    cfg.pv_source_selection.enabled = True
    cfg.pv_source_selection.lookback_days = 6
    cfg.pv_source_selection.min_samples = 50
    cfg.pv_source_selection.min_improvement_percent = 1.0
    grid = _daytime_grid(6)
    actual = np.full(len(grid), 4000.0)
    critical = np.arange(len(grid)) % 10 == 0
    # Solcast insgesamt leicht besser, aber in den kritischen 10 % deutlich
    # daneben. pvlib macht seinen kleinen Fehler nur in normalen Slots.
    solcast = actual + np.where(critical, 440.0, 0.0)
    pvlib = actual + np.where(critical, 0.0, 50.0)
    con = local_history._con(db)
    for ts, measured, is_critical in zip(grid, actual, critical):
        utc = ts.tz_convert("UTC").isoformat()
        con.execute("INSERT OR REPLACE INTO actuals(ts,pv_w) VALUES(?,?)",
                    (utc, float(measured)))
        con.execute("INSERT OR REPLACE INTO house_load(ts,w) VALUES(?,?)",
                    (utc, float(measured if is_critical else 0.0)))
        con.execute("INSERT OR REPLACE INTO spot_price(ts,ct) VALUES(?,?)",
                    (utc, -10.0 if is_critical else 5.0))
    con.commit()
    con.close()
    for source, values in (("sc1", solcast), ("pvmodel:Ost", pvlib)):
        for ts, value in zip(grid, values):
            local_history.write_pv_forecast_archive(
                db, source, ts - pd.Timedelta(hours=1),
                {ts.tz_convert("UTC").isoformat():
                 (float(value), float(value) * 0.8, float(value) * 1.2)})

    groups = pv_eval._common_archive_metrics(cfg, 6, NOW)
    assert groups["solcast"]["wape_pct"] < groups["pvlib"]["wape_pct"]
    assert (groups["pvlib"]["decision_score_pct"]
            < groups["solcast"]["decision_score_pct"])
    assert groups["solcast"]["mode"] == "decision_weighted"
    assert groups["solcast"]["decision_slots"] > 0
    selected = pv_eval.select_source(cfg, NOW)
    assert selected["selected"] == "pvlib"
    assert "Entscheidungsscore-Punkte besser" in selected["reason"]


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


def _profil(tmp_path, cfg, inhalt):
    import yaml
    pfad = tmp_path / "pv_profile.yaml"
    pfad.write_text(yaml.safe_dump(inhalt), encoding="utf-8")
    cfg.calibration.enabled = True
    cfg.calibration.pv_profile = str(pfad)
    return cfg


def test_each_source_is_measured_with_its_own_profile(tmp_path):
    """Der Quellenvergleich entscheidet die automatische Umschaltung. Trifft
    eine korrigierte auf eine rohe Prognose, misst er die Kalibrierung statt
    der Prognosequalitaet."""
    db = str(tmp_path / "hist.sqlite")
    _seed(db, solcast_bias=1.35, pvlib_bias=1.30)
    cfg = _profil(tmp_path, _cfg(db), {
        "pv_global": round(1 / 1.35, 4),
        "pv_sources": {"solcast": {"pv_global": round(1 / 1.35, 4)},
                       "pvlib": {"pv_global": round(1 / 1.30, 4)}}})
    g = pv_eval.compare_sources(cfg, lookback_days=6, now=NOW)["groups"]
    assert g["solcast"]["wape_pct"] < 2.0
    # ohne eigenes pvlib-Profil blieben hier die rohen ~30 % stehen
    assert g["pvlib"]["wape_pct"] < 2.0


def test_a_flat_profile_is_never_applied_to_the_other_source(tmp_path):
    """Altes Profilformat: die flachen Faktoren gehoeren der produktiven
    Quelle. Auf pvlib angewandt waeren sie schlechter als gar nichts."""
    db = str(tmp_path / "hist.sqlite")
    _seed(db, solcast_bias=1.35, pvlib_bias=1.30)
    cfg = _profil(tmp_path, _cfg(db), {"pv_global": round(1 / 1.35, 4)})
    g = pv_eval.compare_sources(cfg, lookback_days=6, now=NOW)["groups"]
    assert g["solcast"]["wape_pct"] < 2.0
    assert g["pvlib"]["wape_pct"] > 20.0


def test_lead_bound_compares_identical_timestamp_formats(tmp_path):
    """Die Lead-Schranke wird als STRING gegen issue_ts geprueft. Rendert man
    sie mit datetime(), entsteht "2026-05-24 10:00:00" gegen
    "2026-05-24T10:00:00+00:00" - das "T" gewinnt gegen das Leerzeichen, und
    jede Ausgabe mit demselben Datum fiel heraus. Die wirksame Vorlaufzeit war
    dadurch bis zu einen Tag groesser als angefordert."""
    db = str(tmp_path / "hist.sqlite")
    ziel = pd.Timestamp("2026-05-26 12:00", tz=TZ)
    for lead in (6.0, 24.0, 48.0, 72.0):
        local_history.write_pv_forecast_archive(
            db, "sc1", (ziel - pd.Timedelta(hours=lead)).tz_convert("UTC"),
            {ziel.tz_convert("UTC").isoformat(): (1000.0 + lead, 0.0, 0.0)})

    for lead in (6.0, 24.0, 48.0):
        s = pv_eval.read_group_asof(
            db, ["sc1"], ziel, ziel + pd.Timedelta(minutes=15), TZ, 15,
            "pv", lead)
        assert len(s) == 1
        # genau die Ausgabe mit dieser Vorlaufzeit, nicht die vom Vortag
        assert float(s.iloc[0]) == 1000.0 + lead, (
            f"Lead {lead} h liefert {float(s.iloc[0])} statt {1000.0 + lead}")


# --------------------------------------------------------------------------- #
# Archiv zwischen den Marken (seit 12.11.2026)
# --------------------------------------------------------------------------- #
def _utc(stamp):
    return pd.Timestamp(stamp, tz="UTC")


def _ausgabe(db, quelle, issue, werte):
    local_history.write_pv_forecast_archive(
        db, quelle, _utc(issue),
        {_utc(t).isoformat(): (v, v, v) for t, v in werte.items()})


def _lesen(db, von, bis, lead=0.0):
    s = pv_eval.read_group_asof(db, ["pvmodel:X"], _utc(von).tz_convert(TZ),
                                _utc(bis).tz_convert(TZ), TZ, 15, "pv", lead)
    s.index = s.index.tz_convert("UTC")
    return s


def test_archive_read_interpolates_without_looking_ahead(tmp_path):
    """Zwischen den Stundenmarken wird interpoliert. Die rechte Stuetzstelle
    stammt aus einer Prognose, die VOR dem Slot erstellt war - nicht aus der
    juengsten fuer ihre eigene Marke (die kam hier erst um 10:40)."""
    db = str(tmp_path / "hist.sqlite")
    _ausgabe(db, "pvmodel:X", "2026-06-10 09:00",
             {"2026-06-10 10:00": 100.0, "2026-06-10 11:00": 200.0,
              "2026-06-10 12:00": 300.0})
    _ausgabe(db, "pvmodel:X", "2026-06-10 10:40",
             {"2026-06-10 11:00": 900.0, "2026-06-10 12:00": 950.0})
    s = _lesen(db, "2026-06-10 10:00", "2026-06-10 12:00")
    assert s[_utc("2026-06-10 10:00")] == 100.0        # auf der Marke wie bisher
    assert s[_utc("2026-06-10 10:15")] == 125.0        # 3/4 * 100 + 1/4 * 200
    assert s[_utc("2026-06-10 11:00")] == 900.0
    assert s[_utc("2026-06-10 11:15")] == 912.5        # 3/4 * 900 + 1/4 * 950


def test_archive_read_falls_back_without_a_strict_right_point(tmp_path):
    """Gibt es fuer die rechte Marke keine Prognose von vor dem Slot, gilt
    ersatzweise die juengste fuer diese Marke (hoechstens ein Intervall
    Vorgriff) - besser als eine Luecke."""
    db = str(tmp_path / "hist.sqlite")
    _ausgabe(db, "pvmodel:X", "2026-06-10 08:00", {"2026-06-10 10:00": 100.0})
    _ausgabe(db, "pvmodel:X", "2026-06-10 10:40",
             {"2026-06-10 11:00": 900.0, "2026-06-10 12:00": 950.0})
    s = _lesen(db, "2026-06-10 10:00", "2026-06-10 11:00")
    assert s[_utc("2026-06-10 10:15")] == 0.75 * 100.0 + 0.25 * 900.0


def test_archive_read_holds_the_interval_mean_at_the_end(tmp_path):
    """Nach der letzten Marke liegt der Slot noch in deren Stunde."""
    db = str(tmp_path / "hist.sqlite")
    _ausgabe(db, "pvmodel:X", "2026-06-10 08:00",
             {"2026-06-10 10:00": 100.0, "2026-06-10 11:00": 200.0})
    s = _lesen(db, "2026-06-10 10:00", "2026-06-10 12:00")
    assert list(s[_utc("2026-06-10 11:00"):]) == [200.0] * 4


def test_archive_read_of_quarter_hour_sources_is_unchanged(tmp_path):
    """Quellen im Slot-Raster brauchen keine Interpolation."""
    db = str(tmp_path / "hist.sqlite")
    _ausgabe(db, "pvmodel:X", "2026-06-10 08:00",
             {t: float(i) for i, t in enumerate(pd.date_range(
                 "2026-06-10 10:00", periods=4, freq="15min"))})
    s = _lesen(db, "2026-06-10 10:00", "2026-06-10 11:00")
    assert list(s) == [0.0, 1.0, 2.0, 3.0]
