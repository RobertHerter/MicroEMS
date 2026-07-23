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
