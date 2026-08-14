"""Overlay-Persistenz fürs Dashboard: save_override schreibt config_overrides.yaml,
load_config merged sie über die Basis; steuerbare Lasten werden per Slug überlagert."""
from __future__ import annotations

import os

import pytest
import yaml

from ems.config import (_deep_merge, _overrides_path, save_override,
                        parse_controllable_loads)

_REAL_CONFIG = "/opt/ems/config.yaml"


def _require_real_config() -> str:
    """Pfad zur echten Deployment-Config; überspringt den Test, wenn sie fehlt
    (z. B. in CI – config.yaml ist gitignored und enthält Secrets)."""
    if not os.path.exists(_REAL_CONFIG):
        pytest.skip("Deployment-Config /opt/ems/config.yaml nicht vorhanden (CI)")
    return _REAL_CONFIG


def test_deep_merge_nested():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    _deep_merge(base, {"a": {"y": 9, "z": 4}, "c": 5})
    assert base == {"a": {"x": 1, "y": 9, "z": 4}, "b": 3, "c": 5}


def test_save_override_creates_and_merges(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("general: {}\n", encoding="utf-8")
    save_override(str(cfg), "optimization.charge_strategy", "peak")
    save_override(str(cfg), "controllable_loads_overrides.Pool.enabled", False)
    save_override(str(cfg), "controllable_loads_overrides.Pool.target_c", 26.5)
    data = yaml.safe_load(open(_overrides_path(str(cfg)), encoding="utf-8"))
    assert data["optimization"]["charge_strategy"] == "peak"
    assert data["controllable_loads_overrides"]["Pool"] == {
        "enabled": False, "target_c": 26.5}


def test_parse_controllable_loads_applies_overrides():
    raw = [{"name": "Pool", "type": "thermal", "enabled": True, "target_c": 28.0,
            "min_c": 27.0, "max_c": 29.0,
            "stages": [{"name": "klein", "power_w": 400, "heat_w": 3000}]}]
    over = {"Pool": {"enabled": False, "target_c": 26.0, "max_c": 30.0}}
    loads = parse_controllable_loads(raw, over)
    assert loads[0].enabled is False
    assert loads[0].target_c == 26.0 and loads[0].max_c == 30.0
    assert loads[0].min_c == 27.0            # unverändert


def test_parse_controllable_loads_applies_power_profile_override():
    raw = [{"name": "Waschmaschine", "type": "deferrable",
            "power_w": 1200, "runtime_minutes": 60,
            "power_profile_w": [1000, 500]}]
    over = {"Waschmaschine": {"power_profile_w": [2100.0, 300.0, 150.0],
                               "deadline_hours": 8.0}}

    load = parse_controllable_loads(raw, over)[0]

    assert load.power_profile_w == [2100.0, 300.0, 150.0]
    assert load.deadline_hours == 8.0


def test_parse_deferrable_load_power_feedback():
    load = parse_controllable_loads([{
        "name": "Waschmaschine", "type": "deferrable",
        "power_w": 1800, "runtime_minutes": 120,
        "feedback_topic": "washer/on",
        "power_topic": "washer/power",
        "feedback_on_threshold_w": 12.5,
        "feedback_required": True,
    }])[0]

    assert load.feedback_topic == "washer/on"
    assert load.power_topic == "washer/power"
    assert load.feedback_on_threshold_w == 12.5
    assert load.feedback_required is True


def test_load_config_merges_overlay(tmp_path):
    """End-to-end: reale config.yaml + Overlay -> gemergter Wert."""
    import shutil
    from ems.config import load_config
    src = _require_real_config()
    cfg = tmp_path / "config.yaml"
    shutil.copy(src, cfg)
    save_override(str(cfg), "optimization.charge_strategy", "peak")
    c = load_config(str(cfg))
    assert c.optimization.charge_strategy == "peak"


def test_general_owns_location_and_holiday_settings(tmp_path):
    """Neues Schema: quellenübergreifende Standort-/Kalenderwerte in general."""
    import shutil
    from ems.config import load_config

    cfg = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", cfg)
    raw = yaml.safe_load(cfg.read_text())
    raw["general"].update({
        "latitude": 47.85, "longitude": 12.07,
        "holiday_country": "DE", "holiday_subdivision": "BY",
    })
    cfg.write_text(yaml.safe_dump(raw))
    loaded = load_config(str(cfg))
    assert loaded.general.latitude == 47.85
    assert loaded.general.longitude == 12.07
    assert loaded.general.holiday_country == "DE"
    assert loaded.general.holiday_subdivision == "BY"
    assert not hasattr(loaded.weather, "latitude")
    assert not hasattr(loaded.forecast, "holiday_country")


def test_legacy_location_and_holidays_remain_loadable(tmp_path):
    """Alte Dateien migrieren ohne Startfehler; general hat dabei Vorrang."""
    import shutil
    from ems.config import load_config

    cfg = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", cfg)
    raw = yaml.safe_load(cfg.read_text())
    for key in ("latitude", "longitude", "holiday_country",
                "holiday_subdivision"):
        raw["general"].pop(key, None)
    raw["weather"].update({"latitude": 48.1, "longitude": 11.6})
    raw["forecast"].update({
        "holiday_country": "AT", "holiday_subdivision": "7"})
    cfg.write_text(yaml.safe_dump(raw))
    legacy = load_config(str(cfg))
    assert (legacy.general.latitude, legacy.general.longitude) == (48.1, 11.6)
    assert legacy.general.holiday_country == "AT"
    assert legacy.general.holiday_subdivision == "7"

    raw["general"]["latitude"] = 47.0
    raw["general"]["holiday_country"] = "DE"
    cfg.write_text(yaml.safe_dump(raw))
    preferred = load_config(str(cfg))
    assert preferred.general.latitude == 47.0
    assert preferred.general.holiday_country == "DE"


def test_penalty_invariant_enforced_when_zeroing_negative_price(tmp_path):
    """P2#6: bei feed_in.zero_at_negative_price muss der Einspeise-Malus größer
    als der Late-Zeitmalus sein – sonst wird bei Negativpreis eingespeist statt
    geladen/abgeregelt. Ein (auch per Overlay) kleinerer Wert bricht den Start ab."""
    import shutil

    from ems.config import load_config
    cfg = tmp_path / "config.yaml"
    shutil.copy(_require_real_config(), cfg)
    save_override(str(cfg), "feed_in.zero_at_negative_price", True)
    save_override(str(cfg), "optimization.late_charge_delay_ct_kwh", 5.0)
    save_override(str(cfg), "optimization.negative_price_export_penalty_ct_kwh", 1.0)
    with pytest.raises(ValueError, match="negative_price_export_penalty"):
        load_config(str(cfg))
    # Gültige Kombination (Malus > Zeitmalus) lädt wieder.
    save_override(str(cfg), "optimization.negative_price_export_penalty_ct_kwh", 10.0)
    assert load_config(str(cfg)) is not None


def test_calibration_overrides_are_reloaded_without_restart(tmp_path):
    """Wöchentliche PV-Bandkalibrierung wird im nächsten EMS-Zyklus aktiv."""
    import shutil
    from ems.config import load_config
    from ems.main import _reload_calibration_overrides

    cfg = tmp_path / "config.yaml"
    shutil.copy(_require_real_config(), cfg)
    running = load_config(str(cfg))
    assert running.pv_model.p10_uncertainty != 0.511
    assert running.pv_model.p90_uncertainty != 0.293

    save_override(str(cfg), "pv_model.p10_uncertainty", 0.511)
    save_override(str(cfg), "pv_model.p90_uncertainty", 0.293)
    _reload_calibration_overrides(running)

    assert running.pv_model.p10_uncertainty == 0.511
    assert running.pv_model.p90_uncertainty == 0.293


def test_calibration_reload_updates_learned_load_profile_and_battery(monkeypatch):
    """Extern gelernte Profile und Batterieparameter erreichen den laufenden
    Config-Baum und damit auch das Steuerungs-Panel ohne Dienstneustart."""
    from types import SimpleNamespace

    import ems.config as config_module
    from ems.dashboard import _controls_block
    from ems.main import _reload_calibration_overrides

    def load(profile, runtime, eta):
        return SimpleNamespace(
            dashboard=SimpleNamespace(controls_enabled=True),
            optimization=SimpleNamespace(charge_strategy="auto"),
            e3dc_rscp=SimpleNamespace(control_enabled=True),
            mqtt=SimpleNamespace(enabled=True),
            pv_model=SimpleNamespace(p10_uncertainty=0.2,
                                     p90_uncertainty=0.2),
            house_battery=SimpleNamespace(
                discharge_efficiency=eta,
                max_dc_charge_w=12000,
                max_discharge_w=12000,
            ),
            controllable_loads=[SimpleNamespace(
                name="Spuelmaschine", type="deferrable", enabled=False,
                power_w=2000.0, power_profile_w=profile,
                runtime_minutes=runtime, window_from_hour=8,
                window_to_hour=22, deadline_hours=24,
            )],
        )

    running = load([2000, 300, 150], 120, 0.85)
    running._source_path = "/tmp/config.yaml"
    running._overrides_mtime = 1.0
    learned = load([2076.6, 45.5, 63.3, 55.7], 60, 0.863)
    monkeypatch.setattr(config_module, "_overrides_path",
                        lambda _path: "/tmp/config_overrides.yaml")
    monkeypatch.setattr(config_module, "load_config", lambda _path: learned)
    monkeypatch.setattr(os.path, "getmtime", lambda _path: 2.0)

    _reload_calibration_overrides(running)

    dishwasher = running.controllable_loads[0]
    assert dishwasher.power_profile_w == [2076.6, 45.5, 63.3, 55.7]
    assert dishwasher.runtime_minutes == 60
    assert running.house_battery.discharge_efficiency == 0.863
    assert "2076.6, 45.5, 63.3, 55.7" in _controls_block(running)


def test_broken_overlay_is_reported_not_swallowed(tmp_path, caplog):
    """Ein kaputtes Overlay ist nicht harmlos: die wöchentlich kalibrierten
    Werte fallen still auf die Standardwerte zurück, und der Plan rechnet mit
    veralteten Parametern weiter. Vorher war der Handler ein blankes `pass`."""
    import logging
    from ems.config import load_config
    basis = tmp_path / "config.yaml"
    beispiel = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config.example.yaml")
    with open(beispiel, encoding="utf-8") as fh:
        basis.write_text(fh.read(), encoding="utf-8")
    # kein Overlay -> still, das ist der Normalfall
    with caplog.at_level(logging.WARNING, logger="ems.config"):
        load_config(str(basis))
    assert not [r for r in caplog.records if "Overlay" in r.message]

    # kaputtes Overlay -> Warnung mit Pfad
    _overrides_path(str(basis))
    (tmp_path / "config_overrides.yaml").write_text(
        "house_battery:\n  capacity_wh: [unbalanced\n", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ems.config"):
        load_config(str(basis))
    treffer = [r for r in caplog.records if "Overlay" in r.message]
    assert treffer, "kaputtes Overlay muss gemeldet werden"
    assert "NICHT angewandt" in treffer[0].getMessage()
