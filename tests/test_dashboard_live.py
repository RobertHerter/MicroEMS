from types import SimpleNamespace

from ems.config import ControllableLoad
import pandas as pd

from ems.dashboard import _controls_block, _live_block, _mobile_plot_block


def _config(seconds=5.0):
    return SimpleNamespace(
        dashboard=SimpleNamespace(live_refresh_seconds=seconds))


def test_live_block_contains_all_e3dc_values_and_five_second_poll():
    html = _live_block(_config(5.0))

    for element_id in ("live-pv", "live-house", "live-grid", "live-battery",
                       "live-soc", "live-wallbox"):
        assert f'id="{element_id}"' in html
    assert "api/live.json" in html
    assert "setInterval(tick,5000)" in html
    assert "flow-import" in html and "flow-export" in html
    assert "flow-charge" in html and "flow-discharge" in html
    assert 'id="mobile-live-strip"' not in html
    assert "mlive-pv" not in html


def test_live_block_can_be_disabled():
    assert _live_block(_config(0.0)) == ""


def test_mobile_plot_has_panel_tabs_and_24_48_hour_switch():
    html = _mobile_plot_block(pd.Timestamp("2026-07-17T12:00:00+02:00"),
                              has_loads=True, temp_row=7)

    for panel in ("power", "soc", "price", "control", "mode", "loads",
                  "temperature"):
        assert f'data-panel="{panel}"' in html
    assert 'data-hours="24"' in html and 'data-hours="48"' in html
    assert "displayModeBar:false" in html
    assert "ems-mobile-hours" in html


def test_mobile_plot_omits_optional_tabs_without_matching_panels():
    html = _mobile_plot_block(pd.Timestamp("2026-07-17T12:00:00+02:00"),
                              has_loads=False, temp_row=None)

    assert 'data-panel="loads"' not in html
    assert 'data-panel="temperature"' not in html


def test_controls_are_collapsible_and_render_editable_power_profile():
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(controls_enabled=True),
        optimization=SimpleNamespace(charge_strategy="auto"),
        house_battery=SimpleNamespace(max_dc_charge_w=5000,
                                      max_discharge_w=5000),
        controllable_loads=[ControllableLoad(
            name="Waschmaschine", type="deferrable", enabled=True,
            power_profile_w=[2100, 300, 150], runtime_minutes=45,
            window_from_hour=8, window_to_hour=22, deadline_hours=12)],
    )

    html = _controls_block(cfg)

    assert "<details class='controls'" in html
    assert "<details class='controls' id='ems-controls' open" not in html
    assert "p_Waschmaschine_power_profile_w" in html
    assert "2100, 300, 150" in html
    assert "curve_Waschmaschine" in html
    assert "p.power_profile_w=emsProfile(sg)" in html
    assert "Akkuplanung" in html
    assert "schedule-chart" in html
    assert "api/battery-schedule.json" in html
    assert "emsScheduleAdd" in html and "emsScheduleCancel" in html
    assert "emsScheduleDelete" in html and ">Löschen</button>" in html
    assert "Jetzt starten" in html and "Auto / Stop" in html
    assert "id='bat_w'" not in html and "id='bat_min'" not in html
    assert "value='grid_charge'>Netzladen" in html
    assert "value='pv_charge'>Laden" in html
    assert "value='idle'>Idle" in html
    assert "emsScheduleAction" in html
    assert "Automatisch" in html
    assert "Frühestmöglich laden" in html
    assert "PV-Spitzen glätten" in html
