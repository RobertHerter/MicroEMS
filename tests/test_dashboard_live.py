from types import SimpleNamespace

from ems.config import ControllableLoad
import pandas as pd

from ems.dashboard import (_control_banner, _controls_block, _decision_block,
                           _events_block, _forecast_analysis_block,
                           _forecast_quality_block, _live_block,
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
    assert 'data-hours="all"' in html               # "Alles"-Button
    # Die Schreibtisch-Zeitraumleiste steckt NICHT mehr im Mobilblock: sie
    # steht jetzt ueber der Grafik, also frueher in der Seite.
    assert 'class="desktop-horizon-toolbar"' not in html
    assert "desktopRange" in html and "Plotly.relayout" in html
    assert '"2026-07-17T00:00:00+02:00"' in html    # lokale 00:00, nicht UTC-Vortag
    assert "toISOString()" not in html
    assert "displayModeBar:false" in html
    assert "ems-mobile-hours" in html
    assert "ems-desktop-hours" in html
    assert "mobileView()?'24':'all'" in html
    assert "hoverlabel:" in html
    assert "'#202b36':'#ffffff'" in html


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
    # Eingeklapptes Panel mit stabiler id (Sprungleiste + gemerkter Zustand).
    assert html.startswith("<details class='decisions' id='decisions-panel'>")
    assert " open>" not in html and " open " not in html


def test_operations_block_shows_which_slot_was_checked():
    """Die bestätigte Ausführungsprüfung hängt ~1 h nach (E3DC-Zählerenergie).
    Ohne Zeitbezug wirkt eine gelbe Betriebsdiagnose wie ein AKTUELLES Problem,
    obwohl sie einen länger vergangenen Slot bewertet."""
    from ems.dashboard import _operations_block
    execution = {
        "ok": False, "state": "device_error", "cause": "device",
        "message": "Geräteabweichung: Akku 848 W statt geplant 4.402 W.",
        "planned": {"issued_at": "2026-07-28T06:45:00+00:00", "grid_w": -230.8,
                    "battery_w": 4401.5, "soc": 7.0},
        "actual": {"grid_w": -3839.0, "battery_w": 848.0, "soc": 7.0},
        "deviations": {"battery_energy_kwh": -0.888},
    }
    html = _operations_block({"seconds": 4.0, "polish_seconds": 0.4},
                             execution, "Europe/Berlin")
    assert "Solver 4.0 s" in html
    assert "Geräteabweichung: Akku 848 W statt geplant 4.402 W." in html
    assert "Slot 28.07. 08:45" in html          # UTC 06:45 -> lokal 08:45
    assert "Geräteabweichung: Akku 848 W" in html
    assert "quality-item partial" in html       # gelb, nicht rot
    # Ohne Zeitstempel bleibt das Panel funktionsfähig.
    execution["planned"].pop("issued_at")
    assert "Slot " not in _operations_block({}, execution, "Europe/Berlin")


def test_operations_block_classifies_model_and_plan_warnings():
    from ems.dashboard import _operations_block

    html = _operations_block(
        {}, None, "Europe/Berlin",
        diagnostics={
            "soc_drift": {
                "mae_pp": 9.2, "threshold_pp": 8.0,
                "window_hours": 12, "alert": True,
            },
            "efficiency": {
                "measured": 0.79, "model": 0.93,
                "deviation_percent": -15.1, "windows": 8, "hours": 20,
                "threshold_percent": 6.0, "alert": True,
            },
            "execution_bias": {
                "median_w": -75, "kwh_per_day": -1.8, "n": 400,
                "window_days": 7, "threshold_w": 50, "alert": True,
            },
        },
        plan_status={
            "status": "Infeasible", "infeasible": True,
            "infeasible_reason": "Akku-Mindest-SoC widersprüchlich",
            "car_target_shortfall_wh": 2300,
            "grid_overload_wh": 1400,
        })

    assert "SoC-Prognose ↔ Ist" in html and "MAE 9.2 Prozentpunkte" in html
    assert "Entladewirkungsgrad" in html and "gemessen 0.790" in html
    assert "Ausführungs-Bias" in html and "Median -75 W" in html
    assert "Planlösbarkeit" in html and "Akku-Mindest-SoC" in html
    assert "Fahrzeug-Ziel" in html and "2.3 kWh fehlen" in html
    assert "Hausanschluss-Grenze" in html and "1.4 kWh Überschreitung" in html
    assert "3 Modellwarnungen" in html and "3 Planziele auffällig" in html
    assert '<span class="an-dot bad">' in html


def test_report_block_offers_history_selection():
    """Debug-Panel: Auswahl älterer Läufe (Verlauf) + Download mit ?ts=."""
    from ems.dashboard import _report_block
    cfg = SimpleNamespace(report=SimpleNamespace(enabled=True, mail_to="x@y.de"))
    html = _report_block(cfg, pd.Timestamp("2026-07-27 08:00"), [])
    assert 'id="ems-report-pick"' in html
    assert "api/debug-snapshots.json" in html
    assert "report.json" in html and "?ts=" in html
    # deaktiviert -> leer
    off = SimpleNamespace(report=SimpleNamespace(enabled=False, mail_to=""))
    assert _report_block(off, pd.Timestamp("2026-07-27 08:00"), []) == ""


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
    # Einheitlicher .info-panel-Look; die Statusfarbe des schlechtesten Items
    # (hier replaced -> bad) steckt im Ampelpunkt der Kopfzeile.
    assert html.startswith('<details class="info-panel" id="fquality-panel">')
    assert '<span class="an-dot bad">' in html
    assert " open>" not in html
    assert "aktuell" in html and "teilweise ergänzt" in html
    assert "vollständig ersetzt" in html
    assert "quality-item current" in html and "quality-item partial" in html


def test_forecast_analysis_shows_rolling_load_bias():
    html = _forecast_analysis_block([{
        "name": "Hauslast", "level": "current", "state": "aktuell",
        "detail": "192 von 192 Slots",
    }], load_bias={
        "alert": True, "alert_scope": "Nacht", "night_median_w": 358.6,
        "median_w": 74.6, "threshold_w": 100.0, "window_days": 7, "n": 718,
    })

    assert "Lastprognose-Bias" in html
    assert "Nacht +359 W · Gesamt +75 W" in html
    assert "718 Paare · 7-Tage-Fenster" in html
    assert "historische Tagesstart-Prognosen" in html
    assert "Last-Bias +359 W" in html
    assert '<span class="an-dot warn">' in html


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

    assert "<details class='controls info-panel'" in html
    assert "<span class='an-dot warn'></span>⏻ Steuerung" in html
    assert "<small>E3/DC aus · Automatisch</small>" in html
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
    assert "Spät laden" in html
    assert "maximalen Ziel-SoC möglichst spät erreichen" in html
    assert "Automatischer Modusvergleich" in html
    assert "Drei Ladestrategien mit Karten und Kurven" in html
    assert "aktuelle Auto-Auswahl ist markiert" in html
    assert "Aktuellen Mehrfachvergleich starten" not in html
    assert "Alle Modi berechnen" not in html
    assert "mode-compare-grid" in html
    assert "Eingriffe</span>" in html and "intervention_slots" in html
    assert "filter(([k])=>k!=='auto')" in html
    # PV-Prognose als Kontext-Kurve im Modusvergleich-Chart
    assert "PV-Prognose" in html and "s.pv_w" in html
    assert "100 % sehr wahrscheinlich" not in html  # kommt dynamisch aus dem Laufstatus
    assert "emsLateBadge" in html
    assert "api/control/compare" not in html
    assert "api/mode-comparison.json" in html
    assert "Empfehlung übernehmen" in html
    assert "Direkte E3/DC-Steuerung (RSCP)" in html
    assert "MQTT-Sollwerte bleiben davon unabhängig" in html
    assert "MQTT-Sollwerte · deaktiviert" in html
    assert "api/control/e3dc_control" in html
    assert "id='shadow-chart'" in html
    assert "v.battery_w" in html and "v.soc_percent" in html
    assert "hoverlabel:{bgcolor:dark?'#202b36':'#ffffff'" in html
    assert 'onclick="emsMode(\\\'' in html
    assert (html.index("window.addEventListener('ems-status',e=>emsShadowRender")
            < html.rindex("if(window.emsRuntimePoll)window.emsRuntimePoll();"))


def test_runtime_slot_details_and_event_panels_are_dynamic_and_collapsed():
    runtime = _runtime_block(True)
    assert "Plan neu berechnen" in runtime
    assert "api/control/recalc" in runtime
    assert "api/status.json" in runtime
    assert "cycle_watchdog" in runtime and "EMS-Zyklus überfällig" in runtime
    # P3#4: Auto-Reload nur bei neuem Plan (Sequenz erhöht) und nicht während
    # einer Eingabe – sonst würde ein turnusmäßiger Recalc Eingaben verwerfen.
    assert "advanced" in runtime and "editing" in runtime

    details = _slot_detail_block()
    assert "api/data.json" in details and "plotly_click" in details
    assert "pointerup" in details and "axis.p2d" in details
    assert "ems-plot-ready" in details
    # Der Mobil-Plot (<div id="mobile-plot">) bekommt von Plotly.react NUR
    # '.js-plotly-plot' - ohne diesen Selektor blieben die Slot-Details mobil tot.
    assert ".js-plotly-plot" in details
    # Touch: das 'x unified'-Hover-Panel muss nach dem Tap wieder verschwinden
    # (kein mouseleave auf Touchscreens) - zeitversetzt und beim Tippen daneben.
    assert "Plotly.Fx.unhover" in details
    assert "autoUnhover" in details and "unhoverAll" in details
    # Weder Heranscrollen noch Auto-Aufklappen (beides riss die Ansicht auf
    # Handy UND Desktop aus dem Kontext). Der Panel-Inhalt wird trotzdem
    # aktualisiert - der Nutzer klappt selbst auf, wenn er ihn sehen will.
    assert "scrollIntoView" not in details
    assert "panel.open" not in details
    assert "slot-detail-body" in details
    assert 'id="slot-detail-dot"' in details
    assert 'id="slot-detail-summary"' in details
    assert "className='an-dot ok'" in details
    assert "Detaildaten nicht verfügbar" in details
    assert "Hauslast Ist / Soll / Δ" in details
    assert "PV Ist / Soll / Δ" in details
    assert "Akku Ist / Soll / Δ" in details
    assert "battery_deviation_w" in details
    assert "maximumFractionDigits:d" in details
    assert '<details class="info-panel slot-detail"' in details
    assert '<details class="info-panel slot-detail" open' not in details

    events = _events_block()
    assert "api/events.json" in events
    assert 'id="events-dot"' in events
    assert 'id="events-summary"' in events
    assert "err+' Fehler · '+warn+' Warnungen · '+LAST.length+' Einträge'" in events
    assert '<details class="info-panel events-panel"' in events
    assert '<details class="info-panel events-panel" open' not in events
    # Ereignisart (kind) fließt in die CSS-Klasse -> Warnungen/Schaltvorgänge
    # werden im Panel eingefärbt.
    assert "+esc(e.kind)+" in events
    # Filter-Buttons info/warning/error + Persistenz
    assert 'data-lvl="info"' in events and 'data-lvl="warning"' in events
    assert 'data-lvl="error"' in events
    assert "ems-event-filter" in events
    # Beim Aufklappen/Rendern nach oben (neueste) scrollen, nicht ans Ende
    assert "scrollTop=0" in events
    # Level-Icon vor jedem Eintrag (info/warnung/fehler)
    assert "icon(lvlClass(e.level))" in events


def test_analysis_block_contains_decisions_savings_and_battery_only():
    """Prognosegüte liegt im Prognosepanel und wird hier nicht dupliziert."""
    from ems.dashboard import _analysis_block
    an = _analysis_block()
    assert 'id="analysis-panel"' in an
    assert "api/forecast-accuracy.json" not in an
    assert "api/savings-history.json" in an
    assert "api/battery-health.json" in an
    assert "api/plan-value.json" in an and 'id="an-pvalue"' in an
    assert 'class="tiles"' in an and "toggle" in an   # Kachel-Look, lazy
    assert 'id="an-spark"' in an and 'id="an-drivers"' in an  # Sparkline + Treiber
    assert 'id="an-facc-trend"' not in an
    # Server-seitige Titel-Kennzahl + Ampel (eingeklappt sichtbar)
    with_head = _analysis_block({"status": "warn", "text": "Ersparnis 12,00 € · 3 Tage"})
    assert '<span class="an-dot warn">' in with_head
    assert "Ersparnis 12,00 € · 3 Tage" in with_head


def test_forecast_analysis_block_is_lazy_and_interactive():
    from ems.dashboard import _forecast_analysis_block

    html = _forecast_analysis_block([{
        "name": "PV", "level": "current", "state": "aktuell",
        "detail": "Solcast vollständig",
        "issued_at": "2026-07-29T08:00:00+00:00"}])
    assert 'id="forecast-analysis-panel"' in html
    assert "Prognosen &amp; Qualität" in html
    assert '<span class="an-dot ok"></span>' in html
    assert "1/1 Quellen aktuell" in html
    assert "Aktueller Datenstatus" in html and "Solcast vollständig" in html
    assert "api/forecast-accuracy.json" in html
    assert "PV-Nowcast Nutzen" in html and "ohne Nowcast besser" in html
    assert 'id="fa-accuracy-trend"' in html and "trendSvg" in html
    assert "api/forecast-analysis.json" in html
    assert 'id="fa-calibration"' in html and "Kalibrierungsreife" in html
    assert 'id="fa-day-comparison"' in html and "Tagesverlauf" in html
    assert 'id="fa-day-loading"' in html
    assert "Neue Vergleichsdaten werden geladen" in html
    assert "panel.classList.toggle('loading',on)" in html
    assert "refreshBtn.textContent=on?'Lädt …':'Neu laden'" in html
    assert "todayBtn.disabled=on" in html and "day.disabled=on" in html
    assert "const own=++requestId" in html
    assert "PV Ist" in html and "Solcast" in html and "pvlib" in html
    assert "Last-Soll" in html and "dayComparison" in html
    assert 'id="fa-calibration-history"' in html
    assert 'id="fa-calibration-changes"' in html
    assert "Kalibrierungsverlauf" in html and "calibrationHistory" in html
    assert 'id="fa-heat-pv"' in html and 'id="fa-heat-load"' in html
    assert 'id="fa-vintages"' in html and 'type="date"' in html
    assert "Plotly.react" in html and "ems-theme-change" in html
    assert "hoverlabel" in html and "'#202b36'" in html
    assert "confidence_pct" in html and "calibration-bar" in html
    assert "toggle" in html


def test_whatif_block_only_with_controls_enabled():
    from types import SimpleNamespace

    from ems.dashboard import _whatif_block
    off = SimpleNamespace(dashboard=SimpleNamespace(controls_enabled=False))
    assert _whatif_block(off) == ""
    on = SimpleNamespace(dashboard=SimpleNamespace(controls_enabled=True))
    html = _whatif_block(on)
    assert "api/whatif" in html and 'id="whatif-panel"' in html and "wi-run" in html
    assert 'class="tiles"' in html and "mode-badge" in html   # Kachel-Look statt Tabelle
    assert 'id="whatif-dot"' in html and 'id="whatif-summary"' in html
    assert "className='an-dot '+(d.infeasible?'bad':'ok')" in html


def test_pv_confidence_block_renders_auto_basis():
    from ems.dashboard import _pv_confidence_block
    assert _pv_confidence_block(None) == ""
    assert _pv_confidence_block({}) == ""
    html = _pv_confidence_block({"2026-06-10": {
        "mode": "peak", "p10_kwh": 12.0, "expected_kwh": 15.0,
        "threshold_kwh": 10.0, "basis": "p10"}})
    assert "pvconf-panel" in html and "pvconf-card" in html
    assert "mode-badge peak" in html          # farbiges Modus-Badge
    assert "10.06." in html                   # Datum lesbar formatiert
    assert "robust" in html                   # Basis-Klartext
    assert '<span class="an-dot ok"></span>' in html
    assert "1/1 Tage robust · peak 1 T" in html


def test_thermal_feedback_header_summarizes_status():
    from ems.dashboard import _thermal_feedback_block

    html = _thermal_feedback_block(
        [{"label": "Pool / WP", "configured": True, "fresh": True,
          "on": False, "power_w": 0.0, "age_seconds": 3.0}],
        [{"name": "Pool", "status": "applied", "applied": {"loss": 1.0},
          "n_windows": 20, "r2": 0.82}],
    )
    assert '<span class="an-dot ok"></span>' in html
    assert "♨ Last-Rückkopplung" in html
    assert "1/1 Rückmeldungen frisch · 0 läuft · 1 Thermomodell aktiv" in html


def test_load_bias_card_never_renders_none_as_scope():
    """alert_scope ist None, wenn kein Alarm vorliegt - ein Default im get()
    greift dann NICHT (der Schluessel existiert). Die Karte zeigte "None"."""
    from ems.dashboard import _load_bias_quality_card
    card, level = _load_bias_quality_card({
        "alert": False, "median_w": 62.2, "night_median_w": 359.0,
        "alert_scope": None, "threshold_w": 100.0, "window_days": 7,
        "n": 707, "direction": "Prognose zu niedrig"})
    assert "None" not in card
    assert "Gesamt unter der Schwelle" in card
    assert level == "current"


def test_load_bias_card_states_the_direction():
    """Das Vorzeichen allein soll nicht gedeutet werden muessen - die Karte
    nennt die Richtung im Klartext (Konvention: Prognose minus Ist)."""
    from ems.dashboard import _load_bias_quality_card
    card, _ = _load_bias_quality_card({
        "alert": True, "median_w": -120.0, "night_median_w": None,
        "alert_scope": "Gesamt", "threshold_w": 100.0, "window_days": 7,
        "n": 400, "direction": "Prognose zu niedrig"})
    assert "Prognose zu niedrig" in card


def test_load_timeline_leaves_the_future_blank():
    """Ist-Zeilen duerfen die Zukunft nicht als "unbekannt" faerben.

    Rechts vom Jetzt-Marker kann es keine Istwerte geben; ein 229 Slots langes
    goldenes Band uebertoente die Soll-Blocke voellig ("Pumpen stehen auf
    unbekannt"). Zusaetzlich darf das Label-Mapping an None nicht scheitern -
    genau daran ist der Zyklus beim ersten Versuch gestorben (KeyError: None).
    """
    import pathlib

    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from ems.dashboard import build_dashboard
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        stages=[LoadStage("klein", 400, 1000,
                          power_topic="homie/pool/power")])]
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor("15min")
    index = pd.date_range(now - pd.Timedelta(hours=2), periods=32,
                          freq="15min", tz=cfg.general.timezone)
    n = len(index)
    past = index <= now
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.full(n, 800.0), "grid_import_w": np.zeros(n),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_klein_w": np.zeros(n),
        # Ist nur in der Vergangenheit - so kommt es real aus der Rueckmeldung.
        "actual_load_Pool_klein_on": np.where(past, 1.0, np.nan),
    }, index=index)
    html = pathlib.Path(
        build_dashboard(cfg, table, total_cost_ct=0.0)).read_text(
            encoding="utf-8")
    assert "load_timeline" in html
    # Zukunftszellen als null (leer), nicht als Code 3 (unbekannt).
    assert "null" in html


