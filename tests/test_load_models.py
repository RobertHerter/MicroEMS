import numpy as np
import pandas as pd
import pytest

from ems import load_models
from ems.config import ControllableLoad, LoadStage
from ems.forecast import LoadForecaster
from ems.local_history import (read_live_slot_averages, read_load_stage_power,
                               write_live_sample, write_load_feedback)
from tests.test_synthetic import make_config


TZ = "Europe/Berlin"


def test_live_samples_are_time_weighted_into_slots(tmp_path):
    path = str(tmp_path / "history.sqlite")
    start = pd.Timestamp("2026-07-27 10:00", tz=TZ)
    for seconds in range(0, 306, 5):
        write_live_sample(path, start + pd.Timedelta(seconds=seconds), {
            "house_load_w": 1000.0 + seconds,
            "pv_w": 2000.0,
            "grid_w": -500.0,
            "battery_w": 500.0,
            "wallbox_w": 0.0,
        })
    frame = read_live_slot_averages(
        path, start, start + pd.Timedelta(minutes=15), TZ, 15,
        min_coverage_seconds=180, max_gap_seconds=10)
    # Linearer Verlauf 1000..1305 W über 305 s -> Mittel 1152,5 W.
    assert frame.loc[start, "house_w"] == pytest.approx(1152.5, abs=0.1)
    assert frame.loc[start, "pv_w"] == pytest.approx(2000.0)


def test_live_samples_do_not_bridge_large_gaps(tmp_path):
    path = str(tmp_path / "history.sqlite")
    start = pd.Timestamp("2026-07-27 10:00", tz=TZ)
    write_live_sample(path, start, {"house_load_w": 1000.0})
    write_live_sample(
        path, start + pd.Timedelta(minutes=5), {"house_load_w": 1000.0})
    frame = read_live_slot_averages(
        path, start, start + pd.Timedelta(minutes=15), TZ, 15,
        min_coverage_seconds=30, max_gap_seconds=10)
    assert frame.empty or frame["house_w"].dropna().empty


def test_deferrable_power_feedback_is_read_for_dashboard(tmp_path):
    path = str(tmp_path / "history.sqlite")
    ts = pd.Timestamp("2026-07-27 10:00", tz=TZ)
    write_load_feedback(path, ts, "Waschmaschine", "__load__", {
        "on": True, "power_w": 1842.0, "fresh": True, "age_seconds": 1.0,
    })

    power = read_load_stage_power(
        path, "Waschmaschine", ["__load__"], ts,
        ts + pd.Timedelta(minutes=15), TZ)

    assert power["__load__"].loc[ts] == 1842.0


def test_controllable_feedback_is_removed_from_base_history(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "history.sqlite")
    cfg.forecast.disaggregation_min_samples = 3
    cfg.controllable_loads = [
        ControllableLoad(
            name="Pool", type="thermal", enabled=True,
            stages=[LoadStage(
                "WP", power_w=1000.0, heat_w=4000.0,
                feedback_topic="pool/wp/on")])
    ]
    start = pd.Timestamp("2026-07-20 00:00", tz=TZ)
    index = pd.date_range(start, periods=4 * 96, freq="15min")
    embedded = pd.Series(
        np.where(index.hour == 10, 1000.0, 0.0), index=index)
    history = pd.Series(1500.0, index=index) + embedded
    # Drei vollständige Rückmeldungstage reichen für das Erwartungsprofil.
    for ts in index[index < start + pd.Timedelta(days=3)]:
        on = ts.hour == 10
        write_load_feedback(
            cfg.e3dc_rscp.history_db_path, ts, "Pool", "WP", {
                "on": on, "power_w": 1000.0 if on else 0.0,
                "fresh": True, "age_seconds": 0.0,
            })
    forecaster = LoadForecaster(cfg)
    training, evaluation, diag = load_models.disaggregate(
        cfg, forecaster, history, index[-1] + pd.Timedelta(minutes=15))
    assert training[index.hour == 10].median() == pytest.approx(1500.0)
    assert evaluation.notna().sum() == 3 * 96
    assert evaluation.dropna().median() == pytest.approx(1500.0)
    assert diag["sources"] == ["Pool/WP"]


