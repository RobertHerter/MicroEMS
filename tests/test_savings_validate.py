"""Tests für die Ersparnis-Gegenprüfung gegen die Zählerwerte (reine Logik)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.savings_validate import reconcile
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _scenario():
    """4 Slots (Wh je Slot), Energiebilanz je Slot exakt geschlossen."""
    idx = pd.date_range("2026-01-20 00:00", periods=4, freq="15min", tz=TZ)
    meter = pd.DataFrame({
        "pv_wh":          [0.0, 1000.0,   0.0,   0.0],
        "load_wh":        [500.0, 400.0, 400.0, 800.0],
        "bat_in_wh":      [0.0,  400.0,   0.0,   0.0],
        "bat_out_wh":     [0.0,    0.0, 300.0,   0.0],
        "grid_import_wh": [500.0,   0.0, 100.0, 800.0],
        "grid_export_wh": [0.0,  200.0,   0.0,   0.0],
    }, index=idx)
    price = pd.Series(30.0, index=idx)
    feedin = pd.Series(8.0, index=idx)
    dt = 0.25
    # grid_w konsistent zum Zähler: (+Bezug / -Einspeisung), W
    grid_w = pd.Series([500, -200, 100, 800], index=idx, dtype=float) / dt
    return meter, price, feedin, grid_w


def test_reconcile_matches_meter_and_detects_correct_sign():
    cfg = make_config()
    meter, price, feedin, grid_w = _scenario()
    r = reconcile(meter, price, feedin, cfg, actual_grid_w=grid_w,
                  soc0_wh=cfg.house_battery.min_soc_wh)

    assert r["n_slots"] == 4
    assert r["balance"]["ok"] and r["balance"]["max_abs_wh"] < 1.0
    # Zähler: 1.4 kWh Bezug, 0.2 kWh Einspeisung -> (1.4*30 - 0.2*8)/100 = 0.40 €
    assert r["meter"]["import_kwh"] == pytest.approx(1.4)
    assert r["meter"]["export_kwh"] == pytest.approx(0.2)
    assert r["meter"]["net_cost_eur"] == pytest.approx(0.40, abs=0.01)
    # Baseline wird gerechnet und die Ersparnis ist konsistent zur Differenz
    assert r["saved_eur"] == pytest.approx(
        round(r["baseline"]["net_cost_eur"] - r["meter"]["net_cost_eur"], 2), abs=0.01)
    # Tracking-Signal deckt sich mit dem Zähler -> Vorzeichen ok, Kosten ~gleich
    assert r["tracker"]["sign_ok"] is True
    assert r["tracker"]["import_kwh"] == pytest.approx(1.4)
    assert r["tracker"]["cost_delta_eur"] == pytest.approx(0.0, abs=0.01)


def test_reconcile_flags_inverted_grid_sign():
    cfg = make_config()
    meter, price, feedin, grid_w = _scenario()
    r = reconcile(meter, price, feedin, cfg, actual_grid_w=-grid_w,
                  soc0_wh=cfg.house_battery.min_soc_wh)
    # Vertauschtes Vorzeichen: Bezug/Einspeisung sind vertauscht -> Alarm
    assert r["tracker"]["sign_ok"] is False


def test_reconcile_warns_when_zeroing_active_but_spot_missing(caplog):
    """P3#5: ist zero_at_negative_price aktiv, aber keine Spot-Reihe übergeben,
    darf die Nullung nicht still übersprungen werden (sonst überschätzte
    Vergütung) – es muss eine Warnung erscheinen; die Vergütung bleibt erhalten."""
    cfg = make_config()
    cfg.feed_in.zero_at_negative_price = True
    meter, price, feedin, grid_w = _scenario()
    with caplog.at_level("WARNING", logger="ems.savings_validate"):
        r = reconcile(meter, price, feedin, cfg, actual_grid_w=grid_w,
                      soc0_wh=cfg.house_battery.min_soc_wh)   # spot fehlt
    assert any("Spot-Reihe" in m for m in caplog.messages)
    assert r["meter"]["export_kwh"] == pytest.approx(0.2)      # nicht genullt
    assert r["meter"]["net_cost_eur"] == pytest.approx(0.40, abs=0.01)


def test_reconcile_zeroes_feedin_at_negative_spot():
    """Mit echter Spot-Reihe wird die Einspeisung in Negativpreis-Slots genullt."""
    cfg = make_config()
    cfg.feed_in.zero_at_negative_price = True
    meter, price, feedin, grid_w = _scenario()
    spot = pd.Series([30.0, -3.0, 30.0, 30.0], index=meter.index)  # Slot 1 negativ
    r = reconcile(meter, price, feedin, cfg, actual_grid_w=grid_w,
                  soc0_wh=cfg.house_battery.min_soc_wh, spot=spot)
    # 0.2 kWh Einspeisung im Negativslot -> keine Vergütung mehr -> Kosten höher
    assert r["meter"]["net_cost_eur"] == pytest.approx(0.40 + 0.2 * 8 / 100.0, abs=0.01)


def test_reconcile_handles_empty_after_dropping_gaps():
    cfg = make_config()
    meter, price, feedin, _ = _scenario()
    price = price * np.nan            # keine Preise -> alle Slots verworfen
    r = reconcile(meter, price, feedin, cfg)
    assert r["n_slots"] == 0 and r.get("insufficient")


def test_savings_validated_roundtrip_and_idempotent(tmp_path):
    from ems import local_history as lh
    cfg = make_config()
    meter, price, feedin, grid_w = _scenario()
    r = reconcile(meter, price, feedin, cfg, actual_grid_w=grid_w,
                  soc0_wh=cfg.house_battery.min_soc_wh)
    r["computed_ts"] = pd.Timestamp("2026-01-21 02:45", tz=TZ)
    db = str(tmp_path / "h.sqlite")
    lh.write_savings_validated(db, "2026-01-20", r)
    lh.write_savings_validated(db, "2026-01-20", r)     # REPLACE -> nur 1 Zeile
    df = lh.read_savings_validated(db)
    assert len(df) == 1 and df.iloc[0]["day"] == "2026-01-20"
    assert df.iloc[0]["saved_eur"] == pytest.approx(r["saved_eur"])
    assert df.iloc[0]["meter_cost_eur"] == pytest.approx(r["meter"]["net_cost_eur"])
    assert int(df.iloc[0]["balance_ok"]) == 1
    # start_day-Filter
    assert lh.read_savings_validated(db, "2026-02-01").empty