def test_timeline_marks_permit_held_without_heating():
    """"Freigabe steht, Plan 0 W" ist ein EIGENER Zustand (Code 4).

    Als "aus" dargestellt war er getarnt - und genau dieser Zustand hat einmal
    21 kWh ueber Nacht gekostet, weil das Geraet trotz gehaltener Freigabe lief.
    """
    import json
    import pathlib
    import re

    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from ems.dashboard import build_dashboard
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        min_c=26.0, max_c=28.5, thermostat=True,
        stages=[LoadStage("klein", 400, 1000)])]
    index = pd.date_range("2026-07-29 12:00", periods=8, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.zeros(n),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_klein_w": np.zeros(n),          # Plan heizt NICHT
        # Erste Haelfte ueber der Heizgrenze -> Freigabe wird gehalten,
        # zweite Haelfte darunter -> Freigabe muss weg (Code 0).
        "load_Pool_temp_c": np.array([30.0] * 4 + [27.0] * 4),
    }, index=index)
    out = pathlib.Path(build_dashboard(cfg, table, total_cost_ct=0.0))
    html = out.read_text(encoding="utf-8")
    assert "freigegeben, heizt nicht" in html
    match = re.search(r'Plotly\.newPlot\(\s*"[^"]+",\s*(\[.*?\]),\s*\{',
                      html, re.S)
    traces = [x for x in json.loads(match.group(1))
              if x.get("meta") == "load_timeline"]
    assert traces, "Lastenleiste fehlt"
    row = traces[0]["z"][0]                      # Pool / klein (Soll)
    assert row[:4] == [4, 4, 4, 4], row
    assert row[4:] == [0, 0, 0, 0], row


