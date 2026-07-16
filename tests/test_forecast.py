"""Prognose-Tests (pytest): Rezenz-Gewichtung und Umgang mit Datenlücken."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.forecast import LoadForecaster
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
FREQ = "15min"
START = pd.Timestamp("2026-05-01 00:00", tz=TZ)


def _history(days: int, old_w: float, new_w: float, split_days: int) -> pd.Series:
    """Historie: ältere Tage = old_w, die letzten split_days Tage = new_w."""
    idx = pd.date_range(START - pd.Timedelta(days=days), START,
                        freq=FREQ, inclusive="left")
    age_days = (START - idx).total_seconds() / 86400.0
    values = np.where(age_days <= split_days, new_w, old_w).astype(float)
    return pd.Series(values, index=idx)


def test_recency_weighting_follows_recent_level():
    """Mit kurzer Halbwertszeit dominiert das neue Verbrauchsniveau."""
    cfg = make_config()
    cfg.forecast.half_life_days = 7.0
    cfg.forecast.weight_same_temp = 0.0
    hist = _history(days=60, old_w=200.0, new_w=600.0, split_days=30)
    fc = LoadForecaster(cfg).forecast(hist, START, 96)
    assert float(fc.mean()) > 500.0, \
        f"Rezenz-Gewichtung wirkt nicht (Mittel {fc.mean():.0f} W)"


def test_recency_disabled_averages_all_history():
    """half_life_days=0: alte und neue Tage zählen (fast) gleich."""
    cfg = make_config()
    cfg.forecast.half_life_days = 0.0
    cfg.forecast.weight_same_temp = 0.0
    hist = _history(days=60, old_w=200.0, new_w=600.0, split_days=30)
    fc = LoadForecaster(cfg).forecast(hist, START, 96)
    assert 300.0 < float(fc.mean()) < 500.0, \
        f"Ohne Rezenz sollte das Mittel dazwischen liegen ({fc.mean():.0f} W)"


def test_gaps_in_history_are_skipped():
    """NaN-Lücken (Sensorausfall) führen weder zu NaN noch zu Absturz."""
    cfg = make_config()
    hist = _history(days=30, old_w=400.0, new_w=400.0, split_days=0)
    hist.iloc[200:1200] = np.nan          # mehrtägige Lücke
    hist.iloc[-50:] = np.nan              # Lücke direkt vor 'jetzt'
    fc = LoadForecaster(cfg).forecast(hist, START, 96)
    assert len(fc) == 96
    assert fc.notna().all(), "Prognose enthält NaN"
    assert (fc >= 0).all()
    assert abs(float(fc.mean()) - 400.0) < 50.0


def test_empty_history_returns_zeros():
    cfg = make_config()
    empty = pd.Series(dtype="float64",
                      index=pd.DatetimeIndex([], tz=TZ))
    fc = LoadForecaster(cfg).forecast(empty, START, 96)
    assert len(fc) == 96 and (fc == 0).all()


# ---------------------------------------------------------------------- #
# Intraday-Korrektur
# ---------------------------------------------------------------------- #
def _window(hours=3.0, actual=600.0, predicted=400.0):
    idx = pd.date_range(START - pd.Timedelta(hours=hours), START,
                        freq=FREQ, inclusive="left")
    return pd.Series(actual, index=idx), pd.Series(predicted, index=idx)


def test_intraday_ratio_basic_and_clipping():
    from ems.forecast import intraday_ratio
    a, p = _window(actual=600.0, predicted=400.0)
    assert abs(intraday_ratio(a, p) - 1.5) < 1e-9
    a, p = _window(actual=2000.0, predicted=400.0)     # Verhältnis 5 -> Kappe
    assert intraday_ratio(a, p, max_factor=1.5) == 1.5
    a, p = _window(actual=100.0, predicted=400.0)      # 0.25 -> 1/1.5
    assert abs(intraday_ratio(a, p, max_factor=1.5) - (1 / 1.5)) < 1e-9


def test_intraday_ratio_rejects_thin_data():
    from ems.forecast import intraday_ratio
    a, p = _window()
    assert intraday_ratio(a.iloc[:2], p.iloc[:2]) is None          # zu wenig Slots
    a, p = _window(actual=50.0, predicted=10.0)
    assert intraday_ratio(a, p, min_mean=200.0) is None            # PV-Nacht
    a2 = a.copy(); a2[:] = float("nan")
    assert intraday_ratio(a2, p) is None                           # keine Ist-Werte


def test_intraday_ratio_robust_and_pv_power_gate():
    from ems.forecast import intraday_ratio
    idx = pd.date_range(START, periods=8, freq=FREQ)
    pred = pd.Series([100.0] * 4 + [1500.0] * 4, index=idx)
    actual = pd.Series([500.0] * 4 + [1500.0, 1500.0, 1500.0, 9000.0],
                       index=idx)
    ratio, detail = intraday_ratio(
        actual, pred, robust=True, min_slot_value=1000.0,
        return_details=True)
    assert ratio == 1.0                  # 9-kW-Ausreißer ändert den Median nicht
    assert detail["samples"] == 8 and detail["used_samples"] == 4
    assert detail["observations"]["eligible"].tolist() == [0] * 4 + [1] * 4


def test_intraday_stabilization_deadband_and_step_limit():
    from ems.forecast import stabilize_intraday_ratio
    assert stabilize_intraday_ratio(1.08, 1.0, deadband=0.1) == 1.0
    assert stabilize_intraday_ratio(1.5, 1.0, max_step=0.1) == pytest.approx(1.1)
    assert stabilize_intraday_ratio(0.5, 1.0, max_step=0.1) == pytest.approx(0.9)
    assert stabilize_intraday_ratio(None, 1.2) is None


def test_intraday_diagnostic_archive(tmp_path):
    import sqlite3
    from ems.forecast import intraday_ratio
    from ems.local_history import write_intraday_diagnostic
    a, p = _window(hours=1.0, actual=600.0, predicted=500.0)
    _, details = intraday_ratio(a, p, robust=True, return_details=True)
    db = str(tmp_path / "intraday.sqlite")
    write_intraday_diagnostic(db, START, "load", START - pd.Timedelta(hours=1),
                              details, 1.1)
    con = sqlite3.connect(db)
    summary = con.execute(
        "SELECT raw_ratio, applied_ratio, samples, used_samples "
        "FROM intraday_correction").fetchone()
    windows = con.execute("SELECT count(*) FROM intraday_window").fetchone()[0]
    con.close()
    assert summary == pytest.approx((1.2, 1.1, 4, 4))
    assert windows == 4


def test_operational_series_does_not_bridge_long_gap():
    from ems.main import _complete_operational_series
    idx = pd.date_range(START, periods=20, freq=FREQ)
    src = pd.Series([100.0, 200.0], index=[idx[0], idx[-1]])
    out, fallback_slots = _complete_operational_series(
        src, idx, fallback=999.0, interpolate_limit=2, edge_limit=0)
    assert out.iloc[1:3].lt(999.0).all()
    assert (out.iloc[3:-1] == 999.0).all()
    assert fallback_slots == 16


def test_price_damping_pulls_estimates_to_mean():
    from ems.forecast import dampen_estimated
    idx = pd.date_range(START, periods=8, freq="1h")
    price = pd.Series([20, 30, 20, 30, 10, 40, 10, 40], index=idx, dtype=float)
    est = pd.Series([False] * 4 + [True] * 4, index=idx)
    out = dampen_estimated(price, est, damping=0.5)
    assert (out.iloc[:4] == price.iloc[:4]).all()       # echte Preise unberührt
    # geschätzte Slots (Mittel 25): 10 -> 17.5, 40 -> 32.5
    assert abs(out.iloc[4] - 17.5) < 1e-9
    assert abs(out.iloc[5] - 32.5) < 1e-9
    assert abs(out[est].mean() - 25.0) < 1e-9           # Mittel bleibt erhalten
    # damping=0 -> unverändert
    assert (dampen_estimated(price, est, 0.0) == price).all()


def test_intraday_factor_decays_with_lead_time():
    from ems.forecast import intraday_factor_series
    idx = pd.date_range(START, periods=96, freq=FREQ)
    fac = intraday_factor_series(1.4, idx, START, decay_hours=6.0)
    assert abs(fac.iloc[0] - 1.4) < 1e-9                 # jetzt: volle Korrektur
    at6h = fac.iloc[24]                                  # 6 h später: halber Weg
    assert abs(at6h - 1.2) < 1e-9
    assert fac.iloc[-1] < 1.1                            # weit voraus: fast 1.0
    # ratio None -> neutral
    assert (intraday_factor_series(None, idx, START) == 1.0).all()


def test_hourly_correction_profile_replaces_global_factor():
    """Stunden-Korrekturprofil (Kalibrierung): wird je Slot-Stunde angewandt
    und ERSETZT den globalen correction_factor (nie beides - das wäre eine
    Doppel-Korrektur). Ohne Profil gilt weiter der globale Faktor."""
    import numpy as np
    import pandas as pd
    from ems.forecast import LoadForecaster
    from tests.test_synthetic import make_config, synthetic_history

    cfg = make_config()
    cfg.forecast.correction_factor = 1.5      # würde global x1.5 heben
    now = pd.Timestamp("2026-06-10 00:00", tz=cfg.general.timezone)
    hist = synthetic_history(cfg, now)

    fc = LoadForecaster(cfg)
    fc.load_hourly = None                     # kein Profil -> globaler Faktor
    base = fc.forecast(hist, now, 96)

    fc2 = LoadForecaster(cfg)
    fc2.load_hourly = {h: 1.0 for h in range(24)}
    fc2.load_hourly[6] = 2.0                  # 06:00 doppelt, Rest neutral
    prof = fc2.forecast(hist, now, 96)

    loc = prof.index.tz_convert(cfg.general.timezone)
    h6 = loc.hour == 6
    # Profil ersetzt global: neutrale Stunden = base/1.5, 06:00 = base/1.5*2
    np.testing.assert_allclose(prof[~h6].values, (base[~h6] / 1.5).values,
                               rtol=1e-9)
    np.testing.assert_allclose(prof[h6].values, (base[h6] / 1.5 * 2.0).values,
                               rtol=1e-9)
