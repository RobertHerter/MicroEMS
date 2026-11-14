"""Tests für Gewichtung und Unsicherheitsband des PV-lib-Ensembles."""
from __future__ import annotations

import pandas as pd

from ems import local_history, pv_ensemble


def _seed_actual(db, target, actual):
    con = local_history._con(db)
    con.execute(
        "INSERT OR REPLACE INTO actuals(ts, pv_w) VALUES(?,?)",
        (target.tz_convert("UTC").isoformat(), float(actual)))
    con.commit()
    con.close()


def test_learns_horizon_weight_from_rolling_origin_errors(tmp_path):
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    sources = {"good": ["member-good"], "bad": ["member-bad"]}
    for day in range(1, 21):
        target = now - pd.Timedelta(days=day) + pd.Timedelta(hours=12)
        issue = target - pd.Timedelta(hours=3)
        _seed_actual(db, target, 1000.0)
        local_history.write_pv_forecast_archive(
            db, "member-good", issue,
            {target.isoformat(): (1050.0, 900.0, 1200.0)})
        local_history.write_pv_forecast_archive(
            db, "member-bad", issue,
            {target.isoformat(): (1800.0, 1200.0, 2200.0)})

    learned = pv_ensemble.learn_weights(
        db, sources, now, lookback_days=30, min_samples=8,
        horizon_hours=[6, 24, 48], min_weight=0.05)
    short = learned["0-6h"]
    assert short["learned"] and short["n"] == 20
    assert short["weights"]["good"] > short["weights"]["bad"]
    assert short["weights"]["bad"] >= 0.05
    # Kein Training im 6-24-h-Bucket -> neutrale Gewichte.
    assert learned["6-24h"]["weights"] == {"good": 0.5, "bad": 0.5}


def test_residual_quantiles_are_lead_specific(tmp_path):
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    for day in range(1, 21):
        target = now - pd.Timedelta(days=day) + pd.Timedelta(hours=12)
        issue = target - pd.Timedelta(hours=3)
        actual = 800.0 if day <= 10 else 1200.0
        _seed_actual(db, target, actual)
        local_history.write_pv_forecast_archive(
            db, "pvmodel:A", issue,
            {target.isoformat(): (1000.0, 700.0, 1300.0)})

    bands = pv_ensemble.residual_quantiles(
        db, ["pvmodel:A"], now, lookback_days=30, min_samples=8,
        horizon_hours=[6, 24], fallback_low=0.65, fallback_high=1.15)
    assert bands["0-6h"]["learned"] and bands["0-6h"]["n"] == 20
    assert 0.79 <= bands["0-6h"]["q10_ratio"] <= 0.81
    assert 1.19 <= bands["0-6h"]["q90_ratio"] <= 1.21
    assert not bands["6-24h"]["learned"]


def test_residual_quantiles_condition_on_season_and_power(tmp_path):
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    base = pd.Timestamp("2026-07-18 00:00", tz="UTC")
    for slot in range(72):
        target = base + pd.Timedelta(minutes=15 * slot)
        issue = target - pd.Timedelta(hours=3)
        high = slot % 2 == 1
        predicted = 1500.0 if high else 300.0
        actual = predicted if high else predicted * 0.5
        _seed_actual(db, target, actual)
        local_history.write_pv_forecast_archive(
            db, "pvmodel:A", issue,
            {target.isoformat(): (predicted, 0.0, 0.0)})

    bands = pv_ensemble.residual_quantiles(
        db, ["pvmodel:A"], now, lookback_days=7, min_samples=16,
        horizon_hours=[6], fallback_low=0.65, fallback_high=1.15,
        timezone="Europe/Berlin")
    short = bands["0-6h"]
    assert short["learned"]
    assert short["conditions"]["power:high"]["q10_ratio"] > 0.9
    assert "season:2|power:high" in short["conditions"]

    issue = pd.Timestamp("2026-07-20 09:00", tz="UTC")
    target = (issue + pd.Timedelta(hours=3)).isoformat()
    outputs = {"model": {
        "pvmodel:A": {target: (1500.0, 0.0, 0.0)}}}
    weights = {
        "0-6h": {"weights": {"model": 1.0}, "n": 72, "learned": True},
        "6h+": {"weights": {"model": 1.0}, "n": 0, "learned": False},
    }
    combined, diagnostics = pv_ensemble.combine(
        outputs, weights, bands, issue, [6])
    _, p10, _ = combined["pvmodel:A"][target]
    assert p10 > 1350.0
    assert diagnostics["condition_slots_by_bucket"]["0-6h"][
        "season:2|power:high"] == 1


