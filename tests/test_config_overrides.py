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


def test_ignored_overlay_keys_are_reported(caplog):
    """Das Overlay wendet nur eine Erlaubnisliste an. Ein Eintrag ausserhalb
    davon sah in der Datei richtig aus und kam nie an - genau daran ist am
    15.08.2026 ein Versuch gescheitert, binary_horizon_hours so zu setzen."""
    import logging
    from ems.config import parse_controllable_loads
    roh = [{"name": "Pool", "type": "thermal", "enabled": True,
            "target_c": 28.0, "min_c": 27.0, "max_c": 28.5,
            "binary_horizon_hours": 0.0, "stages": []}]
    with caplog.at_level(logging.WARNING, logger="ems.config"):
        lasten = parse_controllable_loads(
            roh, {"Pool": {"max_c": 29.0, "binary_horizon_hours": 24.0}})
    assert lasten[0].max_c == 29.0                    # erlaubt -> angewandt
    assert lasten[0].binary_horizon_hours == 0.0      # nicht erlaubt -> ignoriert
    meldungen = [r.getMessage() for r in caplog.records]
    assert any("binary_horizon_hours" in m and "nicht angewandt" in m
               for m in meldungen), meldungen
    assert not any("max_c" in m for m in meldungen)


# --------------------------------------------------------------------------- #
# Overlay-Schreiben: eindeutige Temp-Datei, gemeinsame Sperre, kein stilles
# Ueberschreiben eines unlesbaren Overlays
# --------------------------------------------------------------------------- #
def _beispiel(tmp_path, aendern=None):
    """config.example.yaml nach tmp_path kopieren, optional veraendert."""
    beispiel = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config.example.yaml")
    with open(beispiel, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if aendern is not None:
        aendern(doc)
    ziel = tmp_path / "config.yaml"
    ziel.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return ziel


def test_save_override_uses_a_unique_temp_file(tmp_path):
    """Die feste ``.tmp``-Datei teilten sich gleichzeitige Schreiber. Eine
    fremde Datei dieses Namens darf weder ueberschrieben noch verbraucht
    werden, und es bleibt keine Temp-Datei liegen."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    fremd = tmp_path / "config_overrides.yaml.tmp"
    fremd.write_text("halb geschrieben von einem anderen Prozess")
    save_override(str(cfg), "optimization.charge_strategy", "peak")
    assert fremd.read_text() == "halb geschrieben von einem anderen Prozess"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "config.yaml", "config_overrides.yaml", "config_overrides.yaml.lock",
        "config_overrides.yaml.tmp"]


def test_save_override_keeps_a_broken_overlay_for_repair(tmp_path, caplog):
    """Ein kaputtes Overlay wurde durch ``{}`` plus den neuen Schluessel
    ersetzt - alle kalibrierten Werte waren unwiederbringlich weg."""
    import logging
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    overlay = tmp_path / "config_overrides.yaml"
    kaputt = "house_battery:\n  capacity_wh: [unbalanced\n"
    overlay.write_text(kaputt)
    with caplog.at_level(logging.ERROR, logger="ems.config"):
        save_override(str(cfg), "optimization.charge_strategy", "late")
    defekt = list(tmp_path.glob("config_overrides.yaml.defekt-*"))
    assert len(defekt) == 1, "die kaputte Datei muss erhalten bleiben"
    assert defekt[0].read_text() == kaputt
    assert yaml.safe_load(overlay.read_text()) == {
        "optimization": {"charge_strategy": "late"}}
    assert any("kaputt" in r.getMessage() for r in caplog.records)


def test_save_override_does_not_overwrite_an_unreadable_overlay(
        tmp_path, monkeypatch):
    """Lesefehler (Rechte, E/A): frueher wie "leer" behandelt und die Datei
    ueberschrieben. Jetzt Fehler, die Datei bleibt unangetastet."""
    import builtins
    import ems.config as config_module
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    overlay = tmp_path / "config_overrides.yaml"
    inhalt = "house_battery:\n  capacity_wh: 12345\n"
    overlay.write_text(inhalt)

    def gesperrt(pfad, mode="r", *args, **kwargs):
        if str(pfad) == str(overlay) and "r" in mode:
            raise PermissionError(13, "Permission denied", str(pfad))
        return builtins.open(pfad, mode, *args, **kwargs)

    monkeypatch.setattr(config_module, "open", gesperrt, raising=False)
    with pytest.raises(OSError, match="NICHT ueberschrieben"):
        save_override(str(cfg), "optimization.charge_strategy", "late")
    monkeypatch.undo()
    assert overlay.read_text() == inhalt


def test_save_override_replaces_a_colliding_scalar(tmp_path):
    """Stand unter einem Zwischenschluessel ein Skalar, landete der neue Wert
    in einem losgeloesten Dict - die Aenderung ging still verloren."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    (tmp_path / "config_overrides.yaml").write_text("optimization: 5\n")
    save_override(str(cfg), "optimization.charge_strategy", "peak")
    assert yaml.safe_load((tmp_path / "config_overrides.yaml").read_text()) == {
        "optimization": {"charge_strategy": "peak"}}


def test_save_override_waits_for_the_shared_overlay_lock(tmp_path):
    """save_override laeuft unter OVERLAY_LOCK - haelt ein anderer Schreiber
    (der Editor) die Sperre, wartet es, statt dazwischenzuschreiben."""
    import threading
    import ems.config as config_module
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    overlay = tmp_path / "config_overrides.yaml"
    worker = threading.Thread(
        target=save_override,
        args=(str(cfg), "optimization.charge_strategy", "peak"))
    with config_module.OVERLAY_LOCK:
        worker.start()
        worker.join(0.3)
        assert worker.is_alive() and not overlay.exists()
    worker.join(5)
    assert not worker.is_alive() and overlay.exists()


# --------------------------------------------------------------------------- #
# Validierung: strikte Booleans, Wertebereiche, unbekannte Schluessel
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("wert,erwartet", [
    (True, True), (False, False), (1, True), (0, False), (None, False),
    ("", False), ("true", True), ("False", False), (" Ja ", True),
    ("nein", False), ("on", True), ("OFF", False), ("1", True), ("0", False)])
def test_parse_bool_accepts_the_documented_spellings(wert, erwartet):
    from ems.config import parse_bool
    assert parse_bool(wert, "x.y") is erwartet


@pytest.mark.parametrize("wert", ["vielleicht", "flase", 2, 0.5, [], {}])
def test_parse_bool_rejects_everything_else(wert):
    from ems.config import parse_bool
    with pytest.raises(ValueError, match="x.y"):
        parse_bool(wert, "x.y")


def test_string_false_does_not_enable_real_control(tmp_path):
    """bool("false") ist True: aus ``control_enabled: "false"`` wurde eine
    eingeschaltete E3/DC-Steuerung, die real in den Speicher eingreift."""
    from ems.config import load_config

    def setze(doc):
        doc["e3dc_rscp"]["control_enabled"] = "false"
        doc["optimization"]["allow_grid_discharge"] = "false"
        doc["dashboard"]["controls_enabled"] = "nein"
        doc["mqtt"]["enabled"] = "ja"
        doc["influxdb"]["signals"]["vehicle_soc"]["optional"] = "true"
    c = load_config(str(_beispiel(tmp_path, setze)))
    assert c.e3dc_rscp.control_enabled is False
    assert c.optimization.allow_grid_discharge is False
    assert c.dashboard.controls_enabled is False
    assert c.mqtt.enabled is True
    assert c.influxdb.signals["vehicle_soc"].optional is True


def test_unparseable_boolean_names_its_path(tmp_path):
    from ems.config import load_config
    pfad = _beispiel(tmp_path, lambda d: d["e3dc_rscp"].update(
        control_enabled="aus?"))
    with pytest.raises(ValueError, match=r"e3dc_rscp\.control_enabled"):
        load_config(str(pfad))


def test_load_override_enabled_string_is_read_strictly():
    lasten = parse_controllable_loads(
        [{"name": "Pool", "type": "thermal", "enabled": True, "stages": []}],
        {"Pool": {"enabled": "false"}})
    assert lasten[0].enabled is False


@pytest.mark.parametrize("abschnitt,schluessel,wert,treffer", [
    ("house_battery", "min_soc_percent", 95, "min_soc_percent"),
    ("house_battery", "max_soc_percent", 120, "max_soc_percent"),
    ("house_battery", "min_soc_percent", -5, "min_soc_percent"),
    ("house_battery", "charge_efficiency", 96, "charge_efficiency"),
    ("house_battery", "discharge_efficiency", 0, "discharge_efficiency"),
    ("house_battery", "ac_charge_efficiency", 1.2, "ac_charge_efficiency"),
    ("house_battery", "capacity_wh", 0, "capacity_wh"),
    ("general", "run_interval_minutes", 0, "run_interval_minutes"),
    ("optimization", "charge_strategy", "peek", "charge_strategy"),
    ("optimization", "solver", "gurobi", "solver"),
    ("e3dc_rscp", "batt_sign", 0, "batt_sign"),
    ("e3dc_rscp", "batt_sign", 2, "batt_sign"),
])
def test_nonsense_values_are_rejected_with_a_clear_message(
        tmp_path, abschnitt, schluessel, wert, treffer):
    """Diese Werte liessen den Dienst bisher starten: vertauschtes SoC-Band,
    Wirkungsgrad in Prozent, Tippfehler in Solver/Strategie (lief dann still
    mit CBC bzw. asap)."""
    from ems.config import load_config

    def setze(doc):
        if abschnitt == "house_battery" and schluessel == "min_soc_percent" \
                and wert == 95:
            doc[abschnitt]["max_soc_percent"] = 90
        doc[abschnitt][schluessel] = wert
    with pytest.raises(ValueError, match=treffer):
        load_config(str(_beispiel(tmp_path, setze)))


def test_solver_and_strategy_are_read_case_insensitively(tmp_path):
    """make_solver verglich schon bisher klein geschrieben; "Peak" lief dagegen
    still als asap."""
    from ems.config import load_config

    def setze(doc):
        doc["optimization"]["solver"] = "HiGHS"
        doc["optimization"]["charge_strategy"] = "Peak"
        doc["e3dc_rscp"]["batt_sign"] = -1
    c = load_config(str(_beispiel(tmp_path, setze)))
    assert c.optimization.solver == "highs"
    assert c.optimization.charge_strategy == "peak"
    assert c.e3dc_rscp.batt_sign == -1.0


def test_unknown_keys_are_reported_with_a_suggestion(tmp_path, caplog):
    """Tippfehler wurden kommentarlos ignoriert: die Einstellung wirkte nicht
    und nichts wies darauf hin. Nur Warnung - aeltere Configs muessen starten."""
    import logging
    from ems.config import load_config

    def setze(doc):
        doc["optimization"]["allow_grid_dischrage"] = True
        doc["dashbord"] = {"enabled": True}
        doc["house_battery"]["voellig_fremd_xyz"] = 1
        doc["influxdb"]["signals"]["battery_soc"]["agregation"] = "last"
        doc["controllable_loads"] = [{
            "name": "Pool", "type": "thermal", "thermostat_an": True,
            "window": {"von": 8},
            "stages": [{"name": "klein", "power_w": 400, "heat_wat": 1}]}]
    with caplog.at_level(logging.WARNING, logger="ems.config"):
        c = load_config(str(_beispiel(tmp_path, setze)))
    assert c.optimization.allow_grid_discharge is False      # laedt trotzdem
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "'optimization.allow_grid_dischrage'" in text
    assert "meintest du 'allow_grid_discharge'?" in text
    assert "'dashbord'" in text and "meintest du 'dashboard'?" in text
    assert "'house_battery.voellig_fremd_xyz' wird ignoriert." in text
    assert "meintest du 'aggregation'?" in text
    assert "controllable_loads[Pool].thermostat_an" in text
    assert "controllable_loads[Pool].window.von" in text
    assert "controllable_loads[Pool].stages[klein].heat_wat" in text

    # je Datei nur einmal - der Dienst laedt die Config immer wieder
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ems.config"):
        load_config(str(tmp_path / "config.yaml"))
    assert not [r for r in caplog.records if "unbekannter" in r.getMessage()]


def test_example_config_and_legacy_keys_raise_no_unknown_key_warning(
        tmp_path, caplog):
    """Die Schluesselliste muss vollstaendig sein: weder die Beispiel-Config
    noch die weiter gelesenen Alt-Schluessel duerfen eine Warnung ausloesen."""
    import logging
    from ems.config import load_config

    def alt(doc):
        doc["weather"]["latitude"] = 48.1
        doc["weather"]["longitude"] = 11.5
        doc["forecast"]["holiday_country"] = "DE"
        doc["forecast"]["holiday_subdivision"] = "BY"
        doc["optimization"]["battery_hold_penalty_ct"] = 4.0
        doc["influxdb"]["v1"]["timeout"] = 20
        doc["controllable_loads_overrides"] = {"Pool": {"enabled": False}}
        doc["controllable_loads"] = [
            {"name": "Pool", "type": "thermal", "pv_surplus_only": True,
             "temp_max_age_minutes": 30,
             "season": {"from": "04-15", "to": "10-15"},
             "stages": [{"name": "klein", "power_w": 400, "heat_w": 3000,
                         "mqtt_topic": "pool/set"}]},
            {"name": "Wama", "type": "deferrable", "mqtt_topic": "wama/start",
             "window": {"from": 8, "to": 22}}]
    for nr, aendern in enumerate((None, alt)):
        ordner = tmp_path / str(nr)
        ordner.mkdir()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="ems.config"):
            load_config(str(_beispiel(ordner, aendern)))
        assert not [r.getMessage() for r in caplog.records
                    if "unbekannter" in r.getMessage()]