def test_learned_profile_headline_does_not_overclaim():
    """Platzhalter-Profile deaktivierter Lasten sind NICHT "in der Planung".

    Vorher zaehlte die Zeile jedes vorhandene power_profile_w und behauptete
    "4/4 Profile in der Planung", waehrend nichts gelernt war und alle vier
    Lasten deaktiviert waren.
    """
    from ems.config import ControllableLoad
    from ems.dashboard import _load_profile_block
    from tests.test_synthetic import make_config

    cfg = make_config()
    profile = [2000.0, 300.0, 150.0, 100.0]
    cfg.controllable_loads = [
        ControllableLoad(name="Trockner", type="deferrable", enabled=False,
                         power_profile_w=profile, runtime_minutes=60.0,
                         power_topic="homie/homey/strom-trockner/measure-power"),
        ControllableLoad(name="Waschmaschine 1", type="deferrable",
                         enabled=True, power_profile_w=profile,
                         runtime_minutes=60.0),
    ]
    html = _load_profile_block(cfg)
    assert "1/2 in der Planung" in html
    assert "1 im Anlernen" in html


def _timeline_row(table, cfg, row=0):
    import json
    import pathlib
    import re

    from ems.dashboard import build_dashboard
    html = pathlib.Path(
        build_dashboard(cfg, table, total_cost_ct=0.0)).read_text(
            encoding="utf-8")
    match = re.search(r'Plotly\.newPlot\(\s*"[^"]+",\s*(\[.*?\]),\s*\{',
                      html, re.S)
    traces = [x for x in json.loads(match.group(1))
              if x.get("meta") == "load_timeline"]
    assert traces, "Lastenleiste fehlt"
    return traces[0]["z"][row], traces[0]["y"], html


