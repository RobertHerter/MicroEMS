"""Lesende Module oeffnen die Historie ueber local_history.connect.

Ein nacktes ``sqlite3.connect`` setzt weder WAL noch busy_timeout; am
14.11.2026 hat ein langer Leser so den Produktivdienst blockiert.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pandas as pd
import pytest

_MODULE = ("ems/pv_ensemble.py", "ems/pv_eval.py", "ems/load_learning.py",
           "kalibrierung.py")


@pytest.mark.parametrize("name", _MODULE)
def test_no_bare_sqlite_connect(name):
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / name).read_text(encoding="utf-8")
    assert "sqlite3.connect(" not in text, (
        f"{name}: sqlite3.connect direkt - stattdessen local_history.connect")


def test_readers_open_in_wal_mode_and_close(tmp_path, monkeypatch):
    """Die Leser gehen durch ``connect`` (WAL) und schliessen ihre Verbindung."""
    from ems import local_history, pv_ensemble, pv_eval

    db = str(tmp_path / "h.sqlite")
    local_history._con(db).close()
    offen = []
    echt = local_history.connect

    class _Wache:
        def __init__(self, con):
            self._con, self.closed = con, False

        def execute(self, *a, **k):
            return self._con.execute(*a, **k)

        def close(self):
            self.closed = True
            self._con.close()

    def gezaehlt(path, **kw):
        wache = _Wache(echt(path, **kw))
        offen.append(wache)
        return wache

    monkeypatch.setattr(pv_eval, "connect", gezaehlt)
    monkeypatch.setattr(pv_ensemble, "connect", gezaehlt)
    start = pd.Timestamp("2026-06-10 00:00", tz="UTC")
    ende = start + pd.Timedelta(hours=2)
    pv_eval.read_actual_pv(db, start, ende, "UTC", 15)
    pv_eval.read_group_asof(db, ["x"], start, ende, "UTC", 15)
    pv_eval.read_group_cache(db, ["x"], start, ende, "UTC", 15)
    pv_ensemble._actuals(db, start, ende)
    pv_ensemble._archive_groups(db, {"g": ["x"]}, start, ende)
    assert len(offen) == 5 and all(w.closed for w in offen)
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()