def test_control_switched_off_does_not_break_curtailment_config(
        tmp_path, caplog):
    """Der Dashboard-Schalter "E3/DC-Steuerung aus" schreibt control_enabled:
    false ins Overlay. Mit curtailment_control_enabled: true in der config.yaml
    warf load_config danach ValueError - jeder Zyklus scheiterte, und nach
    einem Neustart startete der Dienst nicht mehr."""
    import logging
    from ems.config import load_config

    def setze(doc):
        doc["e3dc_rscp"]["control_enabled"] = True
        doc["e3dc_rscp"]["curtailment_control_enabled"] = True
    cfg = _beispiel(tmp_path, setze)
    c = load_config(str(cfg))
    assert c.e3dc_rscp.curtailment_control_enabled is True

    save_override(str(cfg), "e3dc_rscp.control_enabled", False)
    with caplog.at_level(logging.WARNING, logger="ems.config"):
        c = load_config(str(cfg))                     # vorher: ValueError
    assert c.e3dc_rscp.control_enabled is False
    assert c.e3dc_rscp.curtailment_control_enabled is False
    assert any("Abregelsteuerung bleibt inaktiv" in r.getMessage()
               for r in caplog.records)

    save_override(str(cfg), "e3dc_rscp.control_enabled", True)
    c = load_config(str(cfg))
    assert c.e3dc_rscp.curtailment_control_enabled is True
