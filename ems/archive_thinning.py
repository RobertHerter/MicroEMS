"""Prognosearchiv ausduennen, damit es Jahre statt Wochen halten kann.

``pv_forecast_archive`` haelt je Ausgabezeitpunkt die komplette Prognose. Das
ist fuer leckagefreie Rolling-Origin-Auswertung genau richtig, waechst aber mit
jedem Zyklus: gemessen am 14.08.2026 rund 49.600 Zeilen und 14,2 MB pro Tag
(Tabelle plus zwei Indizes). Auf 730 Tage hochgerechnet waeren das ~10,4 GB.

Der Aufwand steckt nicht in der Historie, sondern in der Wiederholung:

* 65 % der Zeilen sind pvlib-Ensemble-Mitglieder (``pvmodel-member:*``). Die
  liest ausschliesslich das Ensemble-Lernen, mit ``ensemble_lookback_days``
  (45). Sie 730 Tage zu halten ist 16-mal laenger als der einzige Konsument
  braucht.
* Auch bei den Aggregat-Quellen wird derselbe Zielslot in jedem Zyklus neu
  prognostiziert - ``pvmodel:Ost`` schreibt 7.280 Zeilen taeglich. Fuer
  Kalibrierung, Quellenvergleich und Validierung wird je Zielslot aber nur
  EINE Prognose je Vorlaufzeit gebraucht.

Deshalb:

1. Rohdaten bleiben ``forecast_archive_raw_days`` (60) vollstaendig erhalten -
   deckt den Ensemble-Lookback (45 T) mit Reserve, ebenso Quellenauswahl (30 T)
   und Feldguete (21 T).
2. Aelter als das: je Quelle und Zielslot bleibt eine Prognose je Lead-Bucket
   (0 h plus ``ensemble_horizon_hours``). Lead 0 ist die juengste Prognose vor
   dem Zielslot - genau die, mit der ``read_group_asof`` den Kalibrier-Fit
   bildet. Der bleibt damit unveraendert.
3. Ensemble-Mitglieder jenseits des Rohfensters fallen ganz weg.

Was verloren geht: die feine Entwicklung einer Prognose fuer alte Slots ("wie
hat sich die Vorhersage fuer den 3. Januar ueber 72 Stunden veraendert").
Kalibrierung, Quellenvergleich und Validierung brauchen sie nicht, eine
spaetere forensische Frage koennte sie nicht mehr stellen.

Aufruf (zaehlt nur, loescht NICHTS):
    python -m ems.archive_thinning --config config.yaml

Mit --apply wird geloescht. --vacuum gibt den Platz an das Dateisystem zurueck
(schreibt die ganze Datei neu und braucht kurzzeitig doppelten Platz - deshalb
nicht automatisch).
"""
from __future__ import annotations

import argparse
import logging

import pandas as pd

from .config import load_config
from .local_history import _con

log = logging.getLogger("ems.archive_thinning")

_MEMBER_PREFIX = "pvmodel-member:"


def lead_buckets(config) -> list:
    """Vorlaufzeiten, die erhalten bleiben muessen.

    0 h ist die juengste Prognose vor dem Zielslot (die Basis des
    Kalibrier-Fits), dazu die Buckets der Rolling-Origin-Validierung."""
    stunden = {0.0}
    for h in (getattr(config.pv_model, "ensemble_horizon_hours", None) or []):
        try:
            stunden.add(float(h))
        except (TypeError, ValueError):
            continue
    return sorted(stunden)


def _keep_rowids(frame: pd.DataFrame, buckets: list) -> set:
    """Je (Quelle, Zielslot) und Bucket die Zeile mit der KLEINSTEN Vorlaufzeit,
    die mindestens dem Bucket entspricht.

    Genau die waehlt ``read_group_asof`` fuer diesen Lead aus ("juengste
    Prognose mit issue_ts <= target_ts - lead"). Damit bleibt jede Leseabfrage
    auf den Buckets nach dem Ausduennen bitgleich. Die naechstliegende Zeile zu
    nehmen genuegt NICHT: eine Vorlaufzeit knapp unter dem Bucket wird von der
    Abfrage nie gesehen, der Lead liefe dann ins Leere.
    """
    behalten = set()
    for _, gruppe in frame.groupby(["source", "target_ts"], sort=False):
        sortiert = gruppe.sort_values("lead_h")
        lead = sortiert["lead_h"].to_numpy()
        for bucket in buckets:
            passend = lead >= bucket - 1e-9
            if not passend.any():
                continue          # diesen Lead gibt es fuer den Slot ohnehin nicht
            behalten.add(int(sortiert["rowid"].to_numpy()[passend.argmax()]))
    return behalten


