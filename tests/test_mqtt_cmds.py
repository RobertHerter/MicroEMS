"""Tests der eingehenden MQTT-Kommandos (ohne Broker)."""
from __future__ import annotations

from datetime import time as dtime

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
    pub._on_message(None, None, Msg("ems/cmd/target_soc", "90"))
    assert pub.target_soc_override == 90.0
    assert pub.recalc_event.is_set()
    pub._on_message(None, None, Msg("ems/cmd/departure_time", "06:30"))
    assert pub.departure_override == dtime(6, 30)

    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.target_soc_percent == 90.0
    assert cfg.vehicle.departure_time == dtime(6, 30)


def test_reset_restores_config_defaults():
    cfg, pub = _pub()
    default_dep = cfg.vehicle.departure_time
    default_tgt = cfg.vehicle.target_soc_percent
    pub._on_message(None, None, Msg("ems/cmd/target_soc", "55"))
    pub._on_message(None, None, Msg("ems/cmd/departure_time", "05:15"))
    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.target_soc_percent == 55.0

    pub._on_message(None, None, Msg("ems/cmd/target_soc", "default"))
    pub._on_message(None, None, Msg("ems/cmd/departure_time", ""))
    pub.apply_vehicle_overrides(cfg.vehicle)
    assert cfg.vehicle.target_soc_percent == default_tgt
    assert cfg.vehicle.departure_time == default_dep


def test_invalid_payloads_are_ignored():
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/departure_time", "06:30"))
    pub._on_message(None, None, Msg("ems/cmd/departure_time", "kaputt"))
    assert pub.departure_override == dtime(6, 30)   # bleibt erhalten
    pub._on_message(None, None, Msg("ems/cmd/target_soc", "150"))
    assert pub.target_soc_override is None          # außerhalb 1..100
    pub._on_message(None, None, Msg("ems/cmd/target_soc", "abc"))
    assert pub.target_soc_override is None


def test_car_boost_and_recalc():
    cfg, pub = _pub()
    pub._on_message(None, None, Msg("ems/cmd/car_boost", "1"))
    assert pub.car_boost is True
    pub._on_message(None, None, Msg("ems/cmd/car_boost", "0"))
    assert pub.car_boost is False
    pub.recalc_event.clear()
    pub._on_message(None, None, Msg("ems/cmd/recalc", "x"))
    assert pub.recalc_event.is_set()