def test_timeline_merges_soll_and_ist_into_one_row():
    """Eine Zeile je Stufe, und die vier Soll/Ist-Kombinationen sind eigene
    Zustaende. Vorher belegte jede Stufe zwei Zeilen und der Konflikt zwischen
    Plan und Realitaet war nur durch Augenvergleich zu sehen.

    Codes: 0 aus · 1 laeuft wie geplant · 2 deaktiviert · 3 Ist unbekannt
           4 freigegeben, heizt nicht · 5 geplant, laeuft nicht
           6 laeuft ungeplant
    """
    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        min_c=26.0, max_c=32.0, thermostat=False,
        stages=[LoadStage("klein", 400, 1000,
                          power_topic="homie/pool/power")])]
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor("15min")
    index = pd.date_range(now - pd.Timedelta(hours=1), periods=8,
                          freq="15min", tz=cfg.general.timezone)
    n = len(index)
    # Vier Kombinationen in der Vergangenheit, danach Zukunft.
    plan = np.array([1000.0, 1000.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    ist = np.array([1.0, 0.0, 1.0, 0.0, np.nan, np.nan, np.nan, np.nan])
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.zeros(n),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_klein_w": plan,
        "actual_load_Pool_klein_on": ist,
    }, index=index)
    row, labels, html = _timeline_row(table, cfg)
    assert labels == ["Pool / klein"], labels        # EINE Zeile je Stufe
    assert row[0] == 1, "Soll AN + Ist laeuft -> wie geplant"
    assert row[1] == 5, "Soll AN + Ist aus -> geplant, laeuft nicht"
    assert row[2] == 6, "Soll aus + Ist laeuft -> laeuft ungeplant"
    assert row[3] == 0, "Soll aus + Ist aus -> aus"
    # Vergangenheit ohne Rueckmeldung -> unbekannt, Zukunft -> nur Soll.
    assert row[4] == 3
    assert set(row[5:]) <= {0}
    # Plotly schreibt Nicht-ASCII escaped (l\u00e4uft) - auf den ASCII-Teil
    # pruefen, sonst schlaegt der Test aus dem falschen Grund fehl.
    for text in ("geplant, l", "nicht geplant", "Soll AN"):
        assert text in html, text


