"""Erzeugt dashboard_beispiel.html mit SYNTHETISCHEN Daten (keine echten
Verbrauchs-/Preisdaten) - als Beispielausgabe fürs Repository.

Aufruf:  python beispiel_dashboard.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.config import ControllableLoad, LoadStage
from ems.dashboard import build_dashboard
from ems.optimizer import Optimizer, OptimizerInputs
from tests.test_synthetic import make_config, synthetic_history
from ems.forecast import LoadForecaster

OUT = "dashboard_beispiel.html"


def _with_demo_api(html: str, now: pd.Timestamp) -> str:
    """Live- und Heute-Kacheln mit Beispielwerten befüllen.

    Die Panels holen ihre Werte per fetch von /api/... - in der eigenständigen
    Beispieldatei gibt es keinen Server, deshalb blieben sie leer ("–"). Statt
    die Werte ins HTML zu schreiben, wird `fetch` durch eine Attrappe ersetzt:
    so läuft die ECHTE Render-Logik der Panels, das Beispiel bleibt also ein
    ehrliches Abbild. Alle Zahlen sind frei erfunden (synthetisch).
    """
    import json as _json

    live = {
        "updated": now.isoformat(),
        "pv_w": 7420.0, "house_load_w": 1310.0, "grid_w": -2870.0,
        "battery_w": 3240.0, "soc_percent": 68.0, "wallbox_w": 0.0,
        "pool_temp_c": 27.6, "outdoor_temp_c": 24.3,
        "current_price_ct_kwh": 28.4,
        "e3dc_control_enabled": True,
        # Tageszähler (Heute-Bereich)
        "daily_energy_updated": now.isoformat(),
        "pv_forecast_today_kwh": 58.7, "pv_yield_today_kwh": 41.2,
        "pv_forecast_until_now_kwh": 39.8,
        "pv_deviation_today_kwh": 1.4, "pv_deviation_today_percent": 3.5,
        "grid_import_today_kwh": 2.1, "grid_export_today_kwh": 18.6,
        "battery_charge_today_kwh": 12.4, "battery_discharge_today_kwh": 6.9,
        "house_consumption_today_kwh": 15.3,
        "energy_balance_residual_kwh": 0.08, "energy_balance_ok": True,
    }
    status = {
        "state": "ready", "phase": "Bereit", "progress": 100,
        "message": "Plan aktuell – nächster Lauf in 12 min",
        "duration_seconds": 6.2, "sequence": 1,
        "plan_generated": now.isoformat(), "pending_recalc": False,
    }
    events = {"events": [
        {"ts": (now - pd.Timedelta(minutes=3)).isoformat(), "kind": "recalc",
         "level": "info", "message": "Plan neu berechnet · Modus peak", "details": {}},
        {"ts": (now - pd.Timedelta(minutes=41)).isoformat(), "kind": "switch",
         "level": "info", "message": "Laden begrenzt auf 3.892 W", "details": {}},
        {"ts": (now - pd.Timedelta(hours=2)).isoformat(), "kind": "alarm",
         "level": "warning",
         "message": "Pool-Rückmeldung wieder aktuell: Pool/WP klein", "details": {}},
    ]}
    stub = (
        "<script>(function(){\n"
        " const DEMO={'api/live.json':" + _json.dumps(live, ensure_ascii=False)
        + ",'api/status.json':" + _json.dumps(status, ensure_ascii=False)
        + ",'api/events.json':" + _json.dumps(events, ensure_ascii=False) + "};\n"
        " const real=window.fetch;\n"
        " window.fetch=function(url,opts){\n"
        "  const key=Object.keys(DEMO).find(k=>String(url).indexOf(k)>=0);\n"
        "  if(key)return Promise.resolve({ok:true,status:200,\n"
        "    json:()=>Promise.resolve(DEMO[key]),text:()=>Promise.resolve('')});\n"
        "  return real?real.apply(window,arguments):Promise.reject(new Error('offline'));\n"
        " };\n"
        "})();</script>")
    # VOR den Panel-Skripten einhängen, damit schon der erste Abruf greift.
    marker = "</head>"
    return html.replace(marker, stub + "\n" + marker, 1)


def main() -> None:
    cfg = make_config(tmp_html=OUT)
    # Steuerbare Last (Pool-WP, wie im echten Betrieb) + interaktives
    # Steuerpanel, damit das Beispielbild die aktuellen Panels zeigt.
    cfg.controllable_loads = [
        ControllableLoad(
            name="Pool", type="thermal", enabled=True, volume_l=8000,
            target_c=28.0, min_c=27.0, max_c=29.0, loss_w_per_k=380.0,
            surface_m2=8.0, solar_absorption=0.75, thermostat=True,
            temp_signal="pool/temperature", no_grid_import=True,
            switch_penalty_ct=5.0,
            stages=[LoadStage("WP Pinguin", 650.0, 4000.0),
                    LoadStage("WP klein", 400.0, 3000.0, requires="WP Pinguin")],
        ),
        ControllableLoad(
            name="Waschmaschine", type="deferrable", enabled=False,
            power_profile_w=[2100, 300, 150, 150, 2000, 250, 200, 100],
            runtime_minutes=120.0, window_from_hour=8, window_to_hour=22,
        ),
    ]
    cfg.dashboard.controls_enabled = True
    cfg.weather.enabled = True     # zeigt die Außentemperatur-Live-Kachel
    # Debug-Report und Konfigurationseditor sind dokumentierte Funktionen -
    # im Beispielbild sollen sie deshalb sichtbar sein. Die Zugangsdaten sind
    # reine Platzhalter, nur damit editor_allowed() das Zahnrad einblendet;
    # dieses Skript erzeugt ausschliesslich synthetische Beispielausgaben.
    cfg.report.enabled = True
    cfg.dashboard.config_editor_enabled = True
    cfg.dashboard.username = "demo"
    cfg.dashboard.password = "demo"
    tz = cfg.general.timezone
    rng = np.random.default_rng(7)

    now = pd.Timestamp.now(tz=tz).floor("15min")
    day_start = now.normalize()
    opt_end = (now + pd.Timedelta(hours=48)).normalize() + pd.Timedelta(days=1)
    opt_index = pd.date_range(now, opt_end, freq="15min", tz=tz, inclusive="left")

    # --- Prognosen (synthetisch) ---
    hist = synthetic_history(cfg, now)
    load_fc = LoadForecaster(cfg).forecast(hist, day_start,
                                           len(pd.date_range(day_start, opt_end,
                                                             freq="15min",
                                                             inclusive="left")))
    full = pd.date_range(day_start, opt_end, freq="15min", tz=tz, inclusive="left")
    load_full = load_fc.reindex(full).ffill().bfill()
    hour = np.asarray(full.hour + full.minute / 60.0, dtype=float)
    pv_full = pd.Series(np.clip(7000 * np.exp(-((hour - 13) ** 2) / 7), 0, None),
                        index=full)
    price_full = pd.Series(24 + 9 * np.sin((hour - 6) / 24 * 2 * np.pi)
                           + np.where((hour >= 18) & (hour <= 21), 12, 0), index=full)
    # Außentemperatur (Tagesgang) und Solar-Einstrahlung (für den Pool-Wärme-
    # eintrag) - synthetisch, wie ein Sommertag mit ~28°C Spitze.
    ambient_full = pd.Series(18 + 10 * np.exp(-((hour - 15) ** 2) / 18), index=full)
    solar_full = pd.Series(np.clip(850 * np.exp(-((hour - 13) ** 2) / 8), 0, None),
                           index=full)

    # --- Optimierung über den Zukunftsteil ---
    inp = OptimizerInputs(
        index=opt_index,
        house_load_w=load_full.reindex(opt_index).values,
        pv_w=pv_full.reindex(opt_index).values,
        price_ct_kwh=price_full.reindex(opt_index).values,
        feedin_ct_kwh=np.full(len(opt_index), 8.0),
        initial_house_soc_wh=3500.0,
        pv10_w=0.55 * pv_full.reindex(opt_index).values,
        ambient_temp_c=ambient_full.reindex(opt_index).values,
        solar_w_m2=solar_full.reindex(opt_index).values,
        load_state={"Pool": 27.5},
    )
    res = Optimizer(cfg).solve(inp)

    # --- Anzeige-Tabelle wie in main._build_display_frame (vereinfacht) ---
    t = pd.DataFrame(index=full)
    t["house_load_w"] = load_full
    t["pv_w"] = pv_full
    t["pv10_w"] = 0.55 * pv_full
    t["pv90_w"] = 1.25 * pv_full
    t["price_ct_kwh"] = price_full
    t["price_estimated"] = (full >= now + pd.Timedelta(hours=20)).astype(float)
    for c in res.table.columns:
        t[c] = res.table[c].reindex(full)
    t["mode"] = t["mode"].fillna("auto")
    t["feedin_ct_kwh"] = t["feedin_ct_kwh"].fillna(8.0)

    # Ist-Werte (Prognose + Rauschen) bis "jetzt"
    past = full <= now
    noise = lambda s, f: s * (1 + rng.normal(0, f, len(s)))
    t.loc[past, "actual_load_w"] = noise(load_full[past], 0.10)
    t.loc[past, "actual_pv_w"] = np.clip(noise(pv_full[past], 0.06), 0, None)
    soc0 = pd.Series(np.nan, index=full)
    soc0.iloc[0] = 45.0
    drift = (t["actual_pv_w"] - t["actual_load_w"]).fillna(0) / cfg.house_battery.capacity_wh * 25
    t["actual_soc_percent"] = (soc0.fillna(0) + drift.cumsum()).clip(10, 100).where(past)
    t.loc[past, "actual_grid_w"] = (t.loc[past, "actual_load_w"]
                                    - t.loc[past, "actual_pv_w"]).clip(lower=-6000)
    t.loc[past, "actual_battery_w"] = (t.loc[past, "actual_pv_w"]
                                       - t.loc[past, "actual_load_w"]).clip(-4000, 6000)

    # Pool-Ist-Temperatur (Vergangenheit): folgt dem geplanten Verlauf mit
    # etwas Rauschen, damit "Pool erwartet" vs. "Pool echt" sich unterscheiden.
    load_temp_actual = {}
    if "load_Pool_temp_c" in t.columns and t.loc[past, "load_Pool_temp_c"].notna().any():
        planned = t.loc[past, "load_Pool_temp_c"]
        load_temp_actual["Pool"] = (planned + rng.normal(0, 0.15, len(planned))).dropna()

    # Diagnose-Panels brauchen Statusdaten, sonst rendern sie gar nicht - das
    # Beispielbild soll aber den im README beschriebenen Umfang zeigen. Alle
    # Werte sind synthetisch.
    solver_status = {
        "seconds": 6.2, "polish_seconds": 0.4, "slow": False,
        "median_seconds": 5.8, "variables": 6353, "binaries": 2126,
        "constraints": 10023, "warm_start": True, "mip_gap": 0.0,
    }
    execution_status = {
        "ok": True, "state": "ok", "cause": "none", "message": "Soll erfüllt.",
        "planned": {"issued_at": (now - pd.Timedelta(minutes=15)).isoformat(),
                    "grid_w": -2870.0, "battery_w": 3240.0, "soc": 66.0},
        "actual": {"grid_w": -2812.0, "battery_w": 3195.0, "soc": 66.0},
        "deviations": {"battery_energy_kwh": -0.011},
        "export_limit_ok": True,
        "battery_action": {"planned": "laden", "actual": "laden", "ok": True},
    }
    forecast_quality = [
        {"name": "Hauslast", "level": "current", "state": "aktuell",
         "detail": "288 von 288 Slots aus der Prognosequelle"},
        {"name": "PV", "level": "current", "state": "aktuell",
         "detail": "Solcast, p10–p90-Band kalibriert",
         "issued_at": (now - pd.Timedelta(minutes=25)).isoformat()},
        {"name": "Strompreis", "level": "partial",
         "state": "teilweise ergänzt",
         "detail": "96 von 288 Slots per Ähnliche-Tage-Schätzung (Folgetag)"},
    ]
    load_feedback_status = [
        {"label": "Pool/WP Pinguin", "configured": True, "fresh": True,
         "on": True, "power_w": 664.0, "age_seconds": 12.0, "required": True},
        {"label": "Pool/WP klein", "configured": True, "fresh": True,
         "on": False, "power_w": 0.0, "age_seconds": 12.0, "required": True},
    ]
    thermal_calibration = [
        {"name": "Pool", "status": "applied", "n_windows": 441, "r2": 0.74,
         "applied": {"loss_w_per_k": 163.7, "solar_absorption": 0.52},
         "message": "reale Stufenrückmeldung verwendet"},
    ]

    out = build_dashboard(cfg, t, res.total_cost_ct,
                          export_line_w=res.export_line_w, savings_eur=42.17,
                          load_temp_actual=load_temp_actual,
                          ambient_temp_c=ambient_full,
                          solver_status=solver_status,
                          execution_status=execution_status,
                          forecast_quality=forecast_quality,
                          load_feedback_status=load_feedback_status,
                          thermal_calibration=thermal_calibration)

    # Fürs Repo eigenständig lauffähig: Plotly vom CDN statt lokaler Datei
    # (Version der JS-Bibliothek, nicht des Python-Pakets!)
    from plotly.offline import get_plotlyjs_version
    html = open(out, encoding="utf-8").read()
    html = html.replace(
        '<script src="plotly.min.js"></script>',
        f'<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js">'
        f'</script>')
    html = _with_demo_api(html, now)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Beispiel-Dashboard geschrieben: {out}")


if __name__ == "__main__":
    main()