def test_combines_model_spread_and_empirical_residuals():
    issue = pd.Timestamp("2026-07-20 09:00", tz="UTC")
    target = (issue + pd.Timedelta(hours=3)).isoformat()
    outputs = {
        "good": {"pvmodel:A": {target: (1000.0, 0.0, 0.0)}},
        "high": {"pvmodel:A": {target: (2000.0, 0.0, 0.0)}},
    }
    weights = {
        "0-6h": {"weights": {"good": 0.75, "high": 0.25},
                 "n": 100, "learned": True},
        "6h+": {"weights": {"good": 0.5, "high": 0.5},
                "n": 0, "learned": False},
    }
    residuals = {
        "0-6h": {"q10_ratio": 0.8, "q90_ratio": 1.2,
                 "n": 100, "learned": True},
        "6h+": {"q10_ratio": 0.65, "q90_ratio": 1.15,
                "n": 0, "learned": False},
    }
    combined, diagnostics = pv_ensemble.combine(
        outputs, weights, residuals, issue, [6])
    point, p10, p90 = combined["pvmodel:A"][target]
    assert point == 1250.0
    assert p10 == 1000.0       # Modellstreuung konservativer als Residuum
    assert p90 == 2000.0
    assert p10 <= point <= p90
    assert diagnostics["slots_by_bucket"]["0-6h"] == 1


def test_member_source_keeps_aggregate_ids_compatible():
    assert pv_ensemble.member_source(
        "dwd_icon", "pvmodel:Ost") == "pvmodel-member:dwd_icon:Ost"


# --------------------------------------------------------------------------- #
# Stundenprognose gegen das Ist-MITTEL derselben Stunde
# --------------------------------------------------------------------------- #
def _seed_hourly(db, now, days=12, slots=(0, 15, 30, 45), rampe=True):
    """Je Tag eine Ausgabe mit zwei Stundenmarken (11 und 12 Uhr, Mittel
    1000 W) und 15-min-Ist: innerhalb der Stunde steigend 700/900/1100/1300 W
    (Mittel 1000 W)."""
    stufen = {0: 700.0, 15: 900.0, 30: 1100.0, 45: 1300.0}
    for day in range(1, days + 1):
        basis = now - pd.Timedelta(days=day)
        ziele = [basis + pd.Timedelta(hours=11), basis + pd.Timedelta(hours=12)]
        issue = ziele[0] - pd.Timedelta(hours=3)
        local_history.write_pv_forecast_archive(
            db, "pvmodel:A", issue,
            {z.isoformat(): (1000.0, 700.0, 1300.0) for z in ziele})
        local_history.write_pv_forecast_archive(
            db, "member-good", issue,
            {z.isoformat(): (1000.0, 700.0, 1300.0) for z in ziele})
        local_history.write_pv_forecast_archive(
            db, "member-bad", issue,
            {z.isoformat(): (700.0, 500.0, 900.0) for z in ziele})
        for ziel in ziele:
            for minute in slots:
                _seed_actual(db, ziel + pd.Timedelta(minutes=minute),
                             stufen[minute] if rampe else 1000.0)


def test_hourly_forecast_is_paired_with_the_hourly_actual_mean(tmp_path):
    """Die Prognose ist ein Stundenmittel. Gepaart mit dem EINEN Ist-Slot auf
    der Marke (700 W, die erste Viertelstunde) schiene sie 43 % zu hoch -
    tatsaechlich trifft sie das Stundenmittel (1000 W) genau."""
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    _seed_hourly(db, now)
    bands = pv_ensemble.residual_quantiles(
        db, ["pvmodel:A"], now, lookback_days=30, min_samples=8,
        horizon_hours=[6, 24], fallback_low=0.65, fallback_high=1.15,
        cache_s=0)
    band = bands["0-6h"]
    assert band["learned"] and band["n"] == 24
    assert band["q10_ratio"] == 1.0 and band["q90_ratio"] == 1.0

    learned = pv_ensemble.learn_weights(
        db, {"good": ["member-good"], "bad": ["member-bad"]}, now,
        lookback_days=30, min_samples=8, horizon_hours=[6, 24], cache_s=0)
    # gegen den Slot auf der Marke (700 W) haette "bad" (700 W) gewonnen
    assert learned["0-6h"]["mae_w"]["good"] == 0.0
    assert learned["0-6h"]["mae_w"]["bad"] == 300.0
    assert learned["0-6h"]["weights"]["good"] > learned["0-6h"]["weights"]["bad"]