def test_timeline_row_count_halves():
    """Zeilenzahl = Zahl der Stufen, nicht deren Doppel."""
    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [
        ControllableLoad(name="Pool", type="thermal", enabled=True,
                         target_c=28.0, min_c=26.0, max_c=32.0,
                         stages=[LoadStage("a", 400, 1000,
                                           power_topic="x/a"),
                                 LoadStage("b", 400, 1000,
                                           power_topic="x/b")]),
        ControllableLoad(name="Trockner", type="deferrable", enabled=False,
                         power_profile_w=[1000.0] * 4, runtime_minutes=60.0,
                         power_topic="x/t"),
    ]
    index = pd.date_range("2026-07-29 12:00", periods=4, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.zeros(n),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_a_w": np.zeros(n), "load_Pool_b_w": np.zeros(n),
        "load_Trockner_w": np.zeros(n),
    }, index=index)
    _row, labels, _html = _timeline_row(table, cfg)
    assert labels == ["Pool / a", "Pool / b", "Trockner"], labels


def test_disabled_load_still_shows_that_it_runs():
    """Deaktiviert heisst nicht unsichtbar: eine Last kann laufen, ohne dass
    wir sie steuern (Anlern-Phase, Handbetrieb). Pauschal grau zu malen hat
    genau diese Information vernichtet - der laufende Trockner war weg."""
    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Trockner", type="deferrable", enabled=False,
        power_profile_w=[1000.0] * 4, runtime_minutes=60.0,
        power_topic="homie/homey/strom-trockner/measure-power")]
    now = pd.Timestamp.now(tz=cfg.general.timezone).floor("15min")
    index = pd.date_range(now - pd.Timedelta(minutes=45), periods=6,
                          freq="15min", tz=cfg.general.timezone)
    n = len(index)
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.zeros(n),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Trockner_w": np.zeros(n),
        "actual_load_Trockner_on": np.array([1.0, 1.0, 0.0, np.nan,
                                             np.nan, np.nan]),
    }, index=index)
    row, labels, html = _timeline_row(table, cfg)
    assert labels == ["Trockner"]
    assert row[0] == 6 and row[1] == 6, "laufend, obwohl nicht gesteuert"
    assert row[2] == 2, "aus -> grau"
    assert row[3] == 3, "Vergangenheit ohne Rueckmeldung -> unbekannt"
    assert "deaktiviert, l" in html      # "läuft trotzdem", escaped