def thin_pv_forecast_archive(db: str, now, raw_days: int, buckets: list,
                             apply: bool = False) -> dict:
    """Archiv jenseits des Rohfensters ausduennen. Gibt Zaehlwerte zurueck."""
    grenze = (pd.Timestamp(now).tz_convert("UTC")
              - pd.Timedelta(days=max(1, int(raw_days))))
    grenze_iso = grenze.isoformat()
    out = {"cutoff": grenze_iso, "raw_days": int(raw_days),
           "buckets_h": list(buckets), "scanned": 0, "members_deleted": 0,
           "thinned_deleted": 0, "kept": 0, "applied": bool(apply)}
    con = _con(db)
    try:
        # Nur target_ts vergleichen: einheitliches UTC-ISO mit fester Laenge,
        # der Stringvergleich ist damit gleichbedeutend mit dem Zeitvergleich.
        # (issue_ts hat Mikrosekunden und waere dafuer untauglich.)
        aeltester = con.execute(
            "SELECT MIN(target_ts) FROM pv_forecast_archive "
            "WHERE target_ts < ?", (grenze_iso,)).fetchone()[0]
        if aeltester is None:
            return out                       # nichts jenseits des Rohfensters
        tag = pd.Timestamp(aeltester).tz_convert("UTC").normalize()
        while tag < grenze:
            naechster = min(tag + pd.Timedelta(days=1), grenze)
            frame = pd.read_sql(
                "SELECT rowid, source, issue_ts, target_ts "
                "FROM pv_forecast_archive WHERE target_ts >= ? AND target_ts < ?",
                con, params=[tag.isoformat(), naechster.isoformat()])
            tag = naechster
            if frame.empty:
                continue
            out["scanned"] += len(frame)
            mitglieder = frame["source"].str.startswith(_MEMBER_PREFIX)
            zu_loeschen = [int(r) for r in frame.loc[mitglieder, "rowid"]]
            out["members_deleted"] += len(zu_loeschen)
            rest = frame.loc[~mitglieder]
            if not rest.empty:
                rest = rest.assign(lead_h=(
                    pd.to_datetime(rest["target_ts"], format="ISO8601", utc=True)
                    - pd.to_datetime(rest["issue_ts"], format="ISO8601", utc=True)
                ).dt.total_seconds() / 3600.0)
                behalten = _keep_rowids(rest, buckets)
                out["kept"] += len(behalten)
                weg = [int(r) for r in rest["rowid"] if int(r) not in behalten]
                out["thinned_deleted"] += len(weg)
                zu_loeschen.extend(weg)
            if apply and zu_loeschen:
                for i in range(0, len(zu_loeschen), 500):
                    teil = zu_loeschen[i:i + 500]
                    con.execute(
                        "DELETE FROM pv_forecast_archive WHERE rowid IN "
                        f"({','.join('?' * len(teil))})", teil)
                con.commit()
    finally:
        con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--raw-days", type=int, default=None,
                    help="Rohfenster in Tagen (Standard: "
                         "general.forecast_archive_raw_days)")
    ap.add_argument("--apply", action="store_true",
                    help="wirklich loeschen (ohne das wird nur gezaehlt)")
    ap.add_argument("--vacuum", action="store_true",
                    help="Datei nach dem Loeschen verkleinern (schreibt die "
                         "ganze Datenbank neu)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(args.config)
    tage = (args.raw_days if args.raw_days is not None
            else cfg.general.forecast_archive_raw_days)
    buckets = lead_buckets(cfg)
    now = pd.Timestamp.now(tz=cfg.general.timezone)
    res = thin_pv_forecast_archive(cfg.e3dc_rscp.history_db_path, now, tage,
                                   buckets, apply=args.apply)
    weg = res["members_deleted"] + res["thinned_deleted"]
    log.info("Prognosearchiv ausduennen (Rohfenster %d Tage, Grenze %s)",
             res["raw_days"], res["cutoff"][:16])
    log.info("  Lead-Buckets: %s h", ", ".join(f"{b:g}" for b in buckets))
    if not res["scanned"]:
        log.info("  nichts jenseits des Rohfensters - Archiv noch zu jung.")
        return 0
    log.info("  geprueft %d Zeilen | Ensemble-Mitglieder %d | ausgeduennt %d | "
             "behalten %d", res["scanned"], res["members_deleted"],
             res["thinned_deleted"], res["kept"])
    log.info("  %s %d Zeilen (%.0f %%)",
             "geloescht:" if args.apply else "wuerde loeschen:",
             weg, 100.0 * weg / max(1, res["scanned"]))
    if args.apply and args.vacuum:
        con = _con(cfg.e3dc_rscp.history_db_path)
        try:
            con.execute("VACUUM")
        finally:
            con.close()
        log.info("  VACUUM ausgefuehrt - Platz an das Dateisystem zurueckgegeben.")
    elif args.apply:
        log.info("  Platz bleibt in der Datei und wird wiederverwendet "
                 "(--vacuum gibt ihn frei).")
    return 0


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(main())
