"""Synthetik-Test ohne InfluxDB/MQTT.

Prüft:
  * Verbrauchsprognose läuft auf synthetischer Jahreshistorie.
  * MILP-Optimierung ist lösbar und erfüllt die Kern-Nebenbedingungen
    (SoC-Grenzen, kein gleichzeitiges Laden/Entladen, Auto-Ziel-SoC).
  * Dashboard-HTML wird erzeugt.

Aufruf:  python -m tests.test_synthetic
"""
from __future__ import annotations

import sys
from datetime import time, timedelta

import numpy as np
import pandas as pd

from ems.config import (
    Config, GeneralConfig, InfluxConfig, FeedInConfig, HouseBatteryConfig,
    InverterConfig, VehicleConfig, OptimizationConfig, ForecastConfig,
    MqttConfig, DashboardConfig, CalibrationConfig,
)
from ems.forecast import LoadForecaster
from ems.optimizer import Optimizer, OptimizerInputs
from ems.dashboard import build_dashboard


def make_config(tmp_html="/tmp/ems_test_dashboard.html") -> Config:
    return Config(
        general=GeneralConfig(),
        influxdb=InfluxConfig(version=2, v1={}, v2={}, signals={}, outputs={}),
        feed_in=FeedInConfig(mode="fixed", fixed_ct_kwh=8.0),
        house_battery=HouseBatteryConfig(
            capacity_wh=10000, max_dc_charge_w=5000, max_ac_charge_w=3000,
            max_discharge_w=5000, min_soc_percent=10, max_soc_percent=100,
            charge_efficiency=0.96, discharge_efficiency=0.96,
        ),
        inverter=InverterConfig(max_ac_power_w=8000),
        vehicle=VehicleConfig(
            enabled=True, capacity_wh=60000, max_charge_w=11000, min_charge_w=1400,
            min_soc_percent=20, target_soc_percent=80, departure_time=time(7, 0),
            charge_efficiency=0.92,
        ),
        optimization=OptimizationConfig(
            terminal_soc_value="auto", cycle_penalty_ct_kwh=0.1, solver_time_limit_s=60
        ),
        forecast=ForecastConfig(lookback_days=400, holiday_country="DE",
                                holiday_subdivision="BY"),
        mqtt=MqttConfig(enabled=False),
        dashboard=DashboardConfig(enabled=True, output_path=tmp_html),
        calibration=CalibrationConfig(enabled=False),
    )


def synthetic_history(cfg: Config, now: pd.Timestamp) -> pd.Series:
    freq = f"{cfg.general.slot_minutes}min"
    idx = pd.date_range(now - timedelta(days=cfg.forecast.lookback_days), now,
                        freq=freq, tz=cfg.general.timezone, inclusive="left")
    hour = idx.hour + idx.minute / 60.0
    # Grundlast + Morgen-/Abendspitze + Wochenend-Zuschlag + Rauschen
    base = 300 + 250 * np.exp(-((hour - 7.5) ** 2) / 2) + 400 * np.exp(-((hour - 19) ** 2) / 3)
    weekend = np.where(idx.weekday >= 5, 150, 0)
    noise = np.random.default_rng(42).normal(0, 30, len(idx))
    return pd.Series(np.clip(base + weekend + noise, 50, None), index=idx)


