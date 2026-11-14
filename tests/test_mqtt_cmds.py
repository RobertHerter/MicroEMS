"""Tests der eingehenden MQTT-Kommandos (ohne Broker)."""
from __future__ import annotations

from datetime import time as dtime

import pandas as pd

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


def test_pool_stage_newer_relay_state_does_not_override_real_power():
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", volume_l=7000,
        feedback_required=True,
        stages=[LoadStage("WP klein", 400, 3000,
                          feedback_topic="pool/wp-small/on",
                          power_topic="pool/wp/total-power",
                          feedback_on_threshold_w=700)])]
    pub = HomeyMqttPublisher(cfg)

    pub._on_message(None, None, Msg("pool/wp/total-power", "3.2"))
    # Relais/Freigabe kommt danach und ist damit neuer, die gemessene Leistung
    # beweist aber weiterhin, dass die WP nicht tatsächlich läuft.
    pub._on_message(None, None, Msg("pool/wp-small/on", "on"))
    feedback = pub.get_load_feedback("Pool/WP klein")

    assert feedback["power_w"] == 3.2
    assert feedback["on"] is False


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


def test_deferrable_load_uses_power_feedback():
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Waschmaschine", type="deferrable", enabled=True,
        power_w=2000, runtime_minutes=120,
        power_topic="washer/power", feedback_on_threshold_w=10,
        feedback_required=True)]
    pub = HomeyMqttPublisher(cfg)

    assert "washer/power" in pub._feedback_topics
    pub._on_message(None, None, Msg("washer/power", "4.5"))
    assert pub.get_load_feedback("Waschmaschine")["on"] is False
    pub._on_message(None, None, Msg("washer/power", "1842"))
    feedback = pub.get_load_feedback("Waschmaschine")
    assert feedback["fresh"] and feedback["on"] is True
    assert feedback["power_w"] == 1842.0


def test_feedback_hold_while_connected_keeps_retained_value_fresh():
    """Retained/change-only-Sensor (Shelly): bei konstantem Wert kommen keine
    neuen Nachrichten. Mit feedback_hold_while_connected gilt der zuletzt
    empfangene Wert als frisch, solange die MQTT-Verbindung steht - ohne das
    Flag veraltet er nach feedback_max_age_minutes."""
    import pandas as pd
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", volume_l=7000,
        feedback_required=True, feedback_max_age_minutes=20,
        feedback_hold_while_connected=True,
        stages=[LoadStage("WP", 650, 4000, feedback_topic="pool/wp/on",
                          power_topic="pool/wp/power",
                          feedback_on_threshold_w=50)])]
    pub = HomeyMqttPublisher(cfg)

    class _Client:
        def __init__(self, up):
            self._up = up

        def is_connected(self):
            return self._up

    pub._on_message(None, None, Msg("pool/wp/power", "0"))     # WP aus, 0 W
    old = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=40)
    pub.load_feedback["Pool/WP"]["power_updated"] = old        # künstlich altern

    # verbunden -> gehalten, gilt weiter als frisch
    pub._client = _Client(True)
    fb = pub.get_load_feedback("Pool/WP", 20, hold_while_connected=True)
    assert fb["held"] is True and fb["fresh"] is True and fb["on"] is False

    # getrennt -> kein Halten, veraltet
    pub._client = _Client(False)
    fb = pub.get_load_feedback("Pool/WP", 20, hold_while_connected=True)
    assert fb["held"] is False and fb["fresh"] is False

    # ohne Flag -> veraltet trotz Verbindung (bisheriges Verhalten)
    pub._client = _Client(True)
    fb = pub.get_load_feedback("Pool/WP", 20, hold_while_connected=False)
    assert fb["held"] is False and fb["fresh"] is False


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


