"""Konflikt zwischen statischer Korrektur-Empfehlung und Intraday-Korrektur."""
from __future__ import annotations

import sqlite3

import pandas as pd

from kalibrierung import _correction_conflict, _intraday_load_pull


class _Cfg:
    class _R:
        history_db_path = ""
    e3dc_rscp = _R()


def _db(tmp_path, zeilen):
    pfad = str(tmp_path / "hist.sqlite")
    con = sqlite3.connect(pfad)
    con.execute("CREATE TABLE intraday_correction (issue_ts TEXT, signal TEXT, "
                "raw_ratio REAL, clipped_ratio REAL)")
    con.executemany("INSERT INTO intraday_correction VALUES(?,?,?,?)", zeilen)
    con.commit()
    con.close()
    cfg = _Cfg()
    cfg.e3dc_rscp.history_db_path = pfad
    return cfg


def test_pull_reads_mean_and_clipped_share(tmp_path):
    jetzt = pd.Timestamp.now(tz="UTC")
    zeilen = [((jetzt - pd.Timedelta(hours=h)).isoformat(), "load", 0.6, 0.667)
              for h in range(4)]
    zeilen += [((jetzt - pd.Timedelta(hours=h)).isoformat(), "load", 1.0, 1.0)
               for h in range(4, 8)]
    # Ausserhalb des Fensters und fremdes Signal duerfen nicht zaehlen.
    zeilen.append(((jetzt - pd.Timedelta(days=40)).isoformat(), "load", 0.1, 0.1))
    zeilen.append((jetzt.isoformat(), "pv", 0.1, 0.1))
    pull = _intraday_load_pull(_db(tmp_path, zeilen))
    assert pull["runs"] == 8
    assert pull["mean_raw_ratio"] == 0.8
    assert pull["clipped_share_percent"] == 50.0


def test_conflict_only_when_the_two_pull_apart(tmp_path):
    pull = {"days": 7.0, "runs": 672, "mean_raw_ratio": 0.808,
            "clipped_share_percent": 38.1}
    # Empfehlung hoch, Intraday runter -> Konflikt, mit Zahlen in der Meldung.
    text = _correction_conflict(1.2493, pull)
    assert text and "1.2493" in text and "0.808" in text and "672" in text
    # Gleiche Richtung -> kein Hinweis.
    assert _correction_conflict(0.95, pull) is None
    # Ohne Daten kein Hinweis (und kein Absturz).
    assert _correction_conflict(1.25, None) is None
    assert _correction_conflict(None, pull) is None


def test_pull_is_none_without_rows(tmp_path):
    assert _intraday_load_pull(_db(tmp_path, [])) is None