def test_dashboard_listens_only_locally_unless_asked_otherwise(tmp_path):
    """Im Arbeitsverzeichnis des Dienstes liegen config.yaml und die
    History-Datenbank, und ohne username/password laesst der Server jede
    Anfrage durch. Der Standard gehoert deshalb auf 127.0.0.1; wer das
    Dashboard im Netz oder im Container braucht, setzt 0.0.0.0 bewusst."""
    import os
    from ems.config import load_config
    beispiel = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config.example.yaml")

    # Beispielconfig: ausdruecklich lokal
    assert load_config(beispiel).dashboard.host == "127.0.0.1"

    # Config ohne dashboard.host -> Standard, nicht 0.0.0.0
    with open(beispiel, encoding="utf-8") as fh:
        text = fh.read()
    ohne = "\n".join(z for z in text.splitlines()
                     if z.strip() != 'host: "127.0.0.1"')
    ziel = tmp_path / "config.yaml"
    ziel.write_text(ohne, encoding="utf-8")
    assert load_config(str(ziel)).dashboard.host == "127.0.0.1"

    # ausdrueckliches 0.0.0.0 bleibt respektiert
    ziel.write_text(ohne.replace("  serve: true",
                                 '  serve: true\n  host: "0.0.0.0"', 1),
                    encoding="utf-8")
    assert load_config(str(ziel)).dashboard.host == "0.0.0.0"


def test_forecast_analysis_prefers_the_operative_stand():
    """Gezeigt wird der Stand, der den Akku gefuehrt hat - der Tagesstart als
    Kontext. Beide in einen Wert zu mischen war die Ursache dafuer, dass
    wochenlang ein Bias gemeldet wurde, den die laufende Fuehrung nicht hatte
    (gemessen 31.08.2026: Tagesstart +183 W, operativ +24 W)."""
    html = _forecast_analysis_block([], load_bias={
        "alert": False, "alert_scope": None,
        "operative_median_w": -14.1, "operative_night_median_w": -31.5,
        "operative_n": 187, "operative_window_days": 2.0,
        "night_median_w": 174.9, "median_w": 158.6,
        "threshold_w": 100.0, "window_days": 7, "n": 725,
    })

    assert "Nacht -32 W · Gesamt -14 W" in html
    assert "187 Paare · 2.0-Tage-Fenster" in html
    assert "Stand, der den Akku im jeweiligen Slot geführt hat" in html
    # Der Tagesstart bleibt lesbar, aber klar als Vergleich benannt.
    assert "Tagesstart-Stand zum Vergleich" in html
    assert "Nacht +175 W" in html and "Gesamt +159 W" in html
