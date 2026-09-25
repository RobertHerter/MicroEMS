#!/usr/bin/env python3
"""PV-Nowcast-Parameter rollierend auf den ECHTEN Tagen nachrechnen.

Anlass (24.09.2026): an einem trueben Tag kaufte das EMS nicht einmal voll im
guenstigsten Fenster (11,6 ct), sondern in sechs Portionen bis 24,8 ct -
17,6 ct Mehrkosten gegenueber denselben 4,82 kWh im billigsten Fenster. Jeder
einzelne Plan war exakt optimal; falsch war die PV-Erwartung. Der 12:00-Plan
rechnete mit 5,35 kWh PV bis 14:00, geliefert wurden 1,86 kWh. Gemessen hatte
das System den Einbruch - Rohverhaeltnis 0,49 -, aber drei Daempfer
hintereinander liessen davon fast nichts bei der Ladeentscheidung ankommen:

* ``intraday_pv_max_factor`` klemmt nach unten bei 1/1,5 = 0,67,
* ``intraday_pv_max_step`` rampt 0,1 je Lauf, eine Stunde bis dorthin,
* ``intraday_pv_operational_slots`` wirkt nur auf die naechsten 4 Slots.

Die Ladeentscheidung schaut aber zwoelf Stunden voraus.

Vier Fallen, in die ein solcher Vergleich laeuft, und wie sie umgangen werden:

1. **Rollierend, nicht ein Plan.** Ausgefuehrt wird von jedem Plan nur der
   erste Slot; der Schaden entsteht erst durch die Folge der Korrekturen. Jeder
   archivierte Lauf wird neu geloest, mit dem SoC, den die Nachrechnung bis
   dahin erreicht hat - sonst koennen die Varianten nicht auseinanderlaufen.
2. **Rahmen statt Sollwert.** ``planvalue.simulate_schedule`` erzwingt die
   geplante Akkuleistung, auch geplantes PV-Laden ohne Sonne, und zoege die
   Luecke aus dem Netz - jeder PV-Fehler wuerde zu einem Netzbezug, den es real
   nie gab. Hier wird wie im Betrieb der RAHMEN ausgefuehrt: befohlenes
   Netzladen laedt mit der befohlenen Leistung, im Auto-Modus folgt der Akku
   dem echten Ueberschuss innerhalb der Grenzen.
3. **Terminalwert.** Die Varianten enden unterschiedlich voll; verglichen wird
   ``netto = Kosten - Terminalwert`` mit der Formel des Optimierers.
4. **Kein Warmstart, keine Planstabilisierung.** Beides ist globaler Zustand
   und schluege von einer Variante in die naechste durch. Die Nachrechnung ist
   damit nicht bitgleich zum Betrieb; verglichen werden die Varianten
   UNTEREINANDER.

Basis der PV-Erwartung ist ``pv_without_nowcast_w`` aus dem Prognosearchiv,
die Prognose VOR der Intraday-Korrektur; jede Variante legt ihre eigene
Korrektur darauf. Die Rohverhaeltnisse kommen aus ``intraday_correction`` - wo
dort keines steht (unter ``intraday_pv_min_power_w``), bleibt es in allen
Varianten ohne Korrektur. Die Spalte "Nachbau-Abw." prueft, dass die
Betriebskorrektur exakt reproduziert wird; ueber zwei volle Tage 0,0 W. Ein
Einzelwert daneben stammt von einem verspaeteten Lauf.

WO ES UEBERHAUPT WIRKT. An fuenf sonnigen Septembertagen unterschieden sich
alle Varianten um exakt null, obwohl die Korrektur am 18.09. in 43 Laeufen
griff: selbst beim staerksten Einbruch (Rohverhaeltnis 0,63, 2,3 kWh weniger
PV in vier Stunden) blieb der RAHMEN in allen 245 Plan-Slots gleich - Auto,
kein Netzladen, volle Grenzen. Die Daempfer kosten nur an NETZLADETAGEN Geld.
Das Skript loest eine Variante deshalb nur neu, wenn ihre Eingaben von 'ist'
abweichen; sonst uebernimmt sie dessen Entscheidung.

Erster Messpunkt, 24.09.2026 bis 17:30 (ein Tag - ein Anhaltspunkt, keine
Entscheidungsgrundlage), netto gegen 'ist':

    klammer   -2,0 ct     horizont   -6,6 ct
    schritt   +0,0 ct     alle      -12,6 ct   (von 17,6 ct vermeidbar)

Gekauft wurde in allen Varianten fast gleich viel (6,04 bis 6,18 kWh); der
Gewinn kommt aus dem Zeitpunkt. Das RISIKO der Lockerung - an Tagen mit
durchziehenden Wolken die PV unterschaetzen und unnoetig kaufen - zeigt ein
einzelner trueber Tag gar nicht. Dafuer braucht es mehrere Netzladetage.

Rechenaufwand: je archiviertem Lauf eine Loesung fuer 'ist', fuer die anderen
nur ab Abweichung; am 24.09. 68 plus 3 bis 17 Loesungen, 3 min. NIE mehrere
Instanzen parallel und immer mit niedriger Prioritaet - drei parallele Laeufe
verzoegerten am 24.09. den ersten Steuerbefehl des EMS um elf Minuten:

    OMP_NUM_THREADS=1 nice -n 19 python tools/pv_nowcast_sweep.py --von 2026-09-24
    OMP_NUM_THREADS=1 nice -n 19 python tools/pv_nowcast_sweep.py --tage 7 \\
        --variante ist --variante weit:max_factor=2.5,slots=16

Ueber Wochen sammeln (die Snapshots reichen nur ~10 Tage zurueck):

    OMP_NUM_THREADS=1 nice -n 19 python tools/pv_nowcast_sweep.py --tage 7 \\
        --protokoll pv_nowcast_messung.csv
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
import time
import zlib

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ems.config import load_config                            # noqa: E402
from ems.forecast import (intraday_factor_series,             # noqa: E402
                          stabilize_intraday_ratio)
from ems.local_history import read_optimizer_forecast_asof    # noqa: E402
from ems.optimizer import (Optimizer, OptimizerInputs,        # noqa: E402
                           terminal_credit_ct)
from ems.planvalue import load_day                            # noqa: E402

FELDER = ("house_load_w", "pv_w", "price_ct_kwh", "feedin_ct_kwh", "pv10_w",
          "ambient_temp_c", "solar_w_m2")
DURCHREICHEN = ("load_state", "load_feedback", "car_present",
                "initial_car_soc_wh")
SCHLUESSEL = {"max_factor": "intraday_pv_max_factor",
              "max_step": "intraday_pv_max_step",
              "slots": "intraday_pv_operational_slots",
              "decay": "intraday_pv_decay_hours",
              "deadband": "intraday_pv_deadband"}
STANDARD_VARIANTEN = (
    "ist",
    "klammer:max_factor=2.5",
    "schritt:max_step=0.3",
    "horizont:slots=16",
    "alle:max_factor=2.5,max_step=0.3,slots=16",
)


def _variante(text, fc):
    name, _, rest = text.partition(":")
    werte = {k: float(getattr(fc, lang)) for k, lang in SCHLUESSEL.items()}
    for teil in filter(None, rest.split(",")):
        k, _, wert = teil.partition("=")
        if k not in SCHLUESSEL:
            raise SystemExit(f"unbekannter Schluessel {k!r}")
        werte[k] = float(wert)
    werte["slots"] = int(werte["slots"])
    return name, werte


def _payload(con, sid):
    roh, = con.execute("SELECT payload FROM debug_snapshot WHERE id=?",
                       (sid,)).fetchone()
    try:
        return json.loads(zlib.decompress(roh))
    except Exception:
        return json.loads(roh)


def _eingaben(daten):
    i = daten["inputs"]
    kw = {"index": pd.DatetimeIndex(pd.to_datetime(i["index"], format="ISO8601")),
          "initial_house_soc_wh": float(i["initial_house_soc_wh"])}
    for f in FELDER:
        if i.get(f) is not None:
            kw[f] = np.asarray(i[f], dtype=float)
    for f in DURCHREICHEN:
        if i.get(f) is not None:
            kw[f] = i[f]
    return OptimizerInputs(**kw)


def _laeufe(con, t0, t1, tz):
    je = {}
    for sid, erzeugt in con.execute(
            "SELECT id, generated FROM debug_snapshot WHERE generated>=? "
            "AND generated<? ORDER BY id", (t0.isoformat(), t1.isoformat())):
        je[pd.Timestamp(erzeugt).tz_convert(tz)] = sid
    return je


def _verhaeltnisse(con, t0, t1, tz):
    return {pd.Timestamp(ts).tz_convert(tz): (roh, angewandt)
            for ts, roh, angewandt in con.execute(
                "SELECT issue_ts, raw_ratio, applied_ratio FROM intraday_correction "
                "WHERE signal='pv' AND issue_ts>=? AND issue_ts<?",
                (t0.tz_convert("UTC").isoformat(), t1.tz_convert("UTC").isoformat()))}


def _korrigiert(basis, angewandt, jetzt, p):
    if angewandt is None or not np.isfinite(angewandt):
        return basis
    f = intraday_factor_series(float(angewandt), basis.index, jetzt,
                               decay_hours=p["decay"], max_slots=p["slots"],
                               slot_minutes=15)
    return basis * f.to_numpy(dtype=float)


def _ausfuehren(zeile, pv, haus, soc, hb, dt):
    """Den Rahmen so ausfuehren, wie das E3DC ihn umsetzt -> (SoC, Netz)."""
    ladegrenze = float(zeile.get("batt_charge_limit_w", hb.max_total_charge_w))
    entladegrenze = float(zeile.get("batt_discharge_limit_w", hb.max_discharge_w))
    netzladen = float(zeile.get("batt_grid_charge_w", 0.0) or 0.0)
    ueb = pv - haus
    eff_ac, eff_dc = float(hb.eff_ac_charge), float(hb.charge_efficiency)
    if netzladen > 1.0:
        ac = min(netzladen, ladegrenze,
                 max(0.0, (hb.max_soc_wh - soc) / max(1e-9, eff_ac * dt)))
        soc += eff_ac * ac * dt
        dc = min(max(0.0, ueb), max(0.0, ladegrenze - ac),
                 max(0.0, (hb.max_soc_wh - soc) / max(1e-9, eff_dc * dt)))
        soc += eff_dc * dc * dt
        return soc, haus + ac + dc - pv
    if ueb >= 0.0:
        dc = min(ueb, ladegrenze,
                 max(0.0, (hb.max_soc_wh - soc) / max(1e-9, eff_dc * dt)))
        soc += eff_dc * dc * dt
        return soc, haus + dc - pv
    ent = min(-ueb, entladegrenze,
              max(0.0, (soc - hb.min_soc_wh) * hb.discharge_efficiency / max(1e-9, dt)))
    soc -= ent * dt / max(1e-9, hb.discharge_efficiency)
    return soc, haus - pv - ent


def rechne_tag(cfg, con, tag, varianten, zeitlimit):
    tz = cfg.general.timezone
    heute = pd.Timestamp.now(tz=tz).normalize()
    if pd.Timestamp(tag, tz=tz) == heute:
        from ems.planvalue import load_range
        # Heute bis zum letzten VOLLEN Slot - der interessante Teil liegt meist
        # schon hinter uns, und auf den ganzen Tag zu warten verschenkt Tage.
        ist = load_range(cfg, heute, pd.Timestamp.now(tz=tz).floor("15min"))
    else:
        ist = load_day(cfg, tag)
    if ist is None:
        return None, "keine Ist-Daten"
    index = pd.DatetimeIndex(ist["index"])
    t0, t1 = index[0], index[-1] + pd.Timedelta(minutes=15)
    laeufe = _laeufe(con, t0, t1, tz)
    if len(laeufe) < 0.8 * len(index):
        return None, f"nur {len(laeufe)} Laeufe fuer {len(index)} Slots"
    verh = _verhaeltnisse(con, t0 - pd.Timedelta(hours=1), t1, tz)
    # Der Betrieb laeuft durch; sein Rampenzustand stammt vom Vortag. Ein
    # "kalter" Tagesbeginn setzte den ersten Wert ohne Rampe - am 24.09. sofort
    # 0,67 statt 0,90/0,80/0,70 - und machte 'ist' schneller als den Betrieb.
    vorher = con.execute(
        "SELECT applied_ratio FROM intraday_correction WHERE signal='pv' "
        "AND issue_ts<? AND applied_ratio IS NOT NULL ORDER BY issue_ts DESC LIMIT 1",
        (t0.tz_convert("UTC").isoformat(),)).fetchone()
    start_ratio = float(vorher[0]) if vorher else 1.0
    db, hb, dt = cfg.e3dc_rscp.history_db_path, cfg.house_battery, cfg.general.dt_hours
    pv_ist = ist["pv_w"].fillna(0.0).to_numpy(float)
    haus_ist = ist["house_w"].to_numpy(float)
    preis = ist["price_ct_kwh"].to_numpy(float)
    einsp = ist["feedin_ct_kwh"].to_numpy(float)
    soc_start = float(ist["soc_percent"].iloc[0]) / 100.0 * hb.capacity_wh
    cfg_t = copy.deepcopy(cfg)
    cfg_t.optimization.solver_time_limit_s = zeitlimit

    # Archiv einmal je Lauf lesen - fuer alle Varianten dieselbe Basis.
    # Der Archivstand eines Laufs wird erst NACH dem Lauf geschrieben (~1 min
    # spaeter). "as of Slotbeginn" liefert deshalb den VORHERIGEN Lauf - am
    # 24.09. vor dem 12:00-Solcast-Abruf, 2 kW daneben. Gelesen wird darum
    # 10 min nach Slotbeginn und geprueft, dass der Stand in diesen Slot faellt.
    basis_je_lauf, fremd = {}, 0
    for ts, sid in laeufe.items():
        daten = _payload(con, sid)
        inp = _eingaben(daten)
        ii = pd.DatetimeIndex(inp.index).tz_convert(tz)
        stand, archiv = read_optimizer_forecast_asof(
            db, ts + pd.Timedelta(minutes=10), ii[0],
            ii[-1] + pd.Timedelta(minutes=15), tz)
        if stand is None or not (ts <= pd.Timestamp(stand).tz_convert(tz)
                                 < ts + pd.Timedelta(minutes=15)):
            fremd += 1
            archiv = None            # kein eigener Stand -> unkorrigiert lassen
        basis_je_lauf[ts] = (daten, archiv)

    ergebnis, pruef_max, pruef_wo = {}, 0.0, None
    p_prod = dict(varianten[0][1])          # 'ist' = Betriebsparameter
    # Eine Variante wird nur neu geloest, wenn ihre Eingaben von 'ist'
    # abweichen. Solange PV und SoC gleich sind, ist auch der Plan gleich - am
    # 18.09. blieb der Rahmen selbst beim staerksten Einbruch (Rohverhaeltnis
    # 0,63, 2,3 kWh weniger PV in 4 h) in allen 245 Slots identisch. Ohne diese
    # Abkuerzung rechnete das Skript tausende Loesungen, die nichts aendern
    # konnten, und bremste den laufenden EMS elf Minuten aus.
    ist_je_lauf = {}
    for name, p in varianten:
        soc = soc_start
        zustand = {"ratio": start_ratio, "seeded": True}
        letzte = None
        loesungen = 0
        kosten = bezug = einsp_kwh = netzladen_kwh = 0.0
        for k, ts in enumerate(index):
            if ts in basis_je_lauf:
                daten, archiv = basis_je_lauf[ts]
                inp = _eingaben(daten)
                ii = pd.DatetimeIndex(inp.index).tz_convert(tz)
                # Der Betrieb rechnet ab SLOTBEGINN: korrigiert werden der
                # laufende Slot und die drei folgenden (Lead 0/0,25/0,5/0,75 h).
                jetzt = ts
                roh, angewandt_prod = verh.get(ts, (None, None))
                if roh is not None and np.isfinite(roh):
                    geklemmt = float(np.clip(roh, 1.0 / p["max_factor"], p["max_factor"]))
                    angewandt = stabilize_intraday_ratio(
                        geklemmt, zustand["ratio"], deadband=p["deadband"],
                        max_step=p["max_step"] if zustand["seeded"] else 0.0)
                    zustand.update(ratio=angewandt, seeded=True)
                else:
                    angewandt = None
                for feld, bfeld in (("pv_w", "pv_without_nowcast_w"),
                                    ("pv10_w", "pv10_without_nowcast_w")):
                    if archiv is None or bfeld not in archiv or getattr(inp, feld, None) is None:
                        continue
                    original = pd.Series(getattr(inp, feld), index=ii)
                    basis = pd.to_numeric(archiv[bfeld], errors="coerce").reindex(ii).fillna(original)
                    variante = _korrigiert(basis, angewandt, jetzt, p)
                    produktiv = _korrigiert(basis, angewandt_prod, jetzt, p_prod)
                    # Den laufenden Slot mischt der Betrieb teils mit dem
                    # Live-Messwert (am 24.09. um 12:00: 1107 W statt 2760).
                    # Die Variante wird deshalb ANTEILIG auf den archivierten
                    # Wert uebertragen; fuer 'ist' ist das die Identitaet.
                    if produktiv.iloc[0] > 0:
                        variante.iloc[0] = (original.iloc[0] * variante.iloc[0]
                                            / produktiv.iloc[0])
                    else:
                        variante.iloc[0] = original.iloc[0]
                    if name == "ist" and feld == "pv_w":
                        abw = float((produktiv.iloc[1:] - original.iloc[1:]).abs().max())
                        if abw > pruef_max:
                            pruef_max, pruef_wo = abw, ts
                    setattr(inp, feld, variante.to_numpy(float))
                inp.initial_house_soc_wh = soc
                eingabe = (np.asarray(inp.pv_w, float).copy(),
                           None if getattr(inp, "pv10_w", None) is None
                           else np.asarray(inp.pv10_w, float).copy(), soc)
                vorbild = ist_je_lauf.get(ts)
                gleich = (name != "ist" and vorbild is not None
                          and abs(vorbild["soc"] - soc) < 0.5
                          and np.allclose(vorbild["pv"], eingabe[0], atol=0.5)
                          and (eingabe[1] is None or vorbild["pv10"] is None
                               or np.allclose(vorbild["pv10"], eingabe[1], atol=0.5)))
                if gleich:
                    letzte = vorbild["zeile"]
                else:
                    res = Optimizer(cfg_t, store_warm=False, stabilize_plan=False).solve(inp)
                    loesungen += 1
                    if not res.infeasible and not res.table.empty:
                        letzte = res.table.iloc[0].to_dict()
                if name == "ist":
                    ist_je_lauf[ts] = {"pv": eingabe[0], "pv10": eingabe[1],
                                       "soc": soc, "zeile": letzte}
            zeile = letzte or {}
            soc, netz = _ausfuehren(zeile, pv_ist[k], haus_ist[k], soc, hb, dt)
            imp, exp = max(0.0, netz), max(0.0, -netz)
            if cfg.inverter.max_export_w is not None:
                exp = min(exp, float(cfg.inverter.max_export_w))
            kosten += (imp * preis[k] - exp * einsp[k]) * dt / 1000.0
            bezug += imp * dt / 1000.0
            einsp_kwh += exp * dt / 1000.0
            netzladen_kwh += float(zeile.get("batt_grid_charge_w", 0.0) or 0.0) * dt / 1000.0
        tw = terminal_credit_ct(cfg, ist["price_ct_kwh"], ist["feedin_ct_kwh"], soc)
        ergebnis[name] = {"kosten": kosten, "netto": kosten - tw,
                          "loesungen": loesungen,
                          "end_soc": 100.0 * soc / hb.capacity_wh, "bezug": bezug,
                          "einsp": einsp_kwh, "netzladen": netzladen_kwh}
    g = ist.get("grid_w")
    real = None
    if g is not None:
        g = np.asarray(g, dtype=float)
        real = float(np.nansum((np.maximum(0, g) * preis - np.maximum(0, -g) * einsp) * dt / 1000.0))
    return {"v": ergebnis, "real": real, "pruef": pruef_max, "pruef_wo": pruef_wo,
            "fremd": fremd}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--tage", type=int, default=5)
    ap.add_argument("--von")
    ap.add_argument("--bis")
    ap.add_argument("--variante", action="append")
    ap.add_argument("--zeitlimit", type=float, default=60.0)
    ap.add_argument("--protokoll",
                    help="CSV, an die je Tag und Variante eine Zeile angehaengt "
                         "wird; schon protokollierte Tage werden uebersprungen. "
                         "Die Snapshots reichen nur ~10 Tage zurueck - ueber "
                         "Wochen sammeln geht nur so.")
    args = ap.parse_args()
    cfg = load_config(args.config)
    tz = cfg.general.timezone
    varianten = [_variante(t, cfg.forecast) for t in (args.variante or STANDARD_VARIANTEN)]
    if varianten[0][0] != "ist":
        varianten.insert(0, _variante("ist", cfg.forecast))
    if args.von:
        tage = list(pd.date_range(pd.Timestamp(args.von).date(),
                                  pd.Timestamp(args.bis or args.von).date(), freq="D").date)
    else:
        h = pd.Timestamp.now(tz=tz).normalize()
        tage = [(h - pd.Timedelta(days=n)).date() for n in range(args.tage, 0, -1)]

    print("PV-Nowcast rollierend auf Ist-Daten (Rahmenausfuehrung)")
    for n, p in varianten:
        print(f"  {n:>9}: max_factor {p['max_factor']:.2f} (Boden {1/p['max_factor']:.2f}), "
              f"max_step {p['max_step']:.2f}, slots {p['slots']}, decay {p['decay']:.1f} h")
    print(f"\n{'Tag':>11} {'gemessen':>9} {'ist roh':>8}"
          + "".join(f"{n:>11}" for n, _ in varianten)
          + "   (gemessen/roh = Kosten; Varianten netto, als Differenz zu 'ist')",
          flush=True)
    schon = set()
    if args.protokoll and os.path.exists(args.protokoll):
        alt_df = pd.read_csv(args.protokoll)
        schon = set(alt_df["tag"].astype(str))
    heute = pd.Timestamp.now(tz=tz).date()
    con = sqlite3.connect(cfg.e3dc_rscp.history_db_path)
    summe = {n: 0.0 for n, _ in varianten}
    ok, t_start = 0, time.time()
    for tag in tage:
        if str(tag) in schon:
            print(f"{tag!s:>11}  schon protokolliert", flush=True)
            continue
        erg, grund = rechne_tag(cfg, con, tag, varianten, args.zeitlimit)
        if erg is None:
            print(f"{tag!s:>11}  uebersprungen: {grund}", flush=True)
            continue
        if args.protokoll and tag != heute:       # Teiltage nie festschreiben
            zeilen = []
            for n, p in varianten:
                v = erg["v"][n]
                zeilen.append({
                    "tag": str(tag), "variante": n,
                    "max_factor": p["max_factor"], "max_step": p["max_step"],
                    "slots": p["slots"], "decay": p["decay"],
                    "netto_ct": round(v["netto"], 2),
                    "diff_zu_ist_ct": round(v["netto"] - erg["v"]["ist"]["netto"], 2),
                    "netzladen_kwh": round(v["netzladen"], 3),
                    "bezug_kwh": round(v["bezug"], 3),
                    "loesungen": v["loesungen"],
                    "nachbau_abw_w": round(erg["pruef"], 1),
                    "gerechnet": pd.Timestamp.now(tz=tz).isoformat(timespec="seconds")})
            neu_df = pd.DataFrame(zeilen)
            neu_df.to_csv(args.protokoll, mode="a", index=False,
                          header=not os.path.exists(args.protokoll))
        ok += 1
        basis = erg["v"]["ist"]["netto"]
        # "gemessen" ist die echte Stromrechnung, "ist roh" dieselbe Groesse aus
        # der Nachrechnung - so sieht man, ob die Rahmenausfuehrung die
        # Wirklichkeit trifft. Die Spalte "ist" ist netto (mit Terminalwert).
        zeile = (f"{tag!s:>11} "
                 f"{erg['real'] if erg['real'] is not None else float('nan'):9.1f} "
                 f"{erg['v']['ist']['kosten']:8.1f}")
        for n, _ in varianten:
            summe[n] += erg["v"][n]["netto"]
            zeile += (f"{basis:11.1f}" if n == "ist"
                      else f"{erg['v'][n]['netto'] - basis:+11.1f}")
        wo = f" ({erg['pruef_wo']:%H:%M})" if erg.get("pruef_wo") is not None else ""
        print(zeile + f"   Nachbau-Abw. {erg['pruef']:.0f} W{wo}, ohne eigenen "
              f"Archivstand {erg['fremd']}", flush=True)
        for n, _ in varianten:
            v = erg["v"][n]
            print(f"{'':>22}{n:>9}: Netzladen {v['netzladen']:5.2f} kWh, Bezug {v['bezug']:5.2f}, "
                  f"Einsp. {v['einsp']:5.2f}, Ende {v['end_soc']:5.1f} %, "
                  f"{v['loesungen']} Loesungen", flush=True)
    con.close()
    if ok:
        print(f"\n{'Summe':>11} {'':>9} {'':>8}" + "".join(
            (f"{summe[n]:11.1f}" if n == "ist" else f"{summe[n]-summe['ist']:+11.1f}")
            for n, _ in varianten))
        print(f"\n{ok} Tage in {time.time()-t_start:.0f} s. Negativ = billiger als 'ist'.")


if __name__ == "__main__":
    main()