def main() -> int:
    cfg = make_config()
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor(f"{cfg.general.slot_minutes}min")

    # --- Prognose ---
    hist = synthetic_history(cfg, now)
    fc = LoadForecaster(cfg).forecast(hist, now, cfg.general.n_forecast_slots)
    assert len(fc) == cfg.general.n_forecast_slots, "Falsche Prognoselänge"
    assert (fc >= 0).all(), "Negative Prognosewerte"
    print(f"[OK] Prognose: {len(fc)} Slots, Mittel {fc.mean():.0f} W, "
          f"min {fc.min():.0f} / max {fc.max():.0f} W")

    # --- Optimierung ---
    N = cfg.general.n_opt_slots
    opt_index = pd.date_range(now, periods=N, freq=f"{cfg.general.slot_minutes}min",
                              tz=cfg.general.timezone)
    hour = np.asarray(opt_index.hour + opt_index.minute / 60.0, dtype=float)
    pv = np.clip(6000 * np.exp(-((hour - 13) ** 2) / 6), 0, None)
    # Dynamischer Preis: nachts günstig, abends teuer
    price = 25 + 10 * np.sin((hour - 6) / 24 * 2 * np.pi) + np.where((hour >= 17) & (hour <= 20), 15, 0)
    house = fc.reindex(opt_index).ffill().bfill().values
    feedin = np.full(N, 8.0)

    inp = OptimizerInputs(
        index=opt_index, house_load_w=house, pv_w=pv, price_ct_kwh=price,
        feedin_ct_kwh=feedin, initial_house_soc_wh=3000,
        initial_car_soc_wh=0.4 * 60000, car_present=True,
    )
    res = Optimizer(cfg).solve(inp)
    print(f"[OK] Optimierung: Status={res.status}, Kosten={res.total_cost_ct/100:.2f} EUR")
    assert not res.infeasible, "Optimierung nicht optimal"

    t = res.table
    hb = cfg.house_battery
    # SoC-Grenzen
    assert (t["house_soc_percent"] >= hb.min_soc_percent - 0.5).all(), "SoC unter Minimum"
    assert (t["house_soc_percent"] <= hb.max_soc_percent + 0.5).all(), "SoC über Maximum"
    # kein gleichzeitiges Laden/Entladen
    charging = (t["batt_dc_charge_w"] + t["batt_ac_charge_w"]) > 1
    discharging = t["batt_discharge_w"] > 1
    assert not (charging & discharging).any(), "Gleichzeitiges Laden und Entladen!"
    # Auto-Ziel-SoC erreicht (mind. einmal >= 80% oder am Ende)
    assert t["car_soc_percent"].max() >= cfg.vehicle.target_soc_percent - 0.5, \
        "Auto-Ziel-SoC nicht erreicht"
    # Leistungsgrenzen
    assert (t["batt_dc_charge_w"] <= hb.max_dc_charge_w + 1).all()
    assert (t["batt_ac_charge_w"] <= hb.max_ac_charge_w + 1).all()
    assert (t["batt_discharge_w"] <= hb.max_discharge_w + 1).all()
    print("[OK] Nebenbedingungen erfüllt (SoC, kein gleichz. Laden/Entladen, "
          "Auto-Ziel, Leistungsgrenzen)")

    # Sinnvolle Ökonomie: AC-Laden bevorzugt bei niedrigem Preis
    ac_price = t.loc[t["batt_ac_charge_w"] > 1, "price_ct_kwh"]
    if len(ac_price):
        print(f"[INFO] Ø Preis bei AC-Laden {float(ac_price.mean()):.1f} ct/kWh "
              f"(Gesamt-Ø {float(np.mean(np.asarray(price))):.1f})")

    # --- Fallback bei ungültigen Eingaben (NaN) -> neutraler "auto"-Fahrplan ---
    bad = OptimizerInputs(
        index=opt_index, house_load_w=house, pv_w=np.full(N, np.nan),
        price_ct_kwh=price, feedin_ct_kwh=feedin, initial_house_soc_wh=3000,
    )
    res_bad = Optimizer(cfg).solve(bad)
    assert res_bad.infeasible, "NaN-Eingaben müssen als Fallback markiert sein"
    tb = res_bad.table
    assert (tb["mode"] == "auto").all(), "Fallback muss ohne Eingriffe sein"
    assert (tb["batt_grid_charge_w"] == 0).all()
    assert (tb["batt_charge_limit_w"] == hb.max_dc_charge_w).all()
    assert (tb["house_soc_percent"] >= hb.min_soc_percent - 0.5).all()
    assert (tb["house_soc_percent"] <= hb.max_soc_percent + 0.5).all()
    assert tb.notna().drop(columns=["export_line_w"]).all().all(), \
        "Fallback-Tabelle enthält NaN"
    print("[OK] Fallback bei ungültigen Eingaben: neutraler 'auto'-Fahrplan")

    # --- Dashboard ---
    html = build_dashboard(cfg, t, res.total_cost_ct)
    import os
    assert os.path.exists(html) and os.path.getsize(html) > 10000, "Dashboard leer"
    print(f"[OK] Dashboard erzeugt: {html} ({os.path.getsize(html)//1024} KB)")

    print("\nALLE TESTS BESTANDEN.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
