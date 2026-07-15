"""Thermomodell-Kalibrierung (ems/pool_calibration.py).

Prüft:
  * Der Fit findet auf synthetischen Messreihen (bekannter Verlust- und
    Solar-Koeffizient, Sensor-Quantisierung 0,1 K) die wahren Parameter wieder.
  * Slots mit Heiz-Freigabe (permit=1) werden ignoriert - sonst würde der
    unbekannte WP-Wärmeeintrag den Fit verfälschen.
  * load_cmd-Roundtrip in der lokalen SQLite (Lücken bleiben Lücken).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.local_history import read_load_cmd, write_load_cmd
from ems.pool_calibration import fit_thermal_params

TZ = "Europe/Berlin"
CAP = 8000 * 1.163            # 8000 l Pool -> Wh/K
LOSS = 380.0                  # W/K (wahr)
A_SOLAR = 6.0                 # W je W/m² (wahr; z.B. 8 m² * 0.75)


def _synthetic(days=7, permit_pattern="off", seed=1):
    """Pooltemperatur aus dem wahren Modell simulieren (0,1-K-Sensor)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-06-01", periods=days * 96, freq="15min", tz=TZ)
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    t_amb = pd.Series(16 + 8 * np.exp(-((hour - 15) ** 2) / 18), index=idx)
    g = pd.Series(np.clip(800 * np.exp(-((hour - 13) ** 2) / 8), 0, None), index=idx)
    heat_on = np.zeros(len(idx))
    if permit_pattern == "morning_heat":
        heat_on = ((hour >= 9) & (hour < 12)).astype(float)   # WP heizt 9-12
    T = np.empty(len(idx) + 1)
    T[0] = 27.0
    for i in range(len(idx)):
        q = A_SOLAR * g.iloc[i] - LOSS * (T[i] - t_amb.iloc[i]) \
            + heat_on[i] * 7000.0                               # WP-Wärme
        T[i + 1] = T[i] + q * 0.25 / CAP
    # Sensor: 0,1-K-Quantisierung + minimales Rauschen
    t_pool = pd.Series(np.round(T[:-1] + rng.normal(0, 0.02, len(idx)), 1), index=idx)
    permit = pd.Series(heat_on, index=idx)
    return t_pool, t_amb, g, permit


def test_fit_recovers_true_parameters():
    t_pool, t_amb, g, permit = _synthetic(days=7)
    fit = fit_thermal_params(t_pool, t_amb, g, permit, CAP)
    assert fit is not None
    assert abs(fit.loss_w_per_k - LOSS) / LOSS < 0.15, fit.loss_w_per_k
    assert abs(fit.a_solar_w_per_wm2 - A_SOLAR) / A_SOLAR < 0.15, fit.a_solar_w_per_wm2
    assert fit.r2 > 0.7
    assert abs(fit.solar_absorption(8.0) - A_SOLAR / 8.0) < 0.15


def test_fit_ignores_permitted_slots():
    """Mit Heizfenster 9-12 Uhr (permit=1) muss der Fit trotzdem stimmen -
    die beheizten Fenster fliegen raus, statt den Solareintrag aufzublähen."""
    t_pool, t_amb, g, permit = _synthetic(days=10, permit_pattern="morning_heat")
    fit = fit_thermal_params(t_pool, t_amb, g, permit, CAP)
    assert fit is not None
    assert abs(fit.loss_w_per_k - LOSS) / LOSS < 0.15, fit.loss_w_per_k
    assert abs(fit.a_solar_w_per_wm2 - A_SOLAR) / A_SOLAR < 0.20, fit.a_solar_w_per_wm2


def test_fit_needs_enough_windows():
    t_pool, t_amb, g, permit = _synthetic(days=1)
    assert fit_thermal_params(t_pool, t_amb, g, permit, CAP,
                              min_windows=1000) is None


def test_load_cmd_roundtrip(tmp_path):
    db = str(tmp_path / "cmd.sqlite")
    base = pd.Timestamp("2026-06-01 10:00", tz=TZ)
    write_load_cmd(db, base, "Pool", 1)
    write_load_cmd(db, base + pd.Timedelta(minutes=15), "Pool", 0)
    # 10:30 fehlt absichtlich (Lücke = unbekannt)
    write_load_cmd(db, base + pd.Timedelta(minutes=45), "Pool", 0)
    s = read_load_cmd(db, "Pool", base, base + pd.Timedelta(hours=1), TZ)
    assert list(s.values) == [1.0, 0.0, 0.0]
    assert len(s) == 3                      # Lücke wird NICHT aufgefüllt
    assert read_load_cmd(db, "Sauna", base, base + pd.Timedelta(hours=1), TZ).empty