def test_thermostat_permission_keeps_on_only_above_the_heating_limit():
    """WP mit eigenem Thermostat: die Freigabe darf nur gehalten werden, wo sie
    folgenlos ist - also ab max_c, der Temperatur, bei der das Geraet selbst
    abschaltet, und ohne reale Leistungsmeldung."""
    from ems.config import ControllableLoad, LoadStage
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, volume_l=8000, target_c=28.0,
        min_c=27.0, max_c=29.0,
        thermostat=True, temp_signal="homie/pool/temp",
        stages=[LoadStage("klein", 400, 3000)])]
    pub = HomeyMqttPublisher(cfg)
    lane = pub._load_lanes()[0]
    assert lane["thermostat"] is True and lane["max_c"] == 29.0

    # Ab der Heizgrenze haelt das Geraet selbst aus -> Freigabe AN (Anti-Takt).
    pub.load_temps["homie/pool/temp"] = 29.2
    assert pub._lane_command(lane, planned_on=False) == 1
    # Darunter wuerde es heizen -> aktiv AUS, auch oberhalb von target_c.
    pub.load_temps["homie/pool/temp"] = 28.5
    assert pub._lane_command(lane, planned_on=False) == 0
    pub.load_temps["homie/pool/temp"] = 27.0
    assert pub._lane_command(lane, planned_on=False) == 0
    # Heiz-Slot geplant -> immer AN.
    assert pub._lane_command(lane, planned_on=True) == 1
    # Keine Ist-Temperatur empfangen -> konservativ dem Plan folgen.
    pub.load_temps.clear()
    assert pub._lane_command(lane, planned_on=False) == 0


def test_measured_power_overrides_the_assumption():
    """Regression (Akku 97 %->5 % ueber Nacht): die Freigabe blieb AN, weil die
    Temperatur ueber der angenommenen Abschaltschwelle lag - das Geraet hielt
    aber NICHT aus und heizte 14 h aus dem Akku.

    Die Schwelle ist jetzt max_c und muss dem Geraet entsprechen. Sollte sie
    trotzdem falsch stehen, faengt die MESSUNG den Fall: meldet die Stufe
    Leistung, waehrend nichts geplant ist, wird abgeschaltet - unabhaengig von
    jeder Annahme ueber das Geraet.
    """
    from ems.config import ControllableLoad, LoadStage
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, volume_l=8000,
        target_c=28.0, min_c=27.0, max_c=28.0,
        thermostat=True, temp_signal="homie/pool/temp",
        stages=[LoadStage("klein", 400, 3000)])]
    pub = HomeyMqttPublisher(cfg)
    lane = pub._load_lanes()[0]
    pub.load_temps["homie/pool/temp"] = 28.4          # ueber der Heizgrenze

    pub.load_feedback[lane["label"]] = {
        "on": True, "power_w": 663.0, "threshold_w": 50.0,
        "power_updated": pd.Timestamp.now(tz="UTC"),
        "state_updated": pd.Timestamp.now(tz="UTC")}
    assert pub._lane_command(lane, planned_on=False) == 0
    # Meldet sie AUS, bleibt die Freigabe erhalten (Anti-Takt-Zweck).
    pub.load_feedback[lane["label"]]["on"] = False
    pub.load_feedback[lane["label"]]["power_w"] = 0.0
    assert pub._lane_command(lane, planned_on=False) == 1


def test_release_threshold_is_max_c_not_target_c():
    """Massgeblich ist max_c, nicht target_c.

    Die reale Pool-WP heizt bis 28,5 °C und startet erst bei 27,0 °C wieder.
    Zwischen target_c und dem Abschaltpunkt heizt sie also weiter - dort darf
    die Freigabe NICHT gehalten werden; genau dieser Bereich lief eine Nacht
    durch. max_c traegt jetzt diesen Abschaltpunkt, ein zweiter Parameter
    dafuer entfaellt.
    """
    from ems.config import ControllableLoad, LoadStage
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, volume_l=8000,
        target_c=28.0, min_c=26.0, max_c=28.5,
        thermostat=True, temp_signal="homie/pool/temp",
        stages=[LoadStage("klein", 400, 3000)])]
    pub = HomeyMqttPublisher(cfg)
    lane = pub._load_lanes()[0]
    assert lane["max_c"] == 28.5
    assert "cutoff_c" not in lane, "der zweite Parameter ist entfallen"

    # 28,2 °C: ueber target_c, aber UNTER der Heizgrenze -> die WP wuerde
    # weiterheizen -> Freigabe aus (der reale Nachtfall).
    pub.load_temps["homie/pool/temp"] = 28.2
    assert pub._lane_command(lane, planned_on=False) == 0
    # 28,6 °C: Geraet hat selbst abgeschaltet -> Freigabe darf stehen bleiben.
    pub.load_temps["homie/pool/temp"] = 28.6
    assert pub._lane_command(lane, planned_on=False) == 1


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


