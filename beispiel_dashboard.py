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

    out = build_dashboard(cfg, t, res.total_cost_ct,
                          export_line_w=res.export_line_w, savings_eur=42.17,
                          load_temp_actual=load_temp_actual,
                          ambient_temp_c=ambient_full)

    # Fürs Repo eigenständig lauffähig: Plotly vom CDN statt lokaler Datei
    # (Version der JS-Bibliothek, nicht des Python-Pakets!)
    from plotly.offline import get_plotlyjs_version
    html = open(out, encoding="utf-8").read()
    html = html.replace(
        '<script src="plotly.min.js"></script>',
        f'<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js">'
        f'</script>')
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Beispiel-Dashboard geschrieben: {out}")


if __name__ == "__main__":
    main()
