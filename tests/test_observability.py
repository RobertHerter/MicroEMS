"""Beobachtbarkeits-Auswertungen (ems/observability): Ersparnis über die Zeit
und Prognosegüte – reine Leser/Aggregatoren über die lokale Historie."""
from __future__ import annotations

import pandas as pd
import pytest

from ems.observability import (_metrics, _wape, battery_health,
                               forecast_accuracy, savings_drivers,
                               savings_over_time)
from tests.test_synthetic import make_config


def _row(saved, imp=5.0, exp=1.0, meter=1.5, baseline=None, ok=True):
    baseline = baseline if baseline is not None else meter + saved
    return {"n_slots": 96,
            "meter": {"import_kwh": imp, "export_kwh": exp, "net_cost_eur": meter},
            "baseline": {"net_cost_eur": baseline, "end_soc_wh": None},
            "saved_eur": saved, "balance": {"ok": ok},
            "tracker": None, "computed_ts": None}


def test_wape_and_metrics():
    assert _wape([100.0, 100.0], [110.0, 90.0]) == 10.0
    assert _wape([0.0, 0.0], [1.0, 1.0]) == 0.0        # leere Basis -> 0
    m = _metrics([100.0, 200.0], [120.0, 180.0])
    assert m["n"] == 2 and m["bias_w"] == 0.0 and m["mae_w"] == 20.0
    assert _metrics([], [])["n"] == 0


def test_savings_over_time_aggregates_day_week_month(tmp_path):
    from ems.local_history import write_savings_validated
    db = str(tmp_path / "h.sqlite")
    # zwei Tage in derselben ISO-Woche + ein Tag im Folgemonat
    write_savings_validated(db, "2026-01-05", _row(1.0))   # Mo KW02
    write_savings_validated(db, "2026-01-06", _row(2.0))   # Di KW02
    write_savings_validated(db, "2026-02-02", _row(3.0))   # Feb
    out = savings_over_time(db)
    assert out["days"] == 3
    assert out["total_saved_eur"] == 6.0
    assert len(out["daily"]) == 3
    weekly = {w["period"]: w for w in out["weekly"]}
    assert weekly["2026-KW02"]["saved_eur"] == 3.0 and weekly["2026-KW02"]["days"] == 2
    monthly = {m["period"]: m for m in out["monthly"]}
    assert monthly["2026-01"]["saved_eur"] == 3.0
    assert monthly["2026-02"]["saved_eur"] == 3.0


def test_savings_over_time_empty(tmp_path):
    out = savings_over_time(str(tmp_path / "leer.sqlite"))
    assert out == {"days": 0, "total_saved_eur": 0.0,
                   "daily": [], "weekly": [], "monthly": []}


def test_battery_health_from_actuals(tmp_path):
    """Durchsatz/äquiv. Vollzyklen und Vollstand-Verweildauer aus den Ist-Werten."""
    from ems.local_history import write_actuals
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    db = cfg.e3dc_rscp.history_db_path
    step = pd.Timedelta(minutes=cfg.general.slot_minutes)
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(step)
    for i in range(8):                              # 4x laden, SoC ab Slot 2 = 100 %
        write_actuals(db, now - step * (8 - i), {
            "battery_w": 2000.0 if i < 4 else 0.0,
            "soc_percent": 100.0 if i >= 2 else 50.0,
            "pv_w": 0.0, "house_load_w": 0.0, "grid_w": 0.0})
    h = battery_health(cfg, days=1)
    assert h["n"] == 8
    assert h["throughput_kwh"] > 0.0 and h["cycles_equiv"] >= 0.0
    assert h["soc_max_pct"] == 100.0 and h["time_full_pct"] == 75.0


def test_savings_drivers_from_actuals(tmp_path):
    """#5: Treiber (Eigenverbrauch/Autarkie) aus den Ist-Werten."""
    from ems.local_history import write_actuals
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    db = cfg.e3dc_rscp.history_db_path
    step = pd.Timedelta(minutes=cfg.general.slot_minutes)
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(step)
    for i in range(4):                     # PV 2000, Last 500, Einspeisung 1000 W
        write_actuals(db, now - step * (4 - i), {
            "pv_w": 2000.0, "house_load_w": 500.0, "grid_w": -1000.0,
            "battery_w": 0.0, "soc_percent": 80.0})
    d = savings_drivers(cfg, days=1)
    assert d["n"] == 4
    assert d["pv_kwh"] == pytest.approx(2.0) and d["export_kwh"] == pytest.approx(1.0)
    assert d["self_consumed_kwh"] == pytest.approx(1.0)
    assert d["self_consumption_pct"] == pytest.approx(50.0)
    assert d["import_kwh"] == pytest.approx(0.0)
    assert d["autarky_pct"] == pytest.approx(100.0)   # kein Netzbezug


def test_battery_health_empty(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "leer.sqlite")
    h = battery_health(cfg, days=7)
    assert h["n"] == 0 and h["throughput_kwh"] is None


def test_forecast_accuracy_graceful_on_empty_history(tmp_path):
    """Ohne Historie liefert forecast_accuracy leere, aber wohlgeformte Metriken
    (n=0) statt zu werfen."""
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    out = forecast_accuracy(cfg, days=7)
    assert out["days"] == 7
    assert out["pv"]["n"] == 0 and out["load"]["n"] == 0
