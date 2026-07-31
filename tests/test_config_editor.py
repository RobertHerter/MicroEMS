from __future__ import annotations

import copy
import shutil

import pytest
import yaml

from ems.config import load_config
from ems.config_editor import (
    editor_allowed,
    editor_html,
    editor_payload,
    save_document,
    validate_document,
)
from tests.test_synthetic import make_config


def _config_copy(tmp_path):
    path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", path)
    return path


def test_editor_requires_control_flag_editor_flag_and_credentials():
    cfg = make_config()
    cfg.dashboard.controls_enabled = True
    cfg.dashboard.config_editor_enabled = True
    cfg.dashboard.username = "admin"
    cfg.dashboard.password = "secret"
    assert editor_allowed(cfg)
    for field in ("controls_enabled", "config_editor_enabled", "username", "password"):
        changed = copy.deepcopy(cfg)
        setattr(changed.dashboard, field, False if field.endswith("enabled") else "")
        assert not editor_allowed(changed)


def test_payload_merges_regular_and_load_overrides(tmp_path):
    path = _config_copy(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["controllable_loads"] = [{
        "name": "Pool Pumpe", "type": "thermal", "target_c": 28,
        "stages": [{"name": "WP klein", "power_w": 400, "heat_w": 3000}],
    }]
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    (tmp_path / "config_overrides.yaml").write_text(yaml.safe_dump({
        "optimization": {"charge_strategy": "late"},
        "controllable_loads_overrides": {
            "Pool_Pumpe": {
                "target_c": 27,
                "stage_heat_w": {"WP_klein": 3300},
            },
        },
    }))

    payload = editor_payload(str(path))
    assert payload["had_overrides"] is True
    assert payload["config"]["optimization"]["charge_strategy"] == "late"
    load = payload["config"]["controllable_loads"][0]
    assert load["target_c"] == 27
    assert load["stages"][0]["heat_w"] == 3300
    assert "controllable_loads_overrides" not in payload["config"]
    assert payload["descriptions"]["controllable_loads"]


def test_validate_and_save_atomically_consolidates_overlay(tmp_path):
    path = _config_copy(tmp_path)
    overlay = tmp_path / "config_overrides.yaml"
    overlay.write_text("optimization:\n  charge_strategy: peak\n")
    payload = editor_payload(str(path))
    document = payload["config"]
    document["optimization"]["charge_strategy"] = "late"
    result = validate_document(document)
    assert result["valid"] is True

    saved = save_document(str(path), document, payload["revision"])
    assert saved["saved"] is True
    assert yaml.safe_load(overlay.read_text()) == {}
    assert load_config(str(path)).optimization.charge_strategy == "late"
    backups = list((tmp_path / "backup" / "config").glob("config-*.yaml"))
    assert backups
    with pytest.raises(RuntimeError, match="zwischenzeitlich"):
        save_document(str(path), document, payload["revision"])


def test_validate_rejects_invalid_configuration(tmp_path):
    path = _config_copy(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["general"]["slot_minutes"] = 7
    with pytest.raises(ValueError):
        validate_document(document)


def test_every_editable_field_has_a_description(tmp_path):
    """Jedes im Editor sichtbare Eingabefeld braucht einen Erklärtext - sonst
    stehen dort nackte Schlüssel, die man nur raten kann. Geprüft wird gegen die
    Beispiel-Config (vollständiger Feldsatz, unabhängig von der Live-Anlage).

    Die Pfadbildung entspricht dem Editor-JS: Objekt-Kind `pfad.key`,
    Listenpunkt `pfad[i]`, Nachschlag über `pfad` bzw. `pfad` mit `[i]` -> `[]`.
    """
    import pathlib
    import re
    import shutil
    from ems.config_editor import editor_payload

    example = pathlib.Path(__file__).resolve().parents[1] / "config.example.yaml"
    # In ein eigenes Verzeichnis kopieren: so wirkt kein lokales Overlay mit und
    # die Beschreibungen kommen reproduzierbar aus der Beispiel-Config.
    shutil.copy(example, tmp_path / "config.yaml")
    shutil.copy(example, tmp_path / "config.example.yaml")
    payload = editor_payload(str(tmp_path / "config.yaml"))
    doc, desc = payload["config"], payload["descriptions"]

    def described(path):
        return bool(desc.get(path) or desc.get(re.sub(r"\[\d+\]", "[]", path)))

    missing = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
        elif not described(path):                 # Skalar = eigenes Eingabefeld
            missing.append(re.sub(r"\[\d+\]", "[]", path))

    for key, value in doc.items():
        walk(value, key)
        assert desc.get(key), f"Panel '{key}' ohne Beschreibung"

    assert not sorted(set(missing)), (
        "Felder ohne Beschreibung im Editor: " + ", ".join(sorted(set(missing))))


def test_new_safety_options_are_documented_in_the_editor():
    """Neue Konfigurationswerte müssen im Editor erklärt sein - sonst stehen sie
    dort unkommentiert und werden falsch gesetzt. max_c gehört
    zusätzlich in die Vorlage für neue thermische Lasten."""
    from ems.config_editor import LOAD_DESCRIPTIONS, LOAD_TEMPLATES
    assert "controllable_loads[].max_c" in LOAD_DESCRIPTIONS
    assert "max_c" in LOAD_TEMPLATES["thermal"]
    # Die Optimierer-Optionen werden über die Kommentare der Beispiel-Config
    # beschrieben - dort müssen sie also stehen.
    import pathlib
    example = pathlib.Path(__file__).resolve().parents[1] / "config.example.yaml"
    text = example.read_text(encoding="utf-8")
    for key in ("mode_prefill_soc_percent", "mode_prefill_penalty_ct_kwh",
                "no_grid_import_penalty_ct_kwh", "grid_overload_penalty_ct_kwh"):
        assert f"{key}:" in text, f"{key} fehlt in config.example.yaml"


def test_heating_limit_is_max_c():
    """max_c ist die Heizgrenze - ein zweiter Parameter dafuer existiert nicht
    mehr. Frueher stand daneben thermostat_cutoff_c; zwei Werte fuer dieselbe
    Sache mussten uebereinstimmen, und genau das tat die Produktivkonfiguration
    nicht (max_c 32 gegen 28,5 am Geraet)."""
    from ems.config import ControllableLoad, parse_controllable_loads
    loads = parse_controllable_loads([{
        "name": "Pool", "type": "thermal", "enabled": True, "volume_l": 7000,
        "target_c": 28.0, "max_c": 28.5, "thermostat": True,
        "stages": [{"name": "s1", "power_w": 400, "heat_w": 3000}],
    }])
    assert loads[0].max_c == 28.5
    assert not hasattr(ControllableLoad, "thermostat_cutoff_c")


def test_editor_html_is_responsive_and_supports_load_management():
    html = editor_html().decode()
    assert "EMS-Konfiguration" in html
    assert "Speichern &amp; EMS neu starten" in html
    assert "Thermische Last" in html
    assert "Last entfernen" in html
    assert "/api/config/" in html and "submit('save')" in html
    assert "@media(max-width:700px)" in html
    assert "html.dark" in html
