"""Ausduennen des Prognosearchivs: was verschwinden darf und was nicht."""
from __future__ import annotations

import pandas as pd

from ems import pv_eval
from ems.archive_thinning import (lead_buckets, thin_pv_forecast_archive)
from ems.local_history import _con, write_pv_forecast_archive
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
NOW = pd.Timestamp("2026-08-14 12:00", tz=TZ)
BUCKETS = [0.0, 6.0, 24.0, 48.0]
LEADS = [0.25, 6.0, 24.0, 48.0, 72.0]
AGGREGAT = ["sc1", "pvmodel:Ost"]
MITGLIED = "pvmodel-member:dwd_icon:Ost"


def _saat(db, ziele, quellen=None):
    """Je Ziel und Quelle mehrere Ausgabezeitpunkte schreiben."""
    for quelle in (quellen or AGGREGAT + [MITGLIED]):
        for ziel in ziele:
            for lead in LEADS:
                issue = (ziel - pd.Timedelta(hours=lead)).tz_convert("UTC")
                write_pv_forecast_archive(
                    db, quelle, issue,
                    {ziel.tz_convert("UTC").isoformat():
                     (1000.0 + lead, 600.0, 1400.0)})


def _zeilen(db, quelle, von, bis) -> int:
    con = _con(db)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM pv_forecast_archive WHERE source=? "
            "AND target_ts >= ? AND target_ts < ?",
            (quelle, von.tz_convert("UTC").isoformat(),
             bis.tz_convert("UTC").isoformat())).fetchone()[0]
    finally:
        con.close()


def _aufbau(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    alt = pd.date_range(NOW - pd.Timedelta(days=80), periods=6, freq="15min",
                        tz=TZ)
    frisch = pd.date_range(NOW - pd.Timedelta(days=5), periods=6, freq="15min",
                           tz=TZ)
    _saat(db, list(alt) + list(frisch))
    return db, alt, frisch


def test_counting_run_changes_nothing(tmp_path):
    db, alt, _ = _aufbau(tmp_path)
    vorher = _zeilen(db, "sc1", alt[0], alt[-1] + pd.Timedelta(minutes=15))
    res = thin_pv_forecast_archive(db, NOW, 60, BUCKETS, apply=False)
    assert res["scanned"] > 0 and res["applied"] is False
    assert res["members_deleted"] + res["thinned_deleted"] > 0
    assert _zeilen(db, "sc1", alt[0],
                   alt[-1] + pd.Timedelta(minutes=15)) == vorher


def test_thinning_keeps_one_row_per_lead_bucket_and_drops_members(tmp_path):
    db, alt, frisch = _aufbau(tmp_path)
    ende_alt = alt[-1] + pd.Timedelta(minutes=15)
    ende_frisch = frisch[-1] + pd.Timedelta(minutes=15)
    assert _zeilen(db, MITGLIED, alt[0], ende_alt) == len(alt) * len(LEADS)

    thin_pv_forecast_archive(db, NOW, 60, BUCKETS, apply=True)

    # jenseits des Rohfensters: je Zielslot genau ein Treffer pro Bucket
    assert _zeilen(db, "sc1", alt[0], ende_alt) == len(alt) * len(BUCKETS)
    assert _zeilen(db, "pvmodel:Ost", alt[0], ende_alt) == len(alt) * len(BUCKETS)
    # Ensemble-Mitglieder dort komplett weg
    assert _zeilen(db, MITGLIED, alt[0], ende_alt) == 0
    # das Rohfenster bleibt unangetastet - auch die Mitglieder
    assert _zeilen(db, "sc1", frisch[0], ende_frisch) == len(frisch) * len(LEADS)
    assert _zeilen(db, MITGLIED, frisch[0],
                   ende_frisch) == len(frisch) * len(LEADS)


def test_calibration_read_is_bit_identical_after_thinning(tmp_path):
    """Die Zusage, an der alles haengt: der Kalibrier-Fit liest mit Lead 0 und
    darf sich durch das Ausduennen NICHT veraendern."""
    db, alt, _ = _aufbau(tmp_path)
    cfg = make_config()
    ende = alt[-1] + pd.Timedelta(minutes=15)
    lesen = lambda lead: pv_eval.read_group_asof(  # noqa: E731
        db, AGGREGAT, alt[0], ende, TZ, 15, "pv", lead)

    vorher = {lead: lesen(lead) for lead in BUCKETS}
    thin_pv_forecast_archive(db, NOW, 60, BUCKETS, apply=True)
    for lead in BUCKETS:
        pd.testing.assert_series_equal(
            lesen(lead), vorher[lead],
            obj=f"Lead {lead} h nach dem Ausduennen")
    assert not vorher[0.0].empty          # der Test prueft echte Werte
    del cfg


def test_buckets_come_from_the_ensemble_horizons():
    cfg = make_config()
    cfg.pv_model.ensemble_horizon_hours = [6.0, 24.0, 48.0]
    # 0 h muss dabei sein: das ist die juengste Prognose vor dem Zielslot und
    # damit die Basis des Kalibrier-Fits.
    assert lead_buckets(cfg) == [0.0, 6.0, 24.0, 48.0]
    cfg.pv_model.ensemble_horizon_hours = []
    assert lead_buckets(cfg) == [0.0]


def test_nothing_to_do_while_the_archive_is_young(tmp_path):
    db = str(tmp_path / "hist.sqlite")
    _saat(db, list(pd.date_range(NOW - pd.Timedelta(days=5), periods=4,
                                freq="15min", tz=TZ)))
    res = thin_pv_forecast_archive(db, NOW, 60, BUCKETS, apply=True)
    assert res["scanned"] == 0
    assert res["members_deleted"] == res["thinned_deleted"] == 0
