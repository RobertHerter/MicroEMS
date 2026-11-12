"""Einmalige Migration: pvlib-Prognosen auf den Intervall-ANFANG stempeln.

Open-Meteo stempelt Strahlung auf das Intervall-ENDE ("preceding hour mean").
Bis MicroEMS 3.14.3 schrieb ``ems.pvforecast`` den Wert trotzdem auf diese
Marke, also eine Stunde zu spaet: die pvlib-Prognose lag im Mittel 37 min
hinter der Ist-PV. Seit dem Fix steht er auf dem Intervall-Anfang, wie Solcast.

Die schon gespeicherten pvlib-Zeilen tragen noch die alte Marke. Kalibrierung
(bis 730 Tage zurueck), Ensemble-Gewichte, p10/p90-Baender und die
PV-Quellenwahl lernen aus genau diesem Archiv - mit gemischten Stempeln wuerden
sie wochenlang das Falsche lernen. Dieses Werkzeug verschiebt die alten Zeilen
der Quellen ``pvmodel:*`` und ``pvmodel-member:*`` einmalig um eine Stunde nach
vorn, im Live-Cache (``pv_forecast.ts``) und im Archiv
(``pv_forecast_archive.target_ts``). Im Replay vom 12.11.2026 kamen die so
verschobenen Altwerte praktisch auf das Ergebnis des neuen Codes (WAPE nach
Kalibrierung 48,9 statt 50,2 %); der kleine Rest ist der damals falsche
Sonnenstand, der sich nicht nachtraeglich korrigieren laesst.

Welche Zeilen alt sind: Der neue Code merkt sich beim ersten Schreiben
Ausgabezeit und fruehesten Stempel (``merke_neue_konvention``). Laeuft das
Werkzeug vor dem ersten Start des neuen Codes, ist alles alt.

Aufruf (EMS vorher stoppen - die Migration haelt die Datenbank gesperrt):

    python -m ems.pvlib_stempel --config config.yaml           # Probelauf
    python -m ems.pvlib_stempel --config config.yaml --apply   # mit Sicherung
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Optional

import pandas as pd

TABELLE = "ems_migration"
NEU_AB = "pvlib_intervallanfang_ab"
MIGRIERT = "pvlib_stempel_migriert"
_QUELLEN = "(source LIKE 'pvmodel:%' OR source LIKE 'pvmodel-member:%')"
_MINUS_1H = "strftime('%Y-%m-%dT%H:%M:%S+00:00', {col}, '-1 hours')"


def _tabelle(con: sqlite3.Connection) -> None:
    con.execute(f"CREATE TABLE IF NOT EXISTS {TABELLE} ("
                " name TEXT PRIMARY KEY, ts TEXT NOT NULL, info TEXT)")


def _eintrag(con: sqlite3.Connection, name: str) -> Optional[dict]:
    _tabelle(con)
    row = con.execute(f"SELECT ts, info FROM {TABELLE} WHERE name=?",
                      (name,)).fetchone()
    if not row:
        return None
    return {"ts": row[0], "info": json.loads(row[1]) if row[1] else {}}


def merke_neue_konvention(db: str, issue_ts, erster_stempel: str) -> None:
    """Vom neuen Code beim Schreiben aufgerufen: ab wann die pvlib-Zeilen den
    Intervall-Anfang tragen. Nur der erste Aufruf zaehlt."""
    con = sqlite3.connect(db, timeout=10)
    try:
        _tabelle(con)
        con.execute(
            f"INSERT OR IGNORE INTO {TABELLE}(name, ts, info) VALUES(?,?,?)",
            (NEU_AB, pd.Timestamp(issue_ts).tz_convert("UTC").isoformat(),
             json.dumps({"erster_stempel": erster_stempel})))
        con.commit()
    finally:
        con.close()


def _bedingungen(con: sqlite3.Connection) -> tuple:
    """(WHERE fuers Archiv, WHERE fuer den Cache, Parameter, Grenze)."""
    neu = _eintrag(con, NEU_AB)
    if neu is None:
        return _QUELLEN, _QUELLEN, (), (), None
    ab = neu["info"].get("erster_stempel") or neu["ts"]
    return (f"{_QUELLEN} AND issue_ts < ?", f"{_QUELLEN} AND ts < ?",
            (neu["ts"],), (ab,), neu)


def stand(db: str) -> dict:
    """Was die Migration tun wuerde (aendert nichts)."""
    con = sqlite3.connect(db, timeout=10)
    try:
        erledigt = _eintrag(con, MIGRIERT)
        w_archiv, w_cache, p_archiv, p_cache, neu = _bedingungen(con)
        archiv = con.execute(
            f"SELECT count(*), min(target_ts), max(target_ts)"
            f" FROM pv_forecast_archive WHERE {w_archiv}", p_archiv).fetchone()
        cache = con.execute(
            f"SELECT count(*), min(ts), max(ts) FROM pv_forecast"
            f" WHERE {w_cache}", p_cache).fetchone()
    finally:
        con.close()
    return {"bereits_migriert": erledigt, "neuer_code_ab": neu,
            "archiv": {"zeilen": archiv[0], "von": archiv[1], "bis": archiv[2]},
            "cache": {"zeilen": cache[0], "von": cache[1], "bis": cache[2]}}


def sichern(db: str, ziel: str) -> None:
    """Konsistente Kopie der ganzen Datenbank (SQLite-Backup-API)."""
    quelle = sqlite3.connect(db, timeout=30)
    kopie = sqlite3.connect(ziel)
    try:
        quelle.backup(kopie)
    finally:
        kopie.close()
        quelle.close()


def _verschieben(con: sqlite3.Connection, tabelle: str, spalte: str,
                 where: str, params: tuple) -> int:
    """Alle passenden Stempel um -1 h, kollisionsfrei in zwei Schritten.

    Direkt verschoben stiesse eine Zeile auf ihre noch unverschobene
    Vorgaengerstunde (Primaerschluessel). Erst bekommen alle Zielwerte ein
    '#' angehaengt - das traegt kein echter Stempel -, dann faellt es weg.
    """
    minus = _MINUS_1H.format(col=spalte)
    n = con.execute(f"UPDATE {tabelle} SET {spalte} = {minus} || '#'"
                    f" WHERE {where}", params).rowcount
    con.execute(f"UPDATE {tabelle} SET {spalte} = substr({spalte}, 1,"
                f" length({spalte}) - 1) WHERE {spalte} LIKE '%#'")
    return n


def migrieren(db: str, sicherung: Optional[str] = None) -> dict:
    """Verschiebt die alten pvlib-Zeilen (eine Transaktion) und setzt den
    Merker. Ein zweiter Lauf aendert nichts."""
    if sicherung:
        sichern(db, sicherung)
    con = sqlite3.connect(db, timeout=30)
    try:
        if _eintrag(con, MIGRIERT) is not None:
            return {"geaendert": False, "grund": "bereits migriert"}
        w_archiv, w_cache, p_archiv, p_cache, neu = _bedingungen(con)
        con.execute("BEGIN IMMEDIATE")
        archiv = _verschieben(con, "pv_forecast_archive", "target_ts",
                              w_archiv, p_archiv)
        cache = _verschieben(con, "pv_forecast", "ts", w_cache, p_cache)
        info = {"archiv": archiv, "cache": cache, "sicherung": sicherung,
                "neuer_code_ab": neu["ts"] if neu else None}
        con.execute(f"INSERT INTO {TABELLE}(name, ts, info) VALUES(?,?,?)",
                    (MIGRIERT, pd.Timestamp.now(tz="UTC").isoformat(),
                     json.dumps(info)))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"geaendert": True, **info}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="wirklich verschieben (ohne: nur Probelauf)")
    ap.add_argument("--sicherung", default=None,
                    help="Pfad der Sicherungskopie (Standard: neben der DB)")
    ap.add_argument("--ohne-sicherung", action="store_true",
                    help="keine Sicherung anlegen (nur wenn schon gesichert)")
    args = ap.parse_args(argv)
    from .config import load_config
    db = load_config(args.config).e3dc_rscp.history_db_path
    s = stand(db)
    print(f"Datenbank: {db}")
    if s["bereits_migriert"]:
        print(f"Bereits migriert am {s['bereits_migriert']['ts']}: "
              f"{s['bereits_migriert']['info']}")
        return 0
    if s["neuer_code_ab"]:
        print(f"Neuer Code schreibt seit {s['neuer_code_ab']['ts']}; "
              "nur aeltere Zeilen werden verschoben.")
    for name in ("archiv", "cache"):
        t = s[name]
        print(f"  {name:6s}: {t['zeilen']} Zeilen ({t['von']} .. {t['bis']}) -> -1 h")
    if not args.apply:
        print("Probelauf - nichts geaendert. Mit --apply ausfuehren "
              "(EMS vorher stoppen).")
        return 0
    ziel = None if args.ohne_sicherung else (
        args.sicherung or f"{db}.vor-pvlib-stempel-"
        f"{pd.Timestamp.now():%Y%m%d-%H%M%S}")
    if ziel:
        print(f"Sicherung nach {ziel} ...")
    r = migrieren(db, ziel)
    print(("Migriert: " if r["geaendert"] else "Nichts geaendert: ") +
          json.dumps({k: v for k, v in r.items() if k != "geaendert"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