def test_hours_with_too_few_actual_slots_are_skipped(tmp_path):
    """Nur 2 von 4 Ist-Slots der Stunde: kein belastbares Stundenmittel."""
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    _seed_hourly(db, now, slots=(0, 15))
    bands = pv_ensemble.residual_quantiles(
        db, ["pvmodel:A"], now, lookback_days=30, min_samples=8,
        horizon_hours=[6, 24], fallback_low=0.65, fallback_high=1.15,
        cache_s=0)
    assert bands["0-6h"]["n"] == 0 and not bands["0-6h"]["learned"]


# --------------------------------------------------------------------------- #
# Lernergebnis je Prozess merken
# --------------------------------------------------------------------------- #
def test_learning_is_cached_per_database_and_parameters(tmp_path, monkeypatch):
    """Der Betrieb fragt alle 15 min; das Archiv wird hoechstens stuendlich
    neu geladen."""
    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    _seed_hourly(db, now)
    pv_ensemble.clear_learning_cache()
    laeufe = {"n": 0}
    echt = pv_ensemble._archive_groups

    def gezaehlt(*a, **k):
        laeufe["n"] += 1
        return echt(*a, **k)

    monkeypatch.setattr(pv_ensemble, "_archive_groups", gezaehlt)
    modelle = {"good": ["member-good"], "bad": ["member-bad"]}
    args = dict(lookback_days=30, min_samples=8, horizon_hours=[6, 24])
    erst = pv_ensemble.learn_weights(db, modelle, now, **args)
    zweit = pv_ensemble.learn_weights(
        db, modelle, now + pd.Timedelta(minutes=15), **args)
    assert laeufe["n"] == 1 and zweit == erst
    # der Aufrufer bekommt eine Kopie - Aendern trifft den Cache nicht
    zweit["0-6h"]["weights"]["good"] = -1.0
    assert pv_ensemble.learn_weights(db, modelle, now, **args) == erst
    # andere Parameter, andere Funktion, andere Datenbank: eigene Eintraege
    pv_ensemble.learn_weights(db, modelle, now, lookback_days=20,
                              min_samples=8, horizon_hours=[6, 24])
    assert laeufe["n"] == 2
    kw = dict(lookback_days=30, min_samples=8, horizon_hours=[6, 24],
              fallback_low=0.65, fallback_high=1.15)
    pv_ensemble.residual_quantiles(db, ["pvmodel:A"], now, **kw)
    pv_ensemble.residual_quantiles(db, ["pvmodel:A"], now, **kw)
    assert laeufe["n"] == 3
    # abgelaufen -> neu rechnen
    uhr = {"t": pv_ensemble.time.monotonic()}
    monkeypatch.setattr(pv_ensemble.time, "monotonic", lambda: uhr["t"])
    uhr["t"] += pv_ensemble.LERN_CACHE_S + 1.0
    pv_ensemble.learn_weights(db, modelle, now, **args)
    assert laeufe["n"] == 4
    pv_ensemble.clear_learning_cache()


def test_cache_is_thread_safe(tmp_path, monkeypatch):
    import threading

    db = str(tmp_path / "history.sqlite")
    now = pd.Timestamp("2026-07-20 00:00", tz="UTC")
    _seed_hourly(db, now, days=9)
    pv_ensemble.clear_learning_cache()
    laeufe = {"n": 0}
    echt = pv_ensemble._archive_groups

    def gezaehlt(*a, **k):
        laeufe["n"] += 1
        return echt(*a, **k)

    monkeypatch.setattr(pv_ensemble, "_archive_groups", gezaehlt)
    ergebnisse = []

    def lauf():
        ergebnisse.append(pv_ensemble.learn_weights(
            db, {"good": ["member-good"], "bad": ["member-bad"]}, now,
            lookback_days=30, min_samples=8, horizon_hours=[6, 24]))

    threads = [threading.Thread(target=lauf) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert laeufe["n"] == 1
    assert len(ergebnisse) == 6 and all(e == ergebnisse[0] for e in ergebnisse)
    pv_ensemble.clear_learning_cache()
