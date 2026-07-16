"""Tests des Tarifmodells (Spot -> Bezugspreis) inkl. §14a-Fensterlogik."""
from __future__ import annotations

import pandas as pd

from ems import energycharts, tariff
from ems.config import GridFeeWindow, parse_grid_fee_windows
from ems.local_history import read_spot, write_spot


def _cfg(**tkw):
    from tests.test_synthetic import make_config
    c = make_config()
    c.tariff.enabled = True
    for k, v in tkw.items():
        setattr(c.tariff, k, v)
    return c


def test_energycharts_to_map():
    # EUR/MWh -> ct/kWh (/10); None übersprungen
    payload = {"unix_seconds": [1720476000, 1720476900],
               "price": [119.71, None]}
    m = energycharts._to_map(payload)
    assert len(m) == 1
    (k, v), = m.items()
    assert abs(v - 11.971) < 1e-9
    assert k.endswith("+00:00")


def test_apply_tariff_dynamic_formula():
    """netto = spot*(1+auf%) + marge + abgaben + netz; brutto = netto*(1+MwSt)."""
    cfg = _cfg(type="dynamic", markup_percent=10.0, markup_ct_kwh=1.0,
               levies_ct_kwh=5.0, vat_percent=19.0,
               grid_fee_mode="static", grid_fee_ct_kwh=8.0)
    idx = pd.date_range("2026-01-01 10:00", periods=2, freq="15min", tz="Europe/Berlin")
    spot = pd.Series([10.0, 20.0], index=idx)
    out = tariff.apply_tariff(spot, cfg)
    # 10*1.1 + 1 + 5 + 8 = 25 -> *1.19 = 29.75
    assert abs(out.iloc[0] - 29.75) < 1e-6
    # 20*1.1 + 1 + 5 + 8 = 36 -> *1.19 = 42.84
    assert abs(out.iloc[1] - 42.84) < 1e-6


def test_apply_tariff_preserves_nan():
    cfg = _cfg(type="dynamic", grid_fee_mode="included")
    idx = pd.date_range("2026-01-01 10:00", periods=2, freq="15min", tz="Europe/Berlin")
    out = tariff.apply_tariff(pd.Series([10.0, float("nan")], index=idx), cfg)
    assert not pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])


def test_price_series_complete_outage_uses_safe_fallback(tmp_path):
    from ems.main import _price_series
    from tests.test_synthetic import make_config
    cfg = make_config()
    cfg.tariff.enabled = True
    cfg.tariff.type = "dynamic"
    cfg.tariff.fixed_ct_kwh = 31.5
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "empty.sqlite")
    now = pd.Timestamp("2026-07-01 12:00", tz=cfg.general.timezone)
    idx = pd.date_range(now, periods=16, freq="15min")
    out = _price_series(None, cfg, idx, now)
    assert out.notna().all() and (out == 31.5).all()


def test_fixed_tariff_constant():
    cfg = _cfg(type="fixed", fixed_ct_kwh=34.0)
    idx = pd.date_range("2026-01-01 10:00", periods=3, freq="15min", tz="Europe/Berlin")
    out = tariff.apply_tariff(pd.Series([1.0, 2.0, 3.0], index=idx), cfg)
    assert (out == 34.0).all()


def test_14a_windows_hours_and_wrap():
    windows = parse_grid_fee_windows([
        {"ct_kwh": 12.0, "hours": ["16-20"]},          # HT nachmittags
        {"ct_kwh": 3.0, "hours": ["0-6"], "date_from": "11-01", "date_to": "02-28"},  # NT Winter-Nacht
    ])
    cfg = _cfg(type="dynamic", vat_percent=0.0, grid_fee_mode="14a",
               grid_fee_default_ct_kwh=8.0, grid_fee_windows=windows)
    # 17:00 -> HT 12; 03:00 im Januar -> Winter-NT 3; 12:00 -> Default 8
    idx = pd.to_datetime(["2026-01-15 17:00", "2026-01-15 03:00", "2026-01-15 12:00"]
                         ).tz_localize("Europe/Berlin")
    fee = tariff.grid_fee_series(idx, cfg.tariff)
    assert list(fee) == [12.0, 3.0, 8.0]
    # 03:00 im Juli fällt NICHT in den Winter-Datumsbereich -> Default
    jul = pd.to_datetime(["2026-07-15 03:00"]).tz_localize("Europe/Berlin")
    assert tariff.grid_fee_series(jul, cfg.tariff).iloc[0] == 8.0


def test_hours_range_parse():
    from ems.config import _parse_hours
    assert _parse_hours(["16-20"]) == [16, 17, 18, 19]
    assert _parse_hours([22, "6-9"]) == [6, 7, 8, 22]
    assert _parse_hours("22-2") == [0, 1, 22, 23]      # Wrap über Mitternacht


def test_read_spot_roundtrip_and_hold(tmp_path):
    """Stündliche Spot-Werte werden beim Auslesen auf 15-min gehalten; nach dem
    letzten Punkt bleibt NaN (löst Schätzung aus)."""
    db = str(tmp_path / "s.sqlite")
    base = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    mapping = {(base + pd.Timedelta(hours=i)).isoformat(): 10.0 + i for i in range(3)}
    assert write_spot(db, mapping) == 3
    tz = "Europe/Berlin"
    # 4 Stunden Grid ab base; letzter Spot bei +2h -> +2h15..+2h45 gehalten, danach NaN
    s = read_spot(db, base.tz_convert(tz), (base + pd.Timedelta(hours=4)).tz_convert(tz),
                  tz, 15)
    assert len(s) == 16
    assert abs(s.iloc[0] - 10.0) < 1e-9          # 00:00
    assert abs(s.iloc[4] - 11.0) < 1e-9          # +1h gehalten
    assert abs(s.iloc[8] - 12.0) < 1e-9          # +2h
    assert abs(s.iloc[11] - 12.0) < 1e-9         # +2h45 noch gehalten
    assert pd.isna(s.iloc[12])                   # danach NaN


def test_read_spot_native_15min_does_not_extend_past_last_value(tmp_path):
    """Regression: Energy-Charts liefert die jüngste Historie bereits nativ
    15-min-genau. Das alte ffill(limit=spl-1) hielt den letzten ECHTEN 15-min-
    Wert fälschlich 3 weitere Slots (bis zu 45 min) über das reale Datenende
    hinaus - z.B. 23:45 heute erschien dann bis 00:30 morgen "gültig". Bei
    lückenlosen 15-min-Quelldaten darf NACH dem letzten Wert sofort NaN stehen."""
    db = str(tmp_path / "s15.sqlite")
    base = pd.Timestamp("2026-01-01 22:00", tz="UTC")   # = 23:00 CET
    mapping = {(base + pd.Timedelta(minutes=15 * i)).isoformat(): 20.0 + i
               for i in range(4)}                        # 23:00, 23:15, 23:30, 23:45
    assert write_spot(db, mapping) == 4
    tz = "Europe/Berlin"
    s = read_spot(db, base.tz_convert(tz), (base + pd.Timedelta(hours=2)).tz_convert(tz),
                  tz, 15)
    last_real = s.dropna().index[-1]
    assert last_real.strftime("%H:%M") == "23:45", \
        f"letzter echter Wert sollte 23:45 sein, ist {last_real}"
    assert pd.isna(s[s.index > last_real]).all(), \
        "Slots nach dem letzten echten 15-min-Wert dürfen nicht gehalten werden"
