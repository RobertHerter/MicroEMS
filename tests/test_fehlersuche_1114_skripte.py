"""Regressionstests zur Fehlersuche vom 14.11.2026 - Kalibrierung, Drift,
Importskripte und Auswertungswerkzeuge.

Jeder Test haelt einen belegten Fehler fest; im Kopf steht, was vorher herauskam.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
CAP = 22344.0
DT = 0.25
WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lade_skript(relpfad: str):
    """Skript ausserhalb des Pakets (Wurzel, tools/) als Modul laden."""
    name = "skript_" + relpfad.replace("/", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(WURZEL, relpfad))
    modul = importlib.util.module_from_spec(spec)
    sys.modules[name] = modul
    spec.loader.exec_module(modul)
    return modul


# --------------------------------------------------------------------------- #
# 1. Kalibrierung: End-SoC des letzten Slots
# --------------------------------------------------------------------------- #
def _entladung(start, slots, power_w=1000.0, eta=0.80, soc0=95.0):
    """``slots`` Entladezeilen, SoC je Zeile am SLOTBEGINN, unquantisiert."""
    index = pd.date_range(start, periods=slots, freq="15min", tz=TZ)
    je_slot = power_w * DT / eta / CAP * 100.0
    return pd.DataFrame({"battery_w": [-power_w] * slots,
                         "soc": soc0 - np.arange(slots) * je_slot}, index=index)


def test_entladefenster_vor_einer_luecke_zaehlt_den_letzten_slot_nicht():
    """16 Slots Entladung, dann 2 h Ausfall. Vorher: SoC der ersten Zeile NACH
    der Luecke als Fensterende -> 0,533 statt 0,80."""
    from ems.battery_calibration import fit_discharge_efficiency
    a = _entladung("2026-01-10 20:00", 16)
    je_slot = 1000.0 * DT / 0.80 / CAP * 100.0
    # Waehrend des Ausfalls lief die Entladung weiter (8 Slots), danach Ruhe.
    nach = pd.DataFrame(
        {"battery_w": [0.0] * 4, "soc": [95.0 - 24 * je_slot] * 4},
        index=pd.date_range(a.index[-1] + pd.Timedelta(hours=2, minutes=15),
                            periods=4, freq="15min", tz=TZ))
    fit = fit_discharge_efficiency(pd.concat([a, nach]), CAP, DT)
    assert fit.n_windows == 1
    assert fit.windows[0].slots == 15
    assert fit.efficiency == pytest.approx(0.80, abs=0.002)


def test_entladefenster_bis_zum_datenende_zaehlt_den_letzten_slot_nicht():
    """Laufende Entladung bis zur letzten Zeile. Vorher: Energie ueber n Slots
    gegen SoC-Hub ueber n-1 -> 0,853 statt 0,80."""
    from ems.battery_calibration import fit_discharge_efficiency
    fit = fit_discharge_efficiency(_entladung("2026-01-10 20:00", 16), CAP, DT)
    assert fit.n_windows == 1
    assert fit.windows[0].slots == 15
    assert fit.efficiency == pytest.approx(0.80, abs=0.002)


def test_entladefenster_mit_bekanntem_end_soc_bleibt_vollstaendig():
    """Gegenprobe: schliesst eine Zeile lueckenlos an, zaehlt der letzte Slot."""
    from ems.battery_calibration import fit_discharge_efficiency
    a = _entladung("2026-01-10 20:00", 16)
    je_slot = 1000.0 * DT / 0.80 / CAP * 100.0
    ruhe = pd.DataFrame({"battery_w": [0.0], "soc": [95.0 - 16 * je_slot]},
                        index=[a.index[-1] + pd.Timedelta(minutes=15)])
    fit = fit_discharge_efficiency(pd.concat([a, ruhe]), CAP, DT)
    assert fit.windows[0].slots == 16
    assert fit.efficiency == pytest.approx(0.80, abs=0.002)


def test_ladefenster_bis_zum_datenende_ueberschaetzt_die_kapazitaet_nicht():
    """Dieselbe Stelle auf der Ladeseite. Vorher: 12 Slots Energie gegen 11
    Slots SoC-Hub -> Kapazitaet um 12/11 zu gross (24375 statt 22344 Wh)."""
    from ems.battery_calibration import fit_capacity
    slots, power = 12, 4000.0
    index = pd.date_range("2026-07-20 09:00", periods=slots, freq="15min", tz=TZ)
    soc = 30.0 + np.arange(slots) * power * DT / CAP * 100.0
    frame = pd.DataFrame({"battery_w": [power] * slots, "soc": soc}, index=index)
    fit = fit_capacity(frame, DT, 1.0)
    assert fit.n_windows == 1
    assert fit.windows[0].slots == 11
    assert fit.capacity_wh == pytest.approx(CAP, rel=0.001)


# --------------------------------------------------------------------------- #
# 2. Drift: Entprellung ueberlebt die je Zyklus neue Instanz
# --------------------------------------------------------------------------- #
def test_lastprognose_warnung_entprellt_ueber_neue_instanzen(tmp_path, caplog):
    """main legt je Zyklus ``DriftMonitor(config)`` neu an. Vorher lag die
    gemeldete Stufe in der Instanz: die Warnung kam bei jeder Pruefung erneut,
    die Entwarnung nie."""
    from ems.drift import DriftMonitor
    from tests.test_battery_calibration import _seed_load_bias
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    tz = cfg.general.timezone
    _seed_load_bias(cfg.e3dc_rscp.history_db_path, tz,
                    forecast_w=lambda t: 400.0 if t.hour < 6 else 1200.0,
                    actual_w=1200.0)
    now = pd.Timestamp("2026-01-13 12:00", tz=tz)

    def meldungen(stufe, text):
        return [r for r in caplog.records if r.levelno == stufe
                and text in r.getMessage()]

    with caplog.at_level(logging.INFO, logger="ems.drift"):
        for _ in range(4):
            assert DriftMonitor(cfg).check_load_bias(now)["alert"] is True
        assert len(meldungen(logging.WARNING,
                             "Lastprognose-Versatz im OPERATIVEN")) == 1, \
            "Warnung wiederholt sich je Instanz"
        # Schwelle anheben -> kein Alarm mehr: genau EINE Entwarnung.
        cfg.monitoring.load_bias_alert_w = 5000.0
        for _ in range(3):
            assert DriftMonitor(cfg).check_load_bias(now)["alert"] is False
        assert len(meldungen(logging.INFO, "wieder im Rahmen")) == 1


# --------------------------------------------------------------------------- #
# 5. Drift: Tagesfenster sind Kalendertage, auch ueber die Zeitumstellung
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("jetzt, umstellung", [
    ("2026-10-28 12:00", "2026-10-25"),     # 25-h-Tag
    ("2026-04-01 12:00", "2026-03-29"),     # 23-h-Tag
])
def test_lastprognose_tagesfenster_beginnen_um_mitternacht(
        monkeypatch, tmp_path, jetzt, umstellung):
    """Vorher 24-h-Schritte ab ``normalize() - 7 d``: jenseits der Umstellung
    begannen die Fenster um 01:00 bzw. 23:00 - gelesen wurde dann nicht der
    Tagesstart-Stand, und die Fenster passten nicht zum Kalendertag."""
    import ems.local_history as lh
    from ems.drift import DriftMonitor
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    cfg.monitoring.load_bias_window_days = 7.0
    fenster = []

    def fake_asof(_db, issue, start, end, _tz):
        fenster.append((pd.Timestamp(issue), pd.Timestamp(start),
                        pd.Timestamp(end)))
        return None, pd.DataFrame()

    monkeypatch.setattr(lh, "read_optimizer_forecast_asof", fake_asof)
    monkeypatch.setattr(lh, "read_base_house_load",
                        lambda *a, **k: pd.Series(dtype="float64"))
    now = pd.Timestamp(jetzt, tz=TZ)
    assert DriftMonitor(cfg).check_load_bias(now) is None    # keine Daten

    starts = [s for _i, s, _e in fenster]
    assert len(starts) == 8                       # 7 volle Tage + heute
    assert all(s.hour == 0 and s.minute == 0 for s in starts), starts
    assert [s.date() for s in starts] == list(
        pd.date_range(end=now.date(), periods=8, freq="D").date)
    # Der Umstellungstag selbst reicht von Mitternacht bis Mitternacht.
    tag = next((s, e) for _i, s, e in fenster
               if str(s.date()) == umstellung)
    stunden = (tag[1] - tag[0]) / pd.Timedelta(hours=1)
    assert stunden == (25.0 if umstellung.endswith("10-25") else 23.0)


# --------------------------------------------------------------------------- #
# 3. PV-Ist-Import: selbst gemessene Werte bleiben stehen
# --------------------------------------------------------------------------- #
def _pv_w(db):
    con = sqlite3.connect(db)
    try:
        return dict(con.execute("SELECT ts, pv_w FROM actuals").fetchall())
    finally:
        con.close()


def test_write_pv_actual_behaelt_vorhandene_werte(tmp_path):
    """Vorher ``DO UPDATE SET pv_w=excluded.pv_w``: ein zweiter Importlauf
    ersetzte die selbst gemessenen Slotmittel."""
    from ems.local_history import write_pv_actual
    db = str(tmp_path / "h.sqlite")
    a, b, c = ("2026-11-01T10:00:00+00:00", "2026-11-01T10:15:00+00:00",
               "2026-11-01T10:30:00+00:00")
    assert write_pv_actual(db, {a: 1000.0}) == 1
    con = sqlite3.connect(db)
    con.execute("INSERT INTO actuals(ts, house_w) VALUES(?, 500.0)", (b,))
    con.commit()
    con.close()

    # a: vorhanden -> bleibt; b: Zeile da, pv_w NULL -> fuellen; c: neu.
    assert write_pv_actual(db, {a: 111.0, b: 222.0, c: 333.0}) == 2
    assert _pv_w(db) == {a: 1000.0, b: 222.0, c: 333.0}
    con = sqlite3.connect(db)
    assert con.execute("SELECT house_w FROM actuals WHERE ts=?",
                       (b,)).fetchone()[0] == 500.0
    con.close()

    assert write_pv_actual(db, {a: 111.0}, overwrite=True) == 1
    assert _pv_w(db)[a] == 111.0


def test_pv_actual_import_fenster_liegt_auf_dem_slotraster():
    """Vorher ``now`` und ``now - days`` mitten im Slot: beide Rand-Slots
    bekamen ein Mittel ueber nur einen Teil ihrer Messwerte."""
    skript = _lade_skript("pv_actual_import.py")
    start, ende = skript._fenster(
        pd.Timestamp("2026-11-14 10:07:33", tz=TZ), 30, 15)
    assert ende == pd.Timestamp("2026-11-14 10:00", tz=TZ)
    assert start.minute % 15 == 0 and start.second == 0
    assert ende - start == pd.Timedelta(days=30)


@pytest.mark.parametrize("schalter, erwartet", [([], 1000.0),
                                                (["--overwrite"], 250.0)])
def test_pv_actual_import_ueberschreibt_nur_mit_schalter(
        monkeypatch, tmp_path, schalter, erwartet):
    from ems.local_history import write_pv_actual
    skript = _lade_skript("pv_actual_import.py")
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    ts = (pd.Timestamp.now(tz=cfg.general.timezone).floor("15min")
          - pd.Timedelta(hours=2))
    key = ts.tz_convert("UTC").isoformat()
    write_pv_actual(cfg.e3dc_rscp.history_db_path, {key: 1000.0})
    grenzen = []

    class FakeRepo:
        def __init__(self, _cfg):
            pass

        def signal_available(self, _name):
            return True

        def read_slots(self, _name, start, end, fill=False):
            grenzen.append((start, end))
            return pd.Series([250.0], index=[ts])

        def close(self):
            pass

    monkeypatch.setattr(skript, "load_config", lambda _p: cfg)
    monkeypatch.setattr(skript, "InfluxRepository", FakeRepo)
    monkeypatch.setattr(sys, "argv", ["pv_actual_import.py", "--days", "1",
                                      *schalter])
    assert skript.main() == 0
    assert _pv_w(cfg.e3dc_rscp.history_db_path)[key] == erwartet
    for start, end in grenzen:
        assert start.minute % 15 == 0 and start.second == 0
        assert end.minute % 15 == 0 and end.second == 0 \
            and end.microsecond == 0


# --------------------------------------------------------------------------- #
# 4. Solcast-Import: Lesefehler ist nicht "keine Live-Daten"
# --------------------------------------------------------------------------- #
def test_live_start_none_nur_ohne_tabelle_oder_ohne_daten(tmp_path):
    from ems.local_history import write_pv_forecast
    skript = _lade_skript("solcast_import.py")
    db = str(tmp_path / "h.sqlite")
    assert skript._live_start(db) is None               # Tabelle fehlt
    write_pv_forecast(db, "influx_hist",
                      {"2026-01-01T00:00:00+00:00": (1.0, 1.0, 1.0)})
    assert skript._live_start(db) is None               # nur Historie
    write_pv_forecast(db, "ost",
                      {"2026-11-10T08:00:00+00:00": (1.0, 1.0, 1.0),
                       "2026-11-10T07:45:00+00:00": (1.0, 1.0, 1.0)})
    assert skript._live_start(db) == pd.Timestamp("2026-11-10T07:45:00+00:00")


def test_solcast_import_bricht_bei_unlesbarer_datenbank_ab(
        monkeypatch, tmp_path, capsys):
    """Vorher ``except Exception: return None``: eine unlesbare Datenbank galt
    als "noch keine Live-Daten" und der Import schrieb bis JETZT - ueber den
    gesamten Live-Zeitraum."""
    skript = _lade_skript("solcast_import.py")
    db = tmp_path / "kaputt.sqlite"
    db.write_bytes(b"das ist keine SQLite-Datei " * 200)
    with pytest.raises(sqlite3.DatabaseError):
        skript._live_start(str(db))

    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(db)

    def kein_influx(_cfg):
        raise AssertionError("Import laeuft trotz Lesefehler weiter")

    monkeypatch.setattr(skript, "load_config", lambda _p: cfg)
    monkeypatch.setattr(skript, "InfluxRepository", kein_influx)
    monkeypatch.setattr(sys, "argv", ["solcast_import.py"])
    assert skript.main() != 0
    assert "nicht lesbar" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 6. parameter_check: Summen nur ueber Laeufe, die fuer ALLE Varianten zaehlen
# --------------------------------------------------------------------------- #
def test_parameter_check_summiert_nur_vollstaendige_laeufe(capsys):
    """Vorher je Variante eigene Lauf-Menge: ein nur fuer B unloesbarer Lauf
    fehlte in B's Summe (B sah guenstiger aus), LIMIT-Laeufe zaehlten mit."""
    pc = _lade_skript("tools/parameter_check.py")
    werte = [5.0, 0.0]
    summe = {w: 0.0 for w in werte}

    text, ok = pc._werte_lauf(
        werte, {5.0: ("a", 100.0, False), 0.0: ("b", 110.0, False)}, summe)
    assert ok and "nicht gewertet" not in text

    text, ok = pc._werte_lauf(
        werte, {5.0: ("a", 500.0, False), 0.0: None}, summe)
    assert not ok and "unloesbar" in text and "nicht gewertet" in text

    text, ok = pc._werte_lauf(
        werte, {5.0: ("a", 300.0, False), 0.0: ("b", 10.0, True)}, summe)
    assert not ok and "LIMIT" in text and "nicht gewertet" in text

    assert summe == {5.0: 100.0, 0.0: 110.0}
    args = SimpleNamespace(parameter="optimization.x", werte=werte)
    pc._fazit(args, summe, 1, 3)
    aus = capsys.readouterr().out
    assert "gewertet: 1 von 3" in aus
    assert "guenstigster Wert: optimization.x = 5.0" in aus

    pc._fazit(args, {w: 0.0 for w in werte}, 0, 3)
    aus = capsys.readouterr().out
    assert "gewertet: 0 von 3" in aus and "guenstigster" not in aus


