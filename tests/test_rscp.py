"""Tests der RSCP-Anbindung mit gemocktem E3DC-Client (kein pye3dc/Hardware)."""
from __future__ import annotations

from ems.rscp import E3DCLink
from tests.test_synthetic import make_config


class FakeE3DC:
    # Struktur wie pye3dc 0.10 poll() gegen echte Hardware verifiziert:
    # Akku steht unter consumption.battery (+ = Laden), PV unter production.solar.
    def __init__(self):
        self.limits = None
        self._poll = {
            "stateOfCharge": 63.0,
            "production": {"solar": 4200.0, "add": 0.0, "grid": -1500.0},
            "consumption": {"house": 1800.0, "wallbox": 0.0, "battery": 900.0},
        }

    def poll(self):
        return self._poll

    def set_power_limits(self, enable=None, max_charge=None, max_discharge=None):
        self.limits = {"enable": enable, "max_charge": max_charge,
                       "max_discharge": max_discharge}


def _link(**rscp):
    cfg = make_config()
    for k, v in rscp.items():
        setattr(cfg.e3dc_rscp, k, v)
    link = E3DCLink(cfg)
    link._e3dc = FakeE3DC()          # echten Verbindungsaufbau umgehen
    return cfg, link


def test_read_live_mapping():
    cfg, link = _link()
    live = link.read_live()
    assert live["soc_percent"] == 63.0
    assert live["pv_w"] == 4200.0
    assert live["house_load_w"] == 1800.0
    assert live["grid_w"] == -1500.0     # grid_sign 1.0
    assert live["battery_w"] == 900.0


def test_read_live_sign_flip_and_cache():
    cfg, link = _link(grid_sign=-1.0, batt_sign=-1.0)
    live = link.read_live()
    assert live["grid_w"] == 1500.0
    assert live["battery_w"] == -900.0
    # Cache: zweiter Aufruf ohne force liefert dasselbe Objekt
    assert link.read_live() is live


def test_read_live_graceful_failure():
    cfg, link = _link()

    class Boom:
        def poll(self):
            raise RuntimeError("keine Verbindung")
    link._e3dc = Boom()
    assert link.read_live() is None


def test_control_disabled_is_noop():
    cfg, link = _link(control_enabled=False)
    assert link.apply_setpoints({"batt_charge_limit_w": 1000,
                                 "batt_discharge_limit_w": 1000}) is False
    assert link._e3dc.limits is None


def test_control_sets_limits_when_deviating():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    # Ladelimit < Hardware-Max -> Begrenzung aktiv
    ok = link.apply_setpoints({"batt_charge_limit_w": 1200,
                               "batt_discharge_limit_w": hb.max_discharge_w,
                               "batt_grid_charge_w": 0})
    assert ok and link._e3dc.limits["enable"] is True
    assert link._e3dc.limits["max_charge"] == 1200


def test_control_free_running_disables_limit():
    cfg, link = _link(control_enabled=True)
    hb = cfg.house_battery
    # beide Limits = Hardware-Max -> "frei laufen", Begrenzung deaktiviert
    link.apply_setpoints({"batt_charge_limit_w": hb.max_dc_charge_w,
                          "batt_discharge_limit_w": hb.max_discharge_w,
                          "batt_grid_charge_w": 0})
    assert link._e3dc.limits["enable"] is False
