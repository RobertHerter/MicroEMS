"""Tests der eingehenden MQTT-Kommandos (ohne Broker)."""
from __future__ import annotations

from datetime import time as dtime

from ems.config import ControllableLoad, LoadStage
from ems.homey_mqtt import HomeyMqttPublisher
from tests.test_synthetic import make_config


class Msg:
    def __init__(self, topic: str, payload: str):
        self.topic = topic
        self.payload = payload.encode()


def _pub():
    cfg = make_config()
    return cfg, HomeyMqttPublisher(cfg)


def test_departure_and_target_overrides():
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/car_target_soc", "90"))
    assert pub.target_soc_override == 90.0
    assert pub.recalc_event.is_set()
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "06:30"))
    assert pub.departure_override == dtime(6, 30)

    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.target_soc_percent == 90.0
    assert cfg.vehicle.departure_time == dtime(6, 30)


def test_reset_restores_config_defaults():
    cfg, pub = _pub()
    default_dep = cfg.vehicle.departure_time
    default_tgt = cfg.vehicle.target_soc_percent
    pub._on_message(None, None, Msg("ems/cmd/car_target_soc", "55"))
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "05:15"))
    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.target_soc_percent == 55.0

    pub._on_message(None, None, Msg("ems/cmd/car_target_soc", "default"))
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", ""))
    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.target_soc_percent == default_tgt
    assert cfg.vehicle.departure_time == default_dep


def test_holiday_mode_disables_departures_and_target():
    """'off' = Urlaubsmodus: keine Abfahrten an keinem Tag -> im Optimierer
    entfällt jeder Ziel-SoC-Zwang (has_any_departure = False)."""
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "urlaub"))
    assert pub.departure_disabled and pub.recalc_event.is_set()
    pub.apply_vehicle_overrides(cfg.vehicle)
    assert not cfg.vehicle.has_any_departure
    assert all(cfg.vehicle.departure_for_weekday(d) is None for d in range(7))

    # neue Uhrzeit beendet den Urlaubsmodus
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "07:30"))
    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.has_any_departure
    assert cfg.vehicle.departure_time == dtime(7, 30)

    # 'default' ebenfalls
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "off"))
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "default"))
    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.has_any_departure
    assert cfg.vehicle.departure_time == dtime(7, 0)   # make_config-Standard


def test_invalid_payloads_are_ignored():
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "06:30"))
    pub._on_message(None, None, Msg("ems/cmd/car_departure_time", "kaputt"))
    assert pub.departure_override == dtime(6, 30)   # bleibt erhalten
    pub._on_message(None, None, Msg("ems/cmd/car_target_soc", "150"))
    assert pub.target_soc_override is None          # außerhalb 1..100
    pub._on_message(None, None, Msg("ems/cmd/car_target_soc", "abc"))
    assert pub.target_soc_override is None


def test_battery_soc_overrides():
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/min_soc", "40"))
    pub._on_message(None, None, Msg("ems/cmd/max_soc", "90"))
    pub.apply_battery_overrides(cfg.house_battery)
    assert cfg.house_battery.min_soc_percent == 40.0
    assert cfg.house_battery.max_soc_percent == 90.0
    assert cfg.house_battery.min_soc_wh == 0.4 * cfg.house_battery.capacity_wh

    # Reset auf Konfigwerte (make_config: 10 / 100)
    pub._on_message(None, None, Msg("ems/cmd/min_soc", "default"))
    pub._on_message(None, None, Msg("ems/cmd/max_soc", ""))
    pub.apply_battery_overrides(cfg.house_battery)
    assert cfg.house_battery.min_soc_percent == 10.0
    assert cfg.house_battery.max_soc_percent == 100.0


def test_battery_soc_inconsistent_pair_rejected():
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/min_soc", "80"))
    pub._on_message(None, None, Msg("ems/cmd/max_soc", "50"))   # min >= max
    pub.apply_battery_overrides(cfg.house_battery)
    assert cfg.house_battery.min_soc_percent == 10.0   # Konfigwerte
    assert cfg.house_battery.max_soc_percent == 100.0
    # ungültige Payloads ignoriert
    pub._on_message(None, None, Msg("ems/cmd/min_soc", "120"))
    pub._on_message(None, None, Msg("ems/cmd/min_soc", "abc"))
    assert pub.min_soc_override == 80.0


def test_car_boost_and_recalc():
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/car_boost", "1"))
    assert pub.car_boost is True
    pub._on_message(None, None, Msg("ems/cmd/car_boost", "0"))
    assert pub.car_boost is False
    pub.recalc_event.clear()
    pub._on_message(None, None, Msg("ems/cmd/recalc", "x"))
    assert pub.recalc_event.is_set()


def _pub_with_pool():
    from ems.config import ControllableLoad, LoadStage
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, volume_l=7000,
        temp_signal="homie/homey/temperatur-pool/measure-temperature",
        stages=[LoadStage("klein", 400, 3000)])]
    return cfg, HomeyMqttPublisher(cfg)


def test_load_enable_disable_via_mqtt():
    cfg, pub = _pub_with_pool()
    pub._on_message(None, None, Msg("ems/cmd/load/pool", "off"))
    assert pub.recalc_event.is_set()
    pub.apply_load_overrides(cfg.controllable_loads)
    assert cfg.controllable_loads[0].enabled is False
    # zurück auf Konfigurationswert (True)
    pub._on_message(None, None, Msg("ems/cmd/load/pool", "default"))
    pub.apply_load_overrides(cfg.controllable_loads)
    assert cfg.controllable_loads[0].enabled is True
    # explizit wieder an
    pub._on_message(None, None, Msg("ems/cmd/load/pool", "on"))
    pub.apply_load_overrides(cfg.controllable_loads)
    assert cfg.controllable_loads[0].enabled is True


