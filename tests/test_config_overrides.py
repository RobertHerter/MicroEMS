"""Overlay-Persistenz fürs Dashboard: save_override schreibt config_overrides.yaml,
load_config merged sie über die Basis; steuerbare Lasten werden per Slug überlagert."""
from __future__ import annotations

import yaml

from ems.config import (_deep_merge, _overrides_path, save_override,
                        parse_controllable_loads)


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
    src = "/opt/ems/config.yaml"
    cfg = tmp_path / "config.yaml"
    shutil.copy(src, cfg)
    save_override(str(cfg), "optimization.charge_strategy", "peak")
    c = load_config(str(cfg))
    assert c.optimization.charge_strategy == "peak"
