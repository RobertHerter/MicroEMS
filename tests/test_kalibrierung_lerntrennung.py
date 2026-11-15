"""Lastkorrektur: Lern- und Prueftage des Kandidaten sind getrennt.

Die Folds der Validierung reichen in den Befoerderungsrahmen hinein. Das
Kandidatenprofil darf nur aus Paaren mit Zielzeit VOR dem Rahmen entstehen;
gepruefte und geschriebene Faktoren sind dieselben. Mit Forecaster-Spion
(kein echtes Training) auf synthetischer Historie.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yaml

import kalibrierung
from tests.test_kalibrierung_befoerderung import NOW, TZ, _cfg, _hist, _spion


def _reader(origin, end):
    idx = pd.date_range(origin, end, freq="15min", inclusive="left")
    return {"temp": pd.Series(10.0, index=idx),
            "pv": pd.Series(0.0, index=idx), "complete": True}


def _validierung(monkeypatch, hist, **kw):
    monkeypatch.setattr(kalibrierung, "LoadForecaster", _spion([]))
    return kalibrierung.validate_forecast_series(
        _cfg(), hist, None, None, NOW, folds=6, horizon_hours=48,
        min_train_days=60, archive_reader=_reader, **kw)


def test_pairs_inside_the_promotion_frame_do_not_touch_the_candidate(
        monkeypatch):
    """Zwei Historien, die sich NUR im Pruefrahmen unterscheiden: das
    Gesamtprofil der Validierung reagiert, der Kandidat nicht."""
    grenze = kalibrierung._promotion_origins(NOW, TZ, 14)[0]
    ruhig = _hist()
    verdoppelt = ruhig.copy()
    verdoppelt[verdoppelt.index >= grenze] *= 2.0

    a = _validierung(monkeypatch, ruhig, profile_before=grenze)
    b = _validierung(monkeypatch, verdoppelt, profile_before=grenze)
    # Der Test ist scharf: das bisherige Profil sieht die Prueftage ...
    assert a["hourly_correction"] != b["hourly_correction"]
    assert a["global_correction"] != b["global_correction"]
    # ... der Kandidat nicht.
    ka, kb = a["promotion_candidate"], b["promotion_candidate"]
    assert ka["hourly_correction"] == kb["hourly_correction"]
    assert ka["global_correction"] == kb["global_correction"]
    assert ka["pairs"] == kb["pairs"] > 0
    assert ka["pairs_excluded"] > 0
    assert pd.Timestamp(ka["trained_before"]) == grenze


def test_candidate_counts_only_archive_folds_before_the_frame(monkeypatch):
    """Sechs Wochenfolds: zwei liegen ganz im Rahmen, einer ragt hinein (unter
    90 % uebrig - zaehlt nicht, seine fruehen Paare werden trotzdem genutzt).
    Das Archivgewicht des Kandidaten sinkt entsprechend."""
    grenze = kalibrierung._promotion_origins(NOW, TZ, 14)[0]
    res = _validierung(monkeypatch, _hist(), profile_before=grenze)
    assert res["archive_folds"] == 6 and res["archive_weight"] == 1.0
    assert res["correction_profile_source"] == "issue_time_archive"
    kandidat = res["promotion_candidate"]
    assert kandidat["archive_folds_total"] == 6
    assert kandidat["archive_folds"] == 3
    assert kandidat["archive_min_folds"] == 6
    assert kandidat["archive_weight"] == 0.5
    assert kandidat["correction_profile_source"] == "hybrid"
    assert len(kandidat["hourly_correction"]) == 24


def test_without_cutoff_the_validation_result_is_unchanged(monkeypatch):
    ohne = _validierung(monkeypatch, _hist())
    mit = _validierung(
        monkeypatch, _hist(),
        profile_before=kalibrierung._promotion_origins(NOW, TZ, 14)[0])
    assert "promotion_candidate" not in ohne
    mit.pop("promotion_candidate")
    assert mit == ohne
    # ohne Schnitt liest die Befoerderung wie bisher das Gesamtprofil
    assert kalibrierung._load_candidate_source(ohne) is ohne
    assert kalibrierung._load_candidate_source(None) == {}


def test_no_pairs_before_the_frame_means_no_candidate(monkeypatch):
    """Liegt alles im Rahmen, gibt es keinen Kandidaten - mit klarer Logzeile,
    und die Befoerderung faellt NICHT auf das Gesamtprofil zurueck."""
    frueh = pd.Timestamp("2026-01-01", tz=TZ)
    res = _validierung(monkeypatch, _hist(), profile_before=frueh)
    kandidat = res["promotion_candidate"]
    assert "hourly_correction" not in kandidat
    assert kandidat["pairs"] == 0 and kandidat["pairs_excluded"] > 0
    assert kandidat["reason"] == "keine Lernpaare vor dem Prüfrahmen"
    quelle = kalibrierung._load_candidate_source(res)
    assert quelle is kandidat and not quelle.get("hourly_correction")
    text = kalibrierung._candidate_log_text(kandidat)
    assert "kein Kandidat, Champion bleibt" in text
    assert "01.01.2026" in text


def test_candidate_helper_filters_pairs_by_target_time():
    """Direkt am Helfer: ein Paar mit Zielzeit im Rahmen aendert nichts."""
    cfg = _cfg()
    grenze = pd.Timestamp("2026-10-18 00:00", tz=TZ)
    davor = pd.date_range(grenze - pd.Timedelta(days=20), periods=10 * 96,
                          freq="15min")
    danach = pd.date_range(grenze, periods=2 * 96, freq="15min")

    def paare(index, ist):
        return pd.DataFrame({"a": ist, "p": 400.0}, index=index)

    basis = {"ml": [paare(davor, 440.0)]}
    mit_rahmen = {"ml": [paare(davor, 440.0), paare(danach, 4000.0)]}
    leer = {"ml": []}
    a = kalibrierung._candidate_profile_before(cfg, basis, leer, 12, 173,
                                               grenze)
    b = kalibrierung._candidate_profile_before(cfg, mit_rahmen, leer, 12, 173,
                                               grenze)
    assert a["hourly_correction"] == b["hourly_correction"]
    assert a["global_correction"] == b["global_correction"] == 1.1
    assert a["pairs_excluded"] == 0 and b["pairs_excluded"] == 2 * 96
    assert b["correction_profile_source"] == "historical_bootstrap"
    assert "gelernt nur mit Zielzeiten vor 18.10.2026" in (
        kalibrierung._candidate_log_text(b))


def test_main_tests_and_writes_the_separated_candidate(monkeypatch, tmp_path,
                                                       capsys):
    """Durchstich durch ``main``: verglichen UND geschrieben wird das Profil
    aus ``promotion_candidate`` - nicht das Gesamtprofil der Validierung. Und
    nur die PV-Korrektur laeuft niveaubereinigt, die Last unskaliert."""
    import ems.calibration_competition as wettbewerb
    cfg = _cfg()
    cfg.calibration.pv_profile = str(tmp_path / "profil.yaml")
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    (tmp_path / "profil.yaml").write_text(yaml.safe_dump({
        "generated": "2026-10-25T02:00:00+01:00",
        "load_hourly": {h: 1.0 for h in range(24)}, "load_global": 1.0,
        "pv_global": 1.0, "pv_hour": {h: 1.0 for h in range(24)}}))
    (tmp_path / "bericht.yaml").write_text(yaml.safe_dump({
        "generated": "2026-10-25T02:00:00+01:00"}))

    index = pd.date_range(NOW.normalize() - pd.Timedelta(days=14),
                          periods=14 * 96, freq="15min")
    lokal = index.tz_convert(TZ)
    roh = pd.Series(500.0, index=index)
    last_rahmen = pd.DataFrame({"actual": roh * 1.10, "raw": roh})
    tags = (lokal.hour >= 8) & (lokal.hour < 16)
    pv_roh = pd.Series(1000.0, index=index)[tags]
    pv_rahmen = pd.DataFrame({"actual": pv_roh * 0.6, "raw": pv_roh})

    monkeypatch.setattr(kalibrierung, "load_config", lambda _p: cfg)
    monkeypatch.setattr(kalibrierung, "InfluxRepository",
                        lambda _c: type("R", (), {"close": lambda s: None})())
    monkeypatch.setattr(kalibrierung.pv_eval, "active_source",
                        lambda _c: "solcast")
    monkeypatch.setattr(kalibrierung.pv_eval, "source_groups",
                        lambda _c: {"solcast": ["x"]})
    monkeypatch.setattr(kalibrierung, "_issue_time_archive_reader",
                        lambda _c: None)
    monkeypatch.setattr(kalibrierung, "_intraday_load_pull", lambda _c: None)
    monkeypatch.setattr(kalibrierung, "_load_hist",
                        lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(kalibrierung, "_temp_hist", lambda *a, **k: None)
    monkeypatch.setattr(kalibrierung, "_pv_forecast_hist",
                        lambda *a, **k: (None, "test"))
    metrik = {"n": 500, "bias_W": 0.0, "mape_pct": 1.0, "nmae_pct": 1.0,
              "rmse_W": 1.0, "scale_actual_over_pred": 1.0, "corr": 1.0}
    monkeypatch.setattr(kalibrierung, "calibrate_pv", lambda *a, **k: {
        "metrics": dict(metrik), "fit_metrics": dict(metrik),
        "hourly": {h: 0.9 for h in range(24)}, "monthly": {},
        "month_hour": {}, "current_scale": 1.0, "suggested_scale": 1.0,
        "forecast_source": "t", "actual_source": "t",
        "promotion_samples": len(pv_rahmen), "_promotion_frame": pv_rahmen})
    monkeypatch.setattr(kalibrierung, "calibrate_load", lambda *a, **k: {
        "metrics": dict(metrik), "hourly": {}, "daytype": {},
        "suggested_correction_factor": 1.0,
        "promotion_samples": len(last_rahmen),
        "_promotion_frame": last_rahmen})
    gesehen = {}

    def validierung(*a, profile_before=None, **k):
        gesehen["profile_before"] = profile_before
        return {
            "methods": {}, "folds": 12, "horizon_hours": 48,
            "empfehlung": "ml", "global_correction": 1.5,
            "hourly_correction": {h: 1.5 for h in range(24)},
            "correction_profile_source": "issue_time_archive",
            "archive_folds": 12, "archive_min_folds": 6,
            "archive_weight": 1.0,
            "promotion_candidate": {
                "trained_before": pd.Timestamp(profile_before).isoformat(),
                "pairs": 1900, "pairs_excluded": 400, "archive_folds": 10,
                "archive_folds_total": 12, "archive_min_folds": 6,
                "archive_weight": 1.0, "global_correction": 1.1,
                "correction_profile_source": "issue_time_archive",
                "hourly_correction": {h: 1.1 for h in range(24)}}}

    monkeypatch.setattr(kalibrierung, "validate_forecast_series", validierung)
    monkeypatch.setattr(kalibrierung, "_print_validation", lambda _r: None)
    aufrufe = []
    echt = wettbewerb.compare_point_forecasts

    def spion(actual, champion, challenger, tz, **kw):
        aufrufe.append({"challenger": challenger, "actual": actual, "kw": kw})
        return echt(actual, champion, challenger, tz, **kw)

    monkeypatch.setattr(wettbewerb, "compare_point_forecasts", spion)
    monkeypatch.setattr(pd.Timestamp, "now",
                        classmethod(lambda cls, tz=None: NOW))
    monkeypatch.setattr(sys, "argv", [
        "kalibrierung", "--config", "c.yaml", "--no-band",
        "--output", str(tmp_path / "bericht.yaml")])
    monkeypatch.chdir(tmp_path)
    kalibrierung.main()

    # Schnitt = Beginn des Befoerderungsrahmens
    assert gesehen["profile_before"] == kalibrierung._promotion_origins(
        NOW, TZ, cfg.calibration.promotion_days)[0]
    pv_aufruf, last_aufruf = aufrufe
    # (d) nur die PV-Korrektur laeuft niveaubereinigt
    assert pv_aufruf["kw"].get("level_adjusted") is True
    assert pv_aufruf["kw"]["level_method"] == "per_day"
    assert "level_adjusted" not in last_aufruf["kw"]
    assert "max_raw_wape_increase_pct" not in last_aufruf["kw"]
    # geprueft wurde der getrennte Kandidat (1,1), nicht das Gesamtprofil (1,5)
    assert np.allclose(last_aufruf["challenger"].values, 550.0)

    profil = yaml.safe_load((tmp_path / "profil.yaml").read_text())
    bericht = yaml.safe_load((tmp_path / "bericht.yaml").read_text())
    last = bericht["calibration_competition"]["signals"]["load_correction"]
    assert last["promote"] is True and "level_adjusted" not in last
    assert pd.Timestamp(last["candidate_trained_before"]) == gesehen[
        "profile_before"]
    # ... und genau dieses gepruefte Profil steht danach im Betrieb
    assert set(profil["load_hourly"].values()) == {1.1}
    assert profil["load_global"] == 1.1
    pv = bericht["calibration_competition"]["signals"]["pv_correction"]
    assert pv["comparison"] == "level_adjusted"
    assert {"raw_improvement_percent", "level_adjusted",
            "bias_increase_w"} <= set(pv)
    assert bericht["forecast_validation"]["promotion_candidate"][
        "pairs_excluded"] == 400
    aus = capsys.readouterr().out
    assert "Lastkorrektur-Kandidat: gelernt nur mit Zielzeiten vor" in aus
    assert "niveaubereinigt" in aus and "unskaliert" in aus