# --------------------------------------------------------------------------- #
# 7. pv_nowcast_sweep: geloeschter Snapshot ist kein Absturz
# --------------------------------------------------------------------------- #
def test_nowcast_sweep_payload_ohne_snapshot_ist_none():
    """Vorher ``roh, = ...fetchone()`` -> TypeError, wenn der Snapshot zwischen
    Auflisten und Lesen ausgeduennt wurde."""
    sweep = _lade_skript("tools/pv_nowcast_sweep.py")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE debug_snapshot (id INTEGER PRIMARY KEY, "
                "generated TEXT, payload BLOB)")
    con.execute("INSERT INTO debug_snapshot VALUES (1, 'x', ?)",
                ('{"inputs": {}}',))
    assert sweep._payload(con, 99) is None
    assert sweep._payload(con, 1) == {"inputs": {}}
    con.close()


# --------------------------------------------------------------------------- #
# 8. berechnung_heute: nie ins produktive Dashboard schreiben
# --------------------------------------------------------------------------- #
def test_berechnung_heute_schreibt_nicht_ins_produktive_dashboard(
        monkeypatch, tmp_path, capsys):
    """Vorher ``build_dashboard(cfg, ...)`` mit dem output_path der config.yaml:
    die Beispieldaten ueberschrieben das Live-Dashboard."""
    skript = _lade_skript("berechnung_heute.py")
    produktiv = str(tmp_path / "live" / "dashboard.html")
    cfg = SimpleNamespace(
        general=SimpleNamespace(n_opt_slots=48, timezone=TZ),
        house_battery=SimpleNamespace(capacity_wh=CAP),
        vehicle=SimpleNamespace(capacity_wh=60000.0),
        dashboard=SimpleNamespace(output_path=produktiv))
    geschrieben = []

    class FakeOptimizer:
        def __init__(self, _cfg):
            pass

        def solve(self, inp):
            spalten = ["price_ct_kwh", "pv_w", "house_load_w",
                       "batt_dc_charge_w", "batt_ac_charge_w",
                       "batt_discharge_w", "car_charge_w", "grid_import_w",
                       "grid_export_w", "house_soc_percent"]
            table = pd.DataFrame(0.0, index=inp.index, columns=spalten)
            return SimpleNamespace(table=table, total_cost_ct=0.0, status="ok")

    monkeypatch.setattr(skript, "load_config", lambda _p: cfg)
    monkeypatch.setattr(skript, "Optimizer", FakeOptimizer)
    monkeypatch.setattr(
        skript, "build_dashboard",
        lambda c, *_a, **_k: geschrieben.append(c.dashboard.output_path))
    monkeypatch.chdir(tmp_path)
    skript.main()
    capsys.readouterr()

    assert geschrieben == ["dashboard_heute.html"]
    assert (tmp_path / "steuertabelle_heute.csv").exists()
