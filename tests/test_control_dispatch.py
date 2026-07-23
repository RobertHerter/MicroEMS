"""Dashboard-Steuer-Dispatcher (_dispatch_control) und Status-JSON-Endpoints
(_status_api_payload) – beide modulweit und dependency-injected, daher ohne
HTTP-Server/Socket direkt testbar."""
from __future__ import annotations

import threading

import pytest

import ems.main as m
from ems.config import ControllableLoad, LoadStage
from tests.test_synthetic import make_config


class _Pub:
    def __init__(self):
        self.recalc_event = threading.Event()
        self.load_overrides = {}


class _E3DC:
    def __init__(self, control_status=None):
        self.control_enabled_calls = []
        self.manual_calls = []
        self._status = control_status or {"ok": True, "state": "disabled"}

    def set_control_enabled(self, enabled):
        self.control_enabled_calls.append(enabled)
        return {"enabled": enabled, "control_status": self._status}

    def manual_power(self, action, watts, seconds):
        self.manual_calls.append((action, watts, seconds))
        return {"mode": action, "watts": watts, "seconds": seconds}


def _cfg(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        stages=[LoadStage("klein", 400, 3000)])]
    return cfg


def _dispatch(action, payload, cfg, tmp_path, publisher=None, e3dc=None,
              schedule_runner=None):
    return m._dispatch_control(
        action, payload, config=cfg, publisher=publisher, e3dc=e3dc,
        config_path=str(tmp_path / "config.yaml"), schedule_runner=schedule_runner,
        find_load=lambda name: next(l for l in cfg.controllable_loads
                                    if l.name == name),
        load_params={"thermal": {"target_c": float}})


def test_recalc_sets_event_and_queues(tmp_path):
    cfg = _cfg(tmp_path)
    pub = _Pub()
    out = _dispatch("recalc", {}, cfg, tmp_path, publisher=pub)
    assert out["queued"] is True and out["runtime"]["pending_recalc"] is True
    assert pub.recalc_event.is_set()


def test_mode_sets_strategy_and_rejects_invalid(tmp_path):
    cfg = _cfg(tmp_path)
    out = _dispatch("mode", {"strategy": "peak"}, cfg, tmp_path)
    assert out == {"charge_strategy": "peak"}
    assert cfg.optimization.charge_strategy == "peak"
    with pytest.raises(ValueError):
        _dispatch("mode", {"strategy": "quatsch"}, cfg, tmp_path)


def test_e3dc_control_toggles_and_validates(tmp_path):
    cfg = _cfg(tmp_path)
    e3dc = _E3DC()
    out = _dispatch("e3dc_control", {"enabled": True}, cfg, tmp_path, e3dc=e3dc)
    assert out["enabled"] is True and e3dc.control_enabled_calls == [True]
    assert cfg.e3dc_rscp.control_enabled is True
    with pytest.raises(ValueError):                       # enabled kein bool
        _dispatch("e3dc_control", {"enabled": "ja"}, cfg, tmp_path, e3dc=e3dc)
    with pytest.raises(ValueError):                       # keine RSCP-Verbindung
        _dispatch("e3dc_control", {"enabled": True}, cfg, tmp_path, e3dc=None)


def test_load_toggle_and_param_whitelist(tmp_path):
    cfg = _cfg(tmp_path)
    pub = _Pub()
    out = _dispatch("load", {"name": "Pool", "enabled": False,
                             "params": {"target_c": 26.5}}, cfg, tmp_path,
                    publisher=pub)
    assert out["enabled"] is False and out["target_c"] == 26.5
    assert cfg.controllable_loads[0].enabled is False
    assert cfg.controllable_loads[0].target_c == 26.5
    with pytest.raises(ValueError):                       # Parameter nicht erlaubt
        _dispatch("load", {"name": "Pool", "params": {"loss_w_per_k": 400}},
                  cfg, tmp_path, publisher=pub)


