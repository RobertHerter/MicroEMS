from types import SimpleNamespace

from ems.config import ControllableLoad
import pandas as pd

from ems.dashboard import (_control_banner, _controls_block, _decision_block,
                           _events_block, _forecast_quality_block, _live_block,
                           _mobile_plot_block, _runtime_block,
                           _slot_detail_block)


def _config(seconds=5.0):
    return SimpleNamespace(
        dashboard=SimpleNamespace(live_refresh_seconds=seconds))


def test_live_block_contains_all_e3dc_values_and_five_second_poll():
    html = _live_block(_config(5.0))

    for element_id in ("live-pv", "live-house", "live-grid", "live-battery",
                       "live-soc", "live-wallbox"):
        assert f'id="{element_id}"' in html
    for element_id in ("live-pv-forecast-today", "live-pv-yield-today",
                       "live-pv-forecast-now", "live-pv-deviation-today",
                       "live-grid-import-today", "live-grid-export-today",
                       "live-battery-charge-today",
                       "live-battery-discharge-today", "live-house-today",
                       "live-energy-balance", "live-price-now"):
        assert f'id="{element_id}"' in html
    assert 'id="live-daily-panel" open' in html
    assert "ems-live-daily-open" in html
    assert "daily_energy_updated" in html
    assert "e3dc_control_enabled" in html
    assert (html.index('id="live-pv-yield-today"')
            < html.index('id="live-house-today"')
            < html.index('id="live-pv-forecast-now"')
            < html.index('id="live-pv-deviation-today"')
            < html.index('id="live-energy-balance"'))
    assert (html.index('id="live-price-now"')
            < html.index('id="live-daily-panel"'))
    assert "api/live.json" in html
    assert "setInterval(tick,5000)" in html
    assert "flow-import" in html and "flow-export" in html
    assert "flow-charge" in html and "flow-discharge" in html
    assert 'id="mobile-live-strip"' not in html
    assert "mlive-pv" not in html


def test_live_block_can_be_disabled():
    assert _live_block(_config(0.0)) == ""


def test_forecast_quality_shows_issue_time():
    html = _forecast_quality_block([{
        "name": "PV", "state": "aktuell", "level": "current",
        "detail": "Solcast", "issued_at": "2026-07-21T08:05:00+00:00",
    }], "Europe/Berlin")
    assert "erstellt 21.07. 10:05" in html


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


def test_decision_block_shows_empty_plan_state():
    idx = pd.date_range("2026-07-17 12:00", periods=2, freq="15min",
                        tz="Europe/Berlin")
    table = pd.DataFrame({
        "mode": ["auto", "auto"],
        "decision_reason": ["", ""],
    }, index=idx)
    html = _decision_block(table, idx[0])
    assert "Planentscheidungen erklärt" in html
    assert "Keine besonderen Akku-Eingriffe" in html
    assert "<details class='decisions'>" in html
    assert "<details class='decisions' open" not in html


def test_control_failure_has_prominent_dashboard_alarm():
    html = _control_banner({"ok": False, "message": "Limit nicht übernommen"})
    assert "E3DC-Steuer-Ausfall" in html
    assert "Limit nicht übernommen" in html
    assert _control_banner({"ok": True, "message": "bestätigt"}) == ""


def test_forecast_quality_block_renders_all_quality_states():
    html = _forecast_quality_block([
        {"name": "Hauslast", "level": "current", "state": "aktuell",
         "detail": "192 von 192 Slots aus der Prognosequelle"},
        {"name": "PV", "level": "partial", "state": "teilweise ergänzt",
         "detail": "1 von 192 Slots durch 0 W"},
        {"name": "Strompreis", "level": "replaced",
         "state": "vollständig ersetzt", "detail": "alle Slots ersetzt"},
    ])
    assert "Prognosequalität" in html
    # Panel trägt die Statusfarbe des schlechtesten Items (hier replaced) -
    # auch im eingeklappten Zustand sichtbar.
    assert html.startswith("<details class='forecast-quality lvl-replaced'>")
    assert " open>" not in html
    assert "aktuell" in html and "teilweise ergänzt" in html
    assert "vollständig ersetzt" in html
    assert "quality-item current" in html and "quality-item partial" in html


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
    assert "Plan vor Übernahme vergleichen" in html
    assert "api/control/compare" in html
    assert "Modus übernehmen" in html
    assert "Direkte E3/DC-Steuerung (RSCP)" in html
    assert "MQTT-Sollwerte bleiben davon unabhängig" in html
    assert "MQTT-Sollwerte · deaktiviert" in html
    assert "api/control/e3dc_control" in html
    assert "id='compare-chart'" in html
    assert "candidate_battery_w" in html and "candidate_soc_percent" in html


def test_runtime_slot_details_and_event_panels_are_dynamic_and_collapsed():
    runtime = _runtime_block(True)
    assert "Plan neu berechnen" in runtime
    assert "api/control/recalc" in runtime
    assert "api/status.json" in runtime

    details = _slot_detail_block()
    assert "api/data.json" in details and "plotly_click" in details
    assert '<details class="info-panel slot-detail"' in details
    assert '<details class="info-panel slot-detail" open' not in details

    events = _events_block()
    assert "api/events.json" in events
    assert '<details class="info-panel events-panel"' in events
    assert '<details class="info-panel events-panel" open' not in events