class _Info:
    def wait_for_publish(self, timeout=None):
        return None


class _PahoClient:
    """Nimmt auf, was der Publisher am Broker taete. loop_start() ruft
    on_connect wie der Netzwerk-Thread von paho nach dem CONNACK."""

    def __init__(self, *args, **kwargs):
        self.will = None
        self.published = []
        self.subscribed = []
        self.connected = False

    def username_pw_set(self, *args):
        return None

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def connect(self, host, port, keepalive=60):
        self.connected = True

    def loop_start(self):
        self.on_connect(self, None, None, 0, None)

    def loop_stop(self):
        return None

    def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        return _Info()

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)


class _Sock:
    def close(self):
        return None


def _connected(monkeypatch, service: bool):
    import socket

    import paho.mqtt.client as mqtt
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Sock())
    monkeypatch.setattr(mqtt, "Client", _PahoClient)
    cfg, pub = _pub()
    cfg.mqtt.enabled = True
    if service:
        pub.start()
    else:
        pub._ensure_connected()                 # wie run_once im Einzellauf
    return f"{cfg.mqtt.base_topic}/status", pub, pub._client


def _status_msgs(client, topic):
    return [(p, r) for t, p, r in client.published if t == topic]


def test_service_sets_online_on_every_connect_including_silent_reconnect(monkeypatch):
    """paho verbindet nach einem Verbindungsverlust im Hintergrund neu; der
    Broker hat dann den Last Will "offline" verteilt. Wurde "online" nur beim
    ersten Connect gesetzt, blieb ems/status danach falsch stehen (23.10.2026)."""
    topic, pub, client = _connected(monkeypatch, service=True)
    assert client.will == (topic, "offline", True)
    assert _status_msgs(client, topic) == [("online", True)]
    assert topic in client.subscribed

    client.published.clear()
    pub._on_connect(client, None, None, 0, None)      # stiller Reconnect
    assert _status_msgs(client, topic) == [("online", True)]


def test_one_shot_run_leaves_the_service_status_alone(monkeypatch):
    """Ein Einzellauf neben dem Dienst setzte beim Beenden "offline"."""
    topic, pub, client = _connected(monkeypatch, service=False)
    assert client.will is None
    assert topic not in client.subscribed

    pub._on_message(client, None, Msg(topic, "offline"))
    pub.close()
    assert _status_msgs(client, topic) == []


def test_service_overrides_foreign_offline_but_not_its_own_shutdown(monkeypatch):
    topic, pub, client = _connected(monkeypatch, service=True)
    client.published.clear()

    pub._on_message(client, None, Msg(topic, "online"))      # eigenes Echo
    assert _status_msgs(client, topic) == []
    pub._on_message(client, None, Msg(topic, "offline"))     # fremder Prozess
    assert _status_msgs(client, topic) == [("online", True)]

    client.published.clear()
    pub.close()
    # Das eigene "offline" kommt ueber das Abo zurueck - darf nicht geheilt werden.
    pub._on_message(client, None, Msg(topic, "offline"))
    assert _status_msgs(client, topic) == [("offline", True)]


# --------------------------------------------------------------------------- #
# Ist-Temperatur: nur endliche, plausible und frische Werte halten die Freigabe
# --------------------------------------------------------------------------- #
def _thermostat_pool(**extra):
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, volume_l=8000,
        target_c=28.0, min_c=27.0, max_c=28.5,
        thermostat=True, temp_signal="homie/pool/temp",
        stages=[LoadStage("klein", 400, 3000)], **extra)]
    pub = HomeyMqttPublisher(cfg)
    return pub, pub._load_lanes()[0]


