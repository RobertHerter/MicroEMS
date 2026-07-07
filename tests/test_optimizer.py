"""Gezielte Optimierer-Tests (pytest).

Deckt die Randfälle ab, die der Synthetik-E2E-Test nicht prüft:
Ladestrategien (peak/asap), negative Preise, Netz-Entlade-Arbitrage,
Infeasibility-Fallback und DST-Umstellungstage (92/100 Slots pro Tag).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.optimizer import Optimizer, OptimizerInputs
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
FREQ = "15min"
TOL = 1.0  # W


def _day_index(day: str, days: int = 1) -> pd.DatetimeIndex:
    # Tagesgrenzen über Wanduhrzeit (wie main.py mit normalize()), damit
    # DST-Tage korrekt 92/100 Slots haben.
    start = pd.Timestamp(f"{day} 00:00", tz=TZ)
    end = (pd.Timestamp(f"{day} 00:00") + pd.Timedelta(days=days)).tz_localize(TZ)
    return pd.date_range(start, end, freq=FREQ, inclusive="left")


def _inputs(idx, pv=0.0, load=500.0, price=30.0, feedin=8.0,
            soc=5000.0, **kw) -> OptimizerInputs:
    n = len(idx)

    def arr(x):
        return np.full(n, float(x)) if np.isscalar(x) else np.asarray(x, dtype=float)

    return OptimizerInputs(
        index=idx, house_load_w=arr(load), pv_w=arr(pv),
        price_ct_kwh=arr(price), feedin_ct_kwh=arr(feedin),
        initial_house_soc_wh=float(soc), **kw,
    )


def _pv_gauss(idx, peak_w: float) -> np.ndarray:
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    return np.clip(peak_w * np.exp(-((hour - 13.0) ** 2) / 6.0), 0.0, None)


def test_peak_strategy_shaves_and_fills_battery():
    """Peak-Tag: kein Netzladen, Einspeise-Linie gesetzt, Akku wird voll."""
    cfg = make_config()
    cfg.optimization.charge_strategy = "peak"
    idx = _day_index("2026-06-10")
    res = Optimizer(cfg).solve(_inputs(idx, pv=_pv_gauss(idx, 8000), soc=1500))
    assert not res.infeasible
    t = res.table
    assert (t["batt_ac_charge_w"] <= TOL).all(), "Peak-Tag darf nicht netzladen"
    assert t["export_line_w"].notna().any(), "Einspeise-Linie fehlt"
    assert t["house_soc_percent"].max() >= 99.0, "Akku sollte voll werden"


def test_asap_strategy_exports_only_when_full_or_at_max():
    """asap: Einspeisung nur bei vollem Akku oder maximaler Ladeleistung."""
    cfg = make_config()
    cfg.optimization.charge_strategy = "asap"
    idx = _day_index("2026-06-10")
    res = Optimizer(cfg).solve(_inputs(idx, pv=_pv_gauss(idx, 8000), soc=1500))
    assert not res.infeasible
    t = res.table
    hb = cfg.house_battery
    exporting = t["grid_export_w"] > TOL
    full = t["house_soc_wh"] >= hb.max_soc_wh - 50.0
    at_max = (t["batt_dc_charge_w"] + t["batt_ac_charge_w"]) >= hb.max_total_charge_w - 50.0
    assert (full | at_max)[exporting].all(), \
        "Einspeisung obwohl Akku weder voll noch mit Max-Leistung lädt"
    assert t["house_soc_percent"].max() >= 99.0


def test_negative_prices_charge_from_grid():
    """Negative Preise: Netzladen findet in den negativen Slots statt;
    nie gleichzeitiger Import und Export."""
    cfg = make_config()
    cfg.optimization.charge_strategy = "asap"
    idx = _day_index("2026-01-20")
    price = np.where(idx.hour < 4, -5.0, 30.0).astype(float)
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price, soc=1500))
    assert not res.infeasible
    t = res.table
    charged = t.loc[t["batt_ac_charge_w"] > TOL]
    assert len(charged) > 0, "Bei negativen Preisen sollte netzgeladen werden"
    assert (charged["price_ct_kwh"] < 10.0).all(), \
        "Netzladen sollte nur in den billigen/negativen Slots stattfinden"
    both = (t["grid_import_w"] > TOL) & (t["grid_export_w"] > TOL)
    assert not both.any(), "Gleichzeitiger Import und Export"


def test_grid_discharge_arbitrage():
    """Akku->Netz nur, wenn künftiger Importpreis unter der Einspeisung liegt:
    jetzt zu 8 ct einspeisen, später zu 2 ct zurückladen."""
    cfg = make_config()
    cfg.optimization.allow_grid_discharge = True
    cfg.optimization.charge_strategy = "asap"
    idx = _day_index("2026-01-20")
    price = np.where(idx.hour < 8, 40.0, 2.0).astype(float)
    res = Optimizer(cfg).solve(
        _inputs(idx, pv=0.0, load=200.0, price=price, soc=10000))
    assert not res.infeasible
    t = res.table
    gd = t.loc[t["batt_grid_discharge_w"] > TOL]
    assert len(gd) > 0, "Arbitrage: Netz-Entladen sollte stattfinden"
    assert (gd["mode"] == "grid_discharge").all()
    recharge = t.loc[(t["batt_ac_charge_w"] > TOL), "price_ct_kwh"]
    assert len(recharge) > 0 and (recharge < 10.0).all(), \
        "Zurückladen sollte in der Billigphase stattfinden"

    # Kontrolle: ohne Preisgefälle unter die Einspeisung (flacher Preis über
    # feedin) bleibt Akku->Netz gesperrt - kein unwirtschaftliches Dumpen.
    res_flat = Optimizer(cfg).solve(
        _inputs(idx, pv=0.0, load=200.0, price=30.0, soc=10000))
    assert (res_flat.table["batt_grid_discharge_w"] <= TOL).all(), \
        "Netz-Entladen trotz flachem Preis"


def test_infeasible_car_target_returns_neutral_fallback():
    """Unerreichbarer Auto-Ziel-SoC -> kein Absturz, neutraler 'auto'-Plan."""
    cfg = make_config()
    from datetime import time
    cfg.vehicle.departure_time = time(2, 0)   # in 2 h; 36 kWh fehlen, max 22 kWh
    idx = _day_index("2026-01-20")[:16]       # 4 h Horizont
    res = Optimizer(cfg).solve(_inputs(
        idx, initial_car_soc_wh=0.2 * cfg.vehicle.capacity_wh, car_present=True))
    assert res.infeasible
    t = res.table
    assert (t["mode"] == "auto").all()
    assert (t["batt_grid_charge_w"] == 0).all()
    assert t["house_soc_percent"].between(
        cfg.house_battery.min_soc_percent - 0.5,
        cfg.house_battery.max_soc_percent + 0.5).all()


def test_car_switch_penalty_limits_toggling():
    """Hoher Schalt-Malus: Auto lädt in EINEM zusammenhängenden Block, auch
    wenn die Preise zappeln."""
    cfg = make_config()
    cfg.optimization.car_switch_penalty_ct = 1000.0
    idx = _day_index("2026-01-20")
    price = np.where(idx.hour % 2 == 0, 10.0, 40.0).astype(float)  # Zickzack
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=price,
        initial_car_soc_wh=0.4 * cfg.vehicle.capacity_wh, car_present=True))
    assert not res.infeasible
    on = (res.table["car_charge_w"].values > TOL).astype(int)
    starts = int(np.sum(np.diff(on) == 1) + on[0])
    assert on.sum() > 0, "Auto muss laden (Ziel-SoC)"
    assert starts == 1, f"Erwartet 1 Ladeblock, {starts} Starts gefunden"


def test_car_taper_limits_power_at_high_soc():
    """Ladekurve: oberhalb taper_start sinkt die zulässige Leistung linear."""
    cfg = make_config()
    from datetime import time
    cfg.vehicle.target_soc_percent = 100.0
    cfg.vehicle.taper_start_soc_percent = 50.0
    cfg.optimization.car_switch_penalty_ct = 0.0
    # Mit Taper dauert 40 -> 100 % ca. 7.6 h -> Abfahrt 20:00 statt 07:00.
    cfg.vehicle.departure_time = time(20, 0)
    idx = _day_index("2026-01-20")
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=20.0,
        initial_car_soc_wh=0.4 * cfg.vehicle.capacity_wh, car_present=True))
    assert not res.infeasible
    t = res.table
    veh = cfg.vehicle
    assert t["car_soc_percent"].max() >= 99.5, "Ziel 100 % nicht erreicht"
    soc_start = np.r_[40.0, t["car_soc_percent"].values[:-1]]  # SoC am Slot-Anfang
    limit = veh.min_charge_w + (veh.max_charge_w - veh.min_charge_w) * \
        (100.0 - soc_start) / (100.0 - veh.taper_start_soc_percent)
    over = t["car_charge_w"].values > np.minimum(limit, veh.max_charge_w) + 5.0
    assert not over.any(), "Ladeleistung überschreitet die Taper-Kurve"


def test_dst_spring_forward_day():
    """DST-Frühjahrstag (23 h, 92 Slots): Tages-Logik läuft ohne Fehler."""
    cfg = make_config()
    idx = _day_index("2026-03-29")            # Zeitumstellung 02:00 -> 03:00
    assert len(idx) == 92
    res = Optimizer(cfg).solve(_inputs(idx, pv=_pv_gauss(idx, 6000), soc=3000))
    assert not res.infeasible
    assert len(res.table) == 92
    hb = cfg.house_battery
    assert res.table["house_soc_percent"].between(
        hb.min_soc_percent - 0.5, hb.max_soc_percent + 0.5).all()


def test_dst_autumn_day():
    """DST-Herbsttag (25 h, 100 Slots): Tages-Logik läuft ohne Fehler."""
    cfg = make_config()
    idx = _day_index("2026-10-25")            # Zeitumstellung 03:00 -> 02:00
    assert len(idx) == 100
    res = Optimizer(cfg).solve(_inputs(idx, pv=_pv_gauss(idx, 3000), soc=3000))
    assert not res.infeasible
    assert len(res.table) == 100
