"""Tests für den Invarianten-Validator: sauberer Plan -> keine Fehler,
manipulierte Pläne -> genau der erwartete Verstoß."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.optimizer import Optimizer, OptimizerInputs
from ems.validate import economic_comparison, validate_plan
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _solve(cfg, **kw):
    idx = pd.date_range("2026-06-10 00:00", "2026-06-11 00:00", freq="15min",
                        tz=TZ, inclusive="left")
    n = len(idx)
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    pv = np.clip(7000 * np.exp(-((hour - 13) ** 2) / 7), 0, None)
    defaults = dict(index=idx, house_load_w=np.full(n, 500.0), pv_w=pv,
                    price_ct_kwh=np.full(n, 25.0), feedin_ct_kwh=np.full(n, 8.0),
                    initial_house_soc_wh=3000.0)
    defaults.update(kw)
    inp = OptimizerInputs(**defaults)
    return inp, Optimizer(cfg).solve(inp)


def _rules(viols, severity=None):
    return {x.rule for x in viols if severity is None or x.severity == severity}


def test_economic_comparison_matches_worse_than_baseline_check():
    """Regression: backtest.py hatte früher eine EIGENE, abweichende
    Terminalwert-Metrik (flacher Mittelpreis statt der konkaven Kurve) und
    meldete dadurch einen perfekt informierten, beweisbar optimalen Plan an
    ~93 % der Tage als "schlechter als Baseline" - reines Bewertungs-
    artefakt, kein echtes Modellproblem (siehe ems-projekt-entscheidungen).
    economic_comparison() ist jetzt die EINZIGE Stelle für diese Rechnung;
    dieser Test verankert, dass sie mit dem echten Zielterm übereinstimmt:
    ein perfekt informierter Plan darf nie schlechter als die Baseline sein."""
    cfg = make_config()
    inp, res = _solve(cfg)
    plan_cost, base_cost = economic_comparison(cfg, res, inp)
    assert plan_cost <= base_cost + 1e-6, \
        "perfekt informierter Plan schlechter als Baseline - Metrik prüfen"
    viols = validate_plan(cfg, res, inp)
    assert "econ.worse_than_baseline" not in _rules(viols)


def test_clean_plan_has_no_errors():
    cfg = make_config()
    inp, res = _solve(cfg)
    viols = validate_plan(cfg, res, inp)
    assert not _rules(viols, "error"), f"unerwartete Fehler: {_rules(viols, 'error')}"


def test_detects_soc_out_of_bounds():
    cfg = make_config()
    inp, res = _solve(cfg)
    res.table.loc[res.table.index[10], "house_soc_wh"] = 1e6
    assert "soc.bounds" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_simultaneous_charge_discharge():
    cfg = make_config()
    inp, res = _solve(cfg)
    i = res.table.index[30]
    res.table.loc[i, "batt_dc_charge_w"] = 2000
    res.table.loc[i, "batt_discharge_w"] = 2000
    assert "mutex.charge_discharge" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_disguised_grid_charge():
    """DC-Laden über den PV-Überschuss hinaus (getarntes Netzladen)."""
    cfg = make_config()
    inp, res = _solve(cfg)
    i = res.table.index[0]   # nachts: PV=0, also jeder dc>0 ist Überschussverstoß
    res.table.loc[i, "batt_dc_charge_w"] = 1500
    assert "pv.dc_le_surplus" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_discharge_on_surplus():
    cfg = make_config()
    inp, res = _solve(cfg)
    i = res.table.index[52]   # 13:00, PV-Überschuss
    res.table.loc[i, "batt_discharge_w"] = 1000
    res.table.loc[i, "batt_dc_charge_w"] = 0
    assert "battery.no_discharge_on_surplus" in _rules(validate_plan(cfg, res, inp), "error")


def test_detects_export_cap_violation():
    cfg = make_config()
    cfg.inverter.max_export_w = 2000.0
    inp, res = _solve(cfg)
    # sauber gelöst -> jetzt künstlich überschreiten
    res.table.loc[res.table.index[50], "grid_export_w"] = 9000
    assert "grid.export_cap" in _rules(validate_plan(cfg, res, inp), "error")


def test_peak_shave_not_flagged_as_worse_than_baseline():
    """Regression: an einem Peak-Tag kappt der Optimierer die Einspeise-SPITZE
    (peak_charge_weight) und verzichtet dafür bewusst auf ein paar ct Einspeise-
    Erlös - ein Netz-/Regulatorik-Nutzen, den die reine Cash-Baseline nicht kennt.
    Die naive Baseline speist die volle Spitze ein und wirkt dadurch günstiger.
    Der Validator muss denselben Spitzen-Malus auf BEIDE Seiten anwenden, sonst
    meldet er den beabsichtigten Peak-Shave als econ.worse_than_baseline.

    Statt die (mehrtägige, lastgekoppelte) Live-Situation nachzubauen, wird der
    Verzicht direkt in die Plantabelle injiziert - wie die übrigen Tests hier."""
    cfg = make_config()
    cfg.inverter.max_export_w = None               # Baseline speist frei ein
    inp, res = _solve(cfg, feedin_ct_kwh=np.full(96, 14.0),
                      pv_w=np.clip(9000 * np.exp(
                          -((np.arange(96) * 0.25 - 13) ** 2) / 9), 0, None),
                      initial_house_soc_wh=3000.0)
    t = res.table
    # Peak-Shave-Verzicht injizieren: Mittags-Fenster als "peak", Einspeisung auf
    # eine niedrige Linie gekappt (Baseline speist deutlich mehr ein), Cash dafür
    # bewusst teurer. Ohne die symmetrische Spitzen-Gutschrift wäre der Plan hier
    # ~1,3 € "teurer" als die Baseline -> Fehlalarm.
    mid = (t.index.hour >= 11) & (t.index.hour < 15)
    t.loc[mid, "mode"] = "peak"
    t.loc[mid, "grid_export_w"] = 1500.0
    t.loc[mid, "slot_cost_ct"] = t.loc[mid, "slot_cost_ct"] + 8.0
    warns = _rules(validate_plan(cfg, res, inp), "warning")
    assert "econ.worse_than_baseline" not in warns, \
        "beabsichtigter Peak-Shave fälschlich als teurer-als-Baseline gemeldet"


def test_debug_snapshot_roundtrip(tmp_path):
    """Snapshot muss valide JSON sein (auch mit mode-Spalte und time-Objekten)."""
    import json
    from ems.debugdump import save_snapshot
    cfg = make_config()
    cfg.report.snapshot_path = str(tmp_path / "snap.json")
    inp, res = _solve(cfg)
    viols = validate_plan(cfg, res, inp)
    path = save_snapshot(cfg, res.table.index[0], inp, res, viols, drift_mae=1.2)
    snap = json.load(open(path))
    assert snap["inputs"]["pv_w"] and snap["plan"]["house_soc_wh"]
    assert snap["drift_soc_mae_pp"] == 1.2
    assert "config" in snap and "optimization" in snap["config"]
