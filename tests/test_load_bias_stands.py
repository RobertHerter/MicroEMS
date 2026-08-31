"""Der Wächter loest auf dem OPERATIVEN Stand aus, nicht auf dem Tagesstart.

Gemessen am 31.08.2026 ueber 14 Tage: der 00:00-Stand hatte +129 W Bias und
34,0 % WAPE, der Stand, der den Akku im jeweiligen Slot wirklich fuehrte, -21 W
und 28,2 % - nachts +183 gegen +24 W. Der Alarm feuerte wochenlang auf einer
Groesse, die nichts steuert, weil die Neuplanung alle 15 min die Nowcast-
Korrektur einrechnet. Die Ursachensuche ging dadurch ins Modell, wo die
Korrekturkette bereits sauber arbeitete.
"""
from __future__ import annotations

import pandas as pd
import pytest

from ems.drift import DriftMonitor
from ems.local_history import (write_house_load,
                               write_optimizer_forecast_archive)
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
JETZT = pd.Timestamp("2026-08-31 12:00", tz=TZ)
IST_W = 1000.0


def _cfg(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    cfg.monitoring.load_bias_window_days = 3.0
    cfg.monitoring.load_bias_operative_days = 2.0
    cfg.monitoring.load_bias_alert_w = 100.0
    return cfg


def _seed(cfg, *, tagesstart_w, operativ_w):
    """Drei Tage Ist-Last, dazu Mitternachtsstaende und (optional) Slot-Staende."""
    db = cfg.e3dc_rscp.history_db_path
    start = (JETZT - pd.Timedelta(days=3)).normalize()
    idx = pd.date_range(start, JETZT, freq="15min", tz=TZ, inclusive="left")
    write_house_load(db, {ts.tz_convert("UTC").isoformat(): IST_W for ts in idx})

    tag = start
    while tag < JETZT:
        nxt = min(tag + pd.Timedelta(days=1), JETZT)
        tages_idx = idx[(idx >= tag) & (idx < nxt)]
        if len(tages_idx):
            write_optimizer_forecast_archive(db, tag, {
                "house_load_w": pd.Series(tagesstart_w, index=tages_idx)})
        tag = nxt

    if operativ_w is not None:
        # Je Slot ein eigener Stand, ausgegeben KURZ VOR dem Slot.
        for ts in idx:
            write_optimizer_forecast_archive(
                db, ts - pd.Timedelta(seconds=30),
                {"house_load_w": pd.Series([operativ_w], index=[ts])})


def test_a_biased_day_start_does_not_alarm_when_the_operative_stand_is_clean(
        tmp_path):
    """Der reale Fall: Tagesstart +300 W, laufende Fuehrung sauber."""
    cfg = _cfg(tmp_path)
    _seed(cfg, tagesstart_w=IST_W + 300.0, operativ_w=IST_W)

    out = DriftMonitor(cfg).check_load_bias(JETZT)

    assert out is not None
    assert out["alert_basis"] == "operativ"
    assert out["operative_median_w"] == pytest.approx(0.0, abs=5.0)
    assert out["alert"] is False, out
    # Der Tagesstart bleibt sichtbar - als Kontext, nicht als Ausloeser.
    assert out["median_w"] == pytest.approx(300.0, abs=5.0)
    assert out["night_median_w"] == pytest.approx(300.0, abs=5.0)
    # Richtung und kWh gehoeren zum Ausloeser, sonst widersprechen sie der Zahl.
    assert abs(float(out["kwh_per_day"])) < 0.5


def test_a_biased_operative_stand_alarms_even_with_a_clean_day_start(tmp_path):
    """Umgekehrt: die laufende Fuehrung liegt daneben - das muss auffallen."""
    cfg = _cfg(tmp_path)
    _seed(cfg, tagesstart_w=IST_W, operativ_w=IST_W + 400.0)

    out = DriftMonitor(cfg).check_load_bias(JETZT)

    assert out["operative_median_w"] == pytest.approx(400.0, abs=5.0)
    assert out["alert"] is True, out
    assert out["alert_scope"] in ("Nacht", "Tag und Nacht")
    assert out["direction"] == "Prognose zu hoch"
    assert out["median_w"] == pytest.approx(0.0, abs=5.0)


def test_with_only_day_start_snapshots_the_bias_is_never_hidden(tmp_path):
    """Gibt es nur Mitternachtsstaende, IST der Mitternachtsstand der operative.

    read_optimizer_forecast_asof liefert je Slot den letzten Stand davor - ohne
    Slot-Staende ist das der Tagesstart. Die Umstellung kann einen Bias also
    nicht verstecken; sie unterscheidet nur dort, wo ein frischerer Stand
    existiert.
    """
    cfg = _cfg(tmp_path)
    _seed(cfg, tagesstart_w=IST_W + 350.0, operativ_w=None)

    out = DriftMonitor(cfg).check_load_bias(JETZT)

    assert out["operative_n"] > 24
    assert out["operative_median_w"] == pytest.approx(350.0, abs=5.0)
    assert out["alert"] is True, out
    assert out["median_w"] == pytest.approx(350.0, abs=5.0)


def test_a_too_thin_operative_window_falls_back_to_the_day_start(tmp_path):
    """Zu wenige operative Paare (< 24): lieber grob warnen als still sein."""
    cfg = _cfg(tmp_path)
    cfg.monitoring.load_bias_operative_days = 0.1      # ~2,4 h = 9 Slots
    _seed(cfg, tagesstart_w=IST_W + 350.0, operativ_w=IST_W)

    out = DriftMonitor(cfg).check_load_bias(JETZT)

    assert out["operative_n"] < 24
    assert out["alert_basis"].startswith("Tagesstart")
    assert out["alert"] is True, out