def test_battery_requires_control_enabled_except_auto(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.e3dc_rscp.control_enabled = False
    e3dc = _E3DC()
    with pytest.raises(ValueError):                       # aus -> nur auto erlaubt
        _dispatch("battery", {"action": "discharge", "watts": 2000}, cfg,
                  tmp_path, e3dc=e3dc)
    out = _dispatch("battery", {"action": "auto"}, cfg, tmp_path, e3dc=e3dc)
    assert out["mode"] == "auto" and e3dc.manual_calls[-1][0] == "auto"


def test_unknown_action_raises_keyerror(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(KeyError):
        _dispatch("explode", {}, cfg, tmp_path)


def test_status_api_payload_status_and_events(tmp_path):
    from ems.local_history import write_dashboard_event
    cfg = _cfg(tmp_path)
    m._runtime_update(state="ready", pending_recalc=False)
    obj, code = m._status_api_payload("/api/status.json", cfg)
    assert code == 200 and obj["state"] == "ready" and "solver_running" in obj

    write_dashboard_event(cfg.e3dc_rscp.history_db_path, "recalc", "Test-Event")
    obj, code = m._status_api_payload("/api/events.json", cfg)
    assert code == 200 and any(e["message"] == "Test-Event" for e in obj["events"])

    assert m._status_api_payload("/index.html", cfg) is None   # kein Status-Pfad


def test_resolve_post_route_gating(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.dashboard.controls_enabled = True
    cfg.dashboard.ingest_enabled = True
    assert m._resolve_post_route("/api/control/mode", cfg) == ("control", "mode")
    assert m._resolve_post_route("/api/ingest/live", cfg) == ("ingest", "live")
    # Gating: Steuerung/Ingest deaktiviert -> 403
    cfg.dashboard.controls_enabled = False
    assert m._resolve_post_route("/api/control/mode", cfg)[:2] == ("error", 403)
    cfg.dashboard.ingest_enabled = False
    assert m._resolve_post_route("/api/ingest/live", cfg)[:2] == ("error", 403)
    # Ingest an, aber unbekannter Pfad -> 404
    cfg.dashboard.ingest_enabled = True
    assert m._resolve_post_route("/api/anderes", cfg)[:2] == ("error", 404)


def test_resolve_get_route_assets_live_status(tmp_path):
    cfg = _cfg(tmp_path)
    r = lambda p: m._resolve_get_route(p, cfg, has_schedule_runner=True)
    assert r("/manifest.webmanifest") == ("raw", "manifest")
    assert r("/app-icon.svg") == ("raw", "icon")
    assert r("/sw.js") == ("raw", "sw")
    assert r("/api/live.json") == ("live",)
    assert r("/api/status.json") == ("status", "/api/status.json")
    assert r("/version") == ("version",)
    assert r("/report.json") == ("file", "report")
    assert r("/index.html") is None                       # -> statische Datei


def test_resolve_get_route_battery_schedule_and_data_gating(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.dashboard.controls_enabled = False
    assert m._resolve_get_route("/api/battery-schedule.json", cfg,
                                has_schedule_runner=True) == (
        "json", {"status": "disabled"}, 403)
    cfg.dashboard.controls_enabled = True
    assert m._resolve_get_route("/api/battery-schedule.json", cfg,
                                has_schedule_runner=False) == (
        "json", {"status": "unavailable"}, 503)
    assert m._resolve_get_route("/api/battery-schedule.json", cfg,
                                has_schedule_runner=True) == ("schedule",)
    # api_data.json nur bei api_enabled
    cfg.dashboard.api_enabled = False
    assert m._resolve_get_route("/api/data.json", cfg,
                                has_schedule_runner=True) is None
    cfg.dashboard.api_enabled = True
    assert m._resolve_get_route("/api/data.json", cfg,
                                has_schedule_runner=True) == ("file", "data")


def test_status_api_payload_mode_comparison(tmp_path):
    cfg = _cfg(tmp_path)
    with m._runtime_lock:
        m._shadow_curves.update(generated=None, series=None)
    obj, code = m._status_api_payload("/api/mode-comparison.json", cfg)
    assert code == 503 and obj["status"] == "unavailable"
    with m._runtime_lock:
        m._shadow_curves.update(generated="2026-07-23T12:00:00+02:00",
                                series={"timestamp": [], "modes": {}})
    obj, code = m._status_api_payload("/api/mode-comparison.json", cfg)
    assert code == 200 and obj["series"] == {"timestamp": [], "modes": {}}
