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


def test_maybe_apply_writes_damped_values_to_overlay(tmp_path):
    """--apply: gute Fit-Qualität -> gedämpfte Werte (Mittel aus alt und Fit)
    landen im Overlay und kommen über load_config wieder an der Last an."""
    import shutil, yaml
    from ems.pool_calibration import FitResult, maybe_apply
    from ems.config import load_config
    cfg_path = str(tmp_path / "config.yaml")
    shutil.copy("config.example.yaml", cfg_path)
    with open(cfg_path, "a", encoding="utf-8") as fh:
        fh.write("\ne3dc_rscp:\n  history_db_path: '%s'\n" % (tmp_path / "h.sqlite"))
        fh.write("controllable_loads:\n"
                 "  - name: Pool\n    type: thermal\n    volume_l: 8000\n"
                 "    loss_w_per_k: 300.0\n    surface_m2: 8.0\n"
                 "    solar_absorption: 0.6\n"
                 "    stages: [{name: WP, power_w: 400, heat_w: 3000}]\n")
    cfg = load_config(cfg_path)
    ld = cfg.controllable_loads[0]
    fit = FitResult(loss_w_per_k=500.0, a_solar_w_per_wm2=8.0,  # absorption 1.0
                    n_windows=200, r2=0.8)
    changed = maybe_apply(fit, ld, cfg_path)
    assert changed == {"loss_w_per_k": 400.0,          # 0.5*300 + 0.5*500
                       "solar_absorption": 0.8}        # 0.5*0.6 + 0.5*1.0
    ov = yaml.safe_load(open(tmp_path / "config_overrides.yaml"))
    assert ov["controllable_loads_overrides"]["Pool"]["loss_w_per_k"] == 400.0
    # und die Whitelist lässt loss_w_per_k durch (Rundreise über load_config)
    ld2 = load_config(cfg_path).controllable_loads[0]
    assert ld2.loss_w_per_k == 400.0
    assert ld2.solar_absorption == 0.8


def test_maybe_apply_respects_quality_gates(tmp_path):
    """Zu wenig Fenster / schlechtes R² / unplausible Werte -> KEINE Übernahme."""
    import shutil
    from ems.pool_calibration import FitResult, maybe_apply
    from ems.config import load_config
    cfg_path = str(tmp_path / "config.yaml")
    shutil.copy("config.example.yaml", cfg_path)
    with open(cfg_path, "a", encoding="utf-8") as fh:
        fh.write("\ncontrollable_loads:\n"
                 "  - name: Pool\n    type: thermal\n    volume_l: 8000\n"
                 "    loss_w_per_k: 300.0\n    surface_m2: 8.0\n"
                 "    stages: [{name: WP, power_w: 400, heat_w: 3000}]\n")
    ld = load_config(cfg_path).controllable_loads[0]
    ok = dict(loss_w_per_k=500.0, a_solar_w_per_wm2=6.0)
    assert maybe_apply(FitResult(**ok, n_windows=50, r2=0.9), ld, cfg_path) is None
    assert maybe_apply(FitResult(**ok, n_windows=200, r2=0.2), ld, cfg_path) is None
    # unplausibler Verlust (5000 W/K) -> loss nicht übernommen, absorption schon
    ch = maybe_apply(FitResult(loss_w_per_k=5000.0, a_solar_w_per_wm2=6.0,
                               n_windows=200, r2=0.9), ld, cfg_path)
    assert ch is not None and "loss_w_per_k" not in ch
    assert not (tmp_path / "config_overrides.yaml").exists() or \
        "loss_w_per_k" not in str(open(tmp_path / "config_overrides.yaml").read())


def test_in_season_guards_calibration_logging():
    """Außerhalb der Saison (Winter: Sensor nicht im Pool!) darf die Freigabe
    nicht geloggt werden - in_season ist der Wächter dafür (inkl. Wrap über
    den Jahreswechsel und "keine Saison = immer")."""
    from ems.config import ControllableLoad, LoadStage
    from ems.loads import in_season
    pool = ControllableLoad(name="Pool", type="thermal",
                            season_from="04-15", season_to="10-15",
                            stages=[LoadStage("WP", 400, 3000)])
    assert in_season(pool, pd.Timestamp("2026-07-15", tz=TZ))
    assert in_season(pool, pd.Timestamp("2026-04-15", tz=TZ))
    assert in_season(pool, pd.Timestamp("2026-10-15", tz=TZ))
    assert not in_season(pool, pd.Timestamp("2026-01-20", tz=TZ))   # Winter
    assert not in_season(pool, pd.Timestamp("2026-11-01", tz=TZ))
    # Wrap über den Jahreswechsel (z.B. Heizsaison Okt-März)
    heat = ControllableLoad(name="Puffer", type="thermal",
                            season_from="10-01", season_to="03-31",
                            stages=[LoadStage("WP", 400, 3000)])
    assert in_season(heat, pd.Timestamp("2026-01-20", tz=TZ))
    assert not in_season(heat, pd.Timestamp("2026-07-15", tz=TZ))
    # keine Saison konfiguriert -> immer aktiv
    always = ControllableLoad(name="X", type="thermal",
                              stages=[LoadStage("WP", 400, 3000)])
    assert in_season(always, pd.Timestamp("2026-01-20", tz=TZ))


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