def test_ensemble_weights_prefer_better_model_after_six_folds():
    cfg = make_config()
    cfg.forecast.load_ensemble_min_folds = 6
    rows = []
    for day in range(6):
        for lead in (1.0, 8.0, 30.0):
            rows.append({
                "fold": f"2026-07-{day + 1:02d}", "lead_h": lead,
                "bucket": load_models._bucket_name(
                    lead, cfg.forecast.load_ensemble_horizon_hours),
                "actual": 1000.0, "similar_days": 1020.0, "ml": 1400.0,
            })
    weights, diagnostics = load_models._learn_ensemble(
        cfg, pd.DataFrame(rows))
    assert all(item["learned"] for item in diagnostics.values())
    assert all(value["similar_days"] > value["ml"]
               for value in weights.values())


def test_temperature_residual_requires_and_improves_real_folds():
    cfg = make_config()
    cfg.forecast.temperature_residual_min_folds = 6
    cfg.forecast.temperature_residual_min_samples = 24
    rows = []
    for day in range(6):
        for quarter in range(8):
            temp = 22.0 + quarter
            base = 1000.0
            rows.append({
                "fold": f"2026-07-{day + 1:02d}", "actual": base
                        + 80.0 * (temp - 20.0),
                "base": base, "temp": temp, "temp_24h": temp - 1.0,
            })
    model, diag = load_models._learn_temperature(
        cfg, pd.DataFrame(rows))
    assert diag["learned"]
    point = pd.Series(
        1000.0, index=pd.date_range(
            "2026-07-27 10:00", periods=8, freq="15min", tz=TZ))
    temp = pd.Series(np.linspace(24.0, 28.0, 8), index=point.index)
    corrected, delta = load_models.apply_temperature_residual(
        point, temp, model, 0.35)
    assert corrected.iloc[-1] > point.iloc[-1]
    assert delta.max() <= 350.0 + 1e-9


def test_measurement_beats_the_projected_profile(monkeypatch):
    """Wo die Last GEMESSEN wurde, muss die Messung abgezogen werden - nicht das
    Erwartungsprofil. Vorher wurde ueberall das Profil abgezogen, auch auf
    Slots mit exakter Rueckmeldung, an denen die Last nachweislich stillstand:
    die Nachtprognose lag dadurch bei 400 W statt real 1200 W.
    """
    import pandas as pd

    from ems import load_models as lm
    from ems.config import ControllableLoad, LoadStage
    from ems.forecast import LoadForecaster
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.forecast.disaggregate_controllable_loads = True
    cfg.forecast.disaggregation_project_unmeasured = False
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        stages=[LoadStage("klein", 400, 1000,
                          power_topic="homie/pool/power")])]
    idx = pd.date_range("2026-07-20", periods=96, freq="15min",
                        tz=cfg.general.timezone)
    total = pd.Series(1200.0, index=idx)          # echte Hauslast
    measured = pd.Series(0.0, index=idx)          # Pumpe stand still
    complete = pd.Series(True, index=idx)

    monkeypatch.setattr(lm, "read_controllable_load_power",
                        lambda *a, **k: (measured, complete, ["Pool/klein"]))
    # Profil, das faelschlich 800 W erwartet - darf hier nichts abziehen.
    monkeypatch.setattr(lm, "_embedded_profile",
                        lambda *a, **k: pd.Series(800.0, index=idx))
    training, evaluation, diag = lm.disaggregate(
        cfg, LoadForecaster(cfg), total, idx[-1])
    assert training.median() == pytest.approx(1200.0)
    assert diag["coverage_percent"] == pytest.approx(100.0)