def test_non_finite_temperature_never_holds_the_release():
    """float("nan") und float("inf") gelingen - und ``nan < max_c`` ist False.
    Ein Sensor, der "nan" meldet, hielt die Heiz-Freigabe in ungeplanten Slots
    dauerhaft auf 1."""
    import pytest
    for payload in ("nan", "NaN", "inf", "-inf", "1e999", "500", "-80"):
        pub, lane = _thermostat_pool()
        pub._on_message(None, None, Msg("homie/pool/temp", payload))
        assert "homie/pool/temp" not in pub.load_temps, payload
        assert pub._lane_command(lane, planned_on=False) == 0, payload
    # ein gueltiger Wert bleibt bei nachfolgendem Unsinn stehen ...
    pub, lane = _thermostat_pool()
    pub._on_message(None, None, Msg("homie/pool/temp", "28,9"))
    pub._on_message(None, None, Msg("homie/pool/temp", "nan"))
    assert pub.load_temps["homie/pool/temp"] == pytest.approx(28.9)
    assert pub._lane_command(lane, planned_on=False) == 1
    # ... und auch ein direkt gesetzter Unsinnswert haelt nichts
    pub.load_temps["homie/pool/temp"] = float("nan")
    assert pub.get_load_temp("homie/pool/temp") is None
    assert pub._lane_command(lane, planned_on=False) == 0


def test_silent_sensor_stops_holding_the_release(caplog):
    """Verstummt der Sensor mit einem letzten Wert ueber max_c, blieb die
    Freigabe unbegrenzt stehen - das Geraet heizte, sobald sein Thermostat es
    wollte, obwohl der Plan 0 W vorsah. Nach temp_max_age_minutes (Vorgabe 45)
    gilt der Wert wie "keine Temperatur": das Signal folgt dem Plan."""
    import logging
    pub, lane = _thermostat_pool()
    assert lane["temp_max_age_minutes"] == 45.0
    topic = "homie/pool/temp"
    pub._on_message(None, None, Msg(topic, "29.0"))
    assert pub._lane_command(lane, planned_on=False) == 1

    pub.load_temps.empfangen[topic] -= 44 * 60          # 44 min alt: gilt noch
    assert pub._lane_command(lane, planned_on=False) == 1
    pub.load_temps.empfangen[topic] -= 2 * 60           # 46 min alt
    with caplog.at_level(logging.WARNING, logger="ems.mqtt"):
        assert pub._lane_command(lane, planned_on=False) == 0
        assert pub._lane_command(lane, planned_on=False) == 0
    meldungen = [r for r in caplog.records if "Ist-Temperatur" in r.getMessage()]
    assert len(meldungen) == 1, "einmal melden, nicht in jedem Zyklus"
    assert pub._lane_command(lane, planned_on=True) == 1   # Plan gilt weiter

    # Andere Leser sehen dieselbe Struktur und den letzten bekannten Wert.
    assert isinstance(pub.load_temps, dict)
    assert pub.load_temps[topic] == 29.0
    assert pub.get_load_temp(topic) == 29.0
    assert pub.get_load_temp(topic, max_age_minutes=45) is None
    assert pub.get_load_temp_age_seconds(topic) > 45 * 60

    # Ein neuer Wert stellt die Freigabe wieder her.
    pub._on_message(None, None, Msg(topic, "29.1"))
    assert pub._lane_command(lane, planned_on=False) == 1


def test_temperature_age_limit_is_configurable():
    """0 = keine Altersgrenze (Sensoren, die nur bei Aenderung senden)."""
    topic = "homie/pool/temp"
    pub, lane = _thermostat_pool(temp_max_age_minutes=0.0)
    pub._on_message(None, None, Msg(topic, "29.0"))
    pub.load_temps.empfangen[topic] -= 24 * 3600
    assert pub._lane_command(lane, planned_on=False) == 1

    pub, lane = _thermostat_pool(temp_max_age_minutes=10.0)
    pub._on_message(None, None, Msg(topic, "29.0"))
    pub.load_temps.empfangen[topic] -= 11 * 60
    assert pub._lane_command(lane, planned_on=False) == 0
