"""Migration der pvlib-Stempel auf den Intervall-Anfang (ems.pvlib_stempel)."""
import sqlite3

import pandas as pd

from ems import local_history, pvlib_stempel

ISSUE_ALT = pd.Timestamp("2026-11-10 06:00", tz="UTC")
STUNDEN = pd.date_range("2026-11-10 08:00", periods=4, freq="1h", tz="UTC")


def _iso(ts):
    return pd.Timestamp(ts).isoformat()


def _db(tmp_path):
    db = str(tmp_path / "h.sqlite")
    werte = {_iso(t): (100.0 * (i + 1), 80.0, 120.0)
             for i, t in enumerate(STUNDEN)}
    for src in ("pvmodel:Ost", "pvmodel-member:dwd_icon:Ost", "solcast-res"):
        local_history.write_pv_forecast(db, src, werte)
        local_history.write_pv_forecast_archive(db, src, ISSUE_ALT, werte)
    return db


def _cache(db, src):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT ts, pv_w FROM pv_forecast WHERE source=?"
                       " ORDER BY ts", (src,)).fetchall()
    con.close()
    return rows


def _archiv(db, src):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT issue_ts, target_ts, pv_w FROM pv_forecast_archive"
                       " WHERE source=? ORDER BY issue_ts, target_ts",
                       (src,)).fetchall()
    con.close()
    return rows


def test_probelauf_aendert_nichts(tmp_path):
    db = _db(tmp_path)
    vorher = _cache(db, "pvmodel:Ost"), _archiv(db, "pvmodel:Ost")
    s = pvlib_stempel.stand(db)
    assert s["cache"]["zeilen"] == 8          # Ost + Member, je 4 Stunden
    assert s["archiv"]["zeilen"] == 8
    assert s["bereits_migriert"] is None
    assert (_cache(db, "pvmodel:Ost"), _archiv(db, "pvmodel:Ost")) == vorher


def test_migration_verschiebt_nur_pvlib_um_eine_stunde(tmp_path):
    db = _db(tmp_path)
    r = pvlib_stempel.migrieren(db)
    assert r["geaendert"] and r["cache"] == 8 and r["archiv"] == 8
    erwartet = [(_iso(t - pd.Timedelta(hours=1)), 100.0 * (i + 1))
                for i, t in enumerate(STUNDEN)]
    for src in ("pvmodel:Ost", "pvmodel-member:dwd_icon:Ost"):
        assert _cache(db, src) == erwartet
        # aufeinanderfolgende Stunden derselben Ausgabe: keine Schluessel-
        # kollision, Ausgabezeit unveraendert
        assert [(t, v) for _, t, v in _archiv(db, src)] == erwartet
        assert {i for i, _, _ in _archiv(db, src)} == {_iso(ISSUE_ALT)}
    # Solcast bleibt, wie es ist
    assert _cache(db, "solcast-res")[0][0] == _iso(STUNDEN[0])
    assert _archiv(db, "solcast-res")[0][1] == _iso(STUNDEN[0])


def test_zweiter_lauf_aendert_nichts(tmp_path):
    db = _db(tmp_path)
    pvlib_stempel.migrieren(db)
    nach_erstem = _cache(db, "pvmodel:Ost"), _archiv(db, "pvmodel:Ost")
    r = pvlib_stempel.migrieren(db)
    assert not r["geaendert"]
    assert (_cache(db, "pvmodel:Ost"), _archiv(db, "pvmodel:Ost")) == nach_erstem
    assert pvlib_stempel.stand(db)["bereits_migriert"] is not None


def test_zeilen_des_neuen_codes_bleiben_stehen(tmp_path):
    """Laeuft die Migration erst nach dem neuen Code, verschiebt sie nur, was
    vor dessen erstem Schreiben entstand."""
    db = _db(tmp_path)
    issue_neu = pd.Timestamp("2026-11-10 09:00", tz="UTC")
    neu = {_iso(t): (500.0, 400.0, 600.0)
           for t in pd.date_range("2026-11-10 10:00", periods=3, freq="1h",
                                  tz="UTC")}
    local_history.write_pv_forecast(db, "pvmodel:Ost", neu)
    local_history.write_pv_forecast_archive(db, "pvmodel:Ost", issue_neu, neu)
    pvlib_stempel.merke_neue_konvention(db, issue_neu, min(neu))
    pvlib_stempel.migrieren(db)

    cache = dict(_cache(db, "pvmodel:Ost"))
    # alt (vor 10:00): verschoben; neu (ab 10:00): unveraendert
    assert cache[_iso(STUNDEN[0] - pd.Timedelta(hours=1))] == 100.0
    assert cache[_iso(STUNDEN[1] - pd.Timedelta(hours=1))] == 200.0
    for t in neu:
        assert cache[t] == 500.0
    archiv_neu = [(t, v) for i, t, v in _archiv(db, "pvmodel:Ost")
                  if i == _iso(issue_neu)]
    assert archiv_neu == [(t, 500.0) for t in sorted(neu)]


def test_merker_haelt_den_ersten_lauf(tmp_path):
    db = _db(tmp_path)
    pvlib_stempel.merke_neue_konvention(db, ISSUE_ALT, "2026-11-10T07:00:00+00:00")
    pvlib_stempel.merke_neue_konvention(
        db, ISSUE_ALT + pd.Timedelta(hours=5), "2026-11-11T07:00:00+00:00")
    neu = pvlib_stempel.stand(db)["neuer_code_ab"]
    assert neu["ts"] == _iso(ISSUE_ALT)
    assert neu["info"]["erster_stempel"] == "2026-11-10T07:00:00+00:00"


def test_sicherung_wird_angelegt(tmp_path):
    db = _db(tmp_path)
    ziel = str(tmp_path / "kopie.sqlite")
    pvlib_stempel.migrieren(db, ziel)
    # die Kopie zeigt den Stand VOR der Migration
    assert _cache(ziel, "pvmodel:Ost")[0][0] == _iso(STUNDEN[0])
    assert _cache(db, "pvmodel:Ost")[0][0] == _iso(STUNDEN[0] - pd.Timedelta(hours=1))