def test_pool_temp_cached_from_mqtt():
    cfg, pub = _pub_with_pool()
    topic = "homie/homey/temperatur-pool/measure-temperature"
    assert topic in pub._temp_topics
    pub._on_message(None, None, Msg(topic, "27,4"))     # Dezimal-Komma zulässig
    assert pub.get_load_temp(topic) == 27.4


def test_pool_stage_feedback_uses_real_power_as_running_state():
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", volume_l=7000,
        feedback_required=True, feedback_max_age_minutes=20,
        stages=[LoadStage("WP", 650, 4000, feedback_topic="pool/wp/on",
                          power_topic="pool/wp/power",
                          feedback_on_threshold_w=50)])]
    pub = HomeyMqttPublisher(cfg)
    assert {"pool/wp/on", "pool/wp/power"} <= set(pub._feedback_topics)
    pub._on_message(None, None, Msg("pool/wp/on", "off"))
    assert pub.get_load_feedback("Pool/WP")["on"] is False
    pub._on_message(None, None, Msg("pool/wp/power", "612,5"))
    feedback = pub.get_load_feedback("Pool/WP")
    assert feedback["fresh"] and feedback["on"] is True
    assert feedback["power_w"] == 612.5


def test_shared_power_topic_derives_multiple_pool_stages():
    cfg = make_config()
    shared = "pool/wp/total-power"
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", feedback_required=True,
        stages=[LoadStage("Grundstufe", 650, 4000, power_topic=shared,
                          feedback_on_threshold_w=10),
                LoadStage("Zusatzstufe", 400, 3000, power_topic=shared,
                          feedback_on_threshold_w=700)])]
    pub = HomeyMqttPublisher(cfg)
    assert len(pub._feedback_topics[shared]) == 2

    pub._on_message(None, None, Msg(shared, "645"))
    assert pub.get_load_feedback("Pool/Grundstufe")["on"] is True
    assert pub.get_load_feedback("Pool/Zusatzstufe")["on"] is False

    pub._on_message(None, None, Msg(shared, "1040"))
    assert pub.get_load_feedback("Pool/Grundstufe")["on"] is True
    assert pub.get_load_feedback("Pool/Zusatzstufe")["on"] is True


def test_load_lanes_includes_disabled_loads():
    """_load_lanes listet ALLE konfigurierten Lasten (auch deaktivierte)."""
    from ems.config import ControllableLoad, LoadStage
    cfg = make_config()
    cfg.controllable_loads = [
        ControllableLoad(name="Pool", type="thermal", enabled=False, volume_l=7000,
                         stages=[LoadStage("WP klein", 400, 3000),
                                 LoadStage("WP groß", 650, 4000, requires="WP klein")]),
        ControllableLoad(name="Waschmaschine", type="deferrable", enabled=False,
                         power_w=2000)]
    lanes = HomeyMqttPublisher(cfg)._load_lanes()
    assert [e["label"] for e in lanes] == ["Pool/WP klein", "Pool/WP groß", "Waschmaschine"]
    assert all(e["enabled"] is False for e in lanes)
    assert all(e["column"].startswith("load_") for e in lanes)


def test_thermostat_permission_keeps_on_when_warm():
    """WP mit eigenem Thermostat (thermostat: true): Freigabe bleibt AN, wenn
    die Ist-Temperatur >= target_c ist (Thermostat hält die WP ohnehin aus);
    "aus" nur, wenn Heizen aktiv verhindert werden muss (T < target_c)."""
    from ems.config import ControllableLoad, LoadStage
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, volume_l=8000, target_c=28.0,
        thermostat=True, temp_signal="homie/pool/temp",
        stages=[LoadStage("klein", 400, 3000)])]
    pub = HomeyMqttPublisher(cfg)
    lane = pub._load_lanes()[0]
    assert lane["thermostat"] is True and lane["target_c"] == 28.0

    # Kein Heiz-Slot geplant, Wasser WARM (29.5 >= 28) -> Freigabe AN
    pub.load_temps["homie/pool/temp"] = 29.5
    assert pub._lane_command(lane, planned_on=False) == 1
    # Kein Heiz-Slot geplant, Wasser KALT (27 < 28) -> aktiv AUS (EMS blockt)
    pub.load_temps["homie/pool/temp"] = 27.0
    assert pub._lane_command(lane, planned_on=False) == 0
    # Heiz-Slot geplant -> immer AN
    assert pub._lane_command(lane, planned_on=True) == 1
    # Keine Ist-Temperatur empfangen -> konservativ dem Plan folgen
    pub.load_temps.clear()
    assert pub._lane_command(lane, planned_on=False) == 0


def test_no_thermostat_follows_plan():
    """Ohne thermostat-Flag folgt der Befehl 1:1 dem Heizplan (wie bisher) -
    Dauer-Freigabe wäre dort ungeplantes Heizen."""
    from ems.config import ControllableLoad, LoadStage
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, volume_l=8000, target_c=28.0,
        thermostat=False, temp_signal="homie/pool/temp",
        stages=[LoadStage("klein", 400, 3000)])]
    pub = HomeyMqttPublisher(cfg)
    lane = pub._load_lanes()[0]
    pub.load_temps["homie/pool/temp"] = 29.5     # warm - egal, kein Thermostat
    assert pub._lane_command(lane, planned_on=False) == 0
    assert pub._lane_command(lane, planned_on=True) == 1
