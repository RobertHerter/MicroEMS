"""Tests der steuerbaren Lasten (controllable_loads): deferrable + thermal."""
from __future__ import annotations

import numpy as np
import pytest
import pandas as pd
import pulp

from ems.config import ControllableLoad, LoadStage
from ems.loads import add_controllable_loads
from ems.optimizer import Optimizer
from tests.test_optimizer import FREQ, TZ, _day_index, _inputs, _pv_gauss
from tests.test_synthetic import make_config

DT_H = 0.25


def test_deferrable_runs_required_time_in_cheap_slots():
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="waschen", type="deferrable", power_w=1000.0, runtime_minutes=60.0,
        switch_penalty_ct=0.0)]
    idx = _day_index("2026-01-15")
    n = len(idx)
    price = np.full(n, 40.0)
    price[:16] = 5.0                       # erste 4 h billig
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_waschen_w"]
    energy = float(w.sum()) * DT_H
    assert abs(energy - 1000.0) < 60.0, f"Laufzeit-Energie {energy:.0f} Wh != ~1000"
    cheap = float(w.iloc[:16].sum()); rest = float(w.iloc[16:].sum())
    assert cheap > rest, "Last läuft nicht bevorzugt in den günstigen Slots"


def test_deferrable_respects_window():
    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="pumpe", type="deferrable", power_w=800.0, runtime_minutes=120.0,
        window_from_hour=10, window_to_hour=14, switch_penalty_ct=0.0)]
    idx = _day_index("2026-06-10")
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=30.0,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_pumpe_w"]
    hours = w.index.tz_convert("Europe/Berlin").hour
    assert float(w[(hours < 10) | (hours >= 14)].sum()) < 1.0, "Last läuft außerhalb des Fensters"
    assert float(w.sum()) * DT_H > 500.0, "Last läuft gar nicht"


def _pool_load(loss=100.0, min_c=27.0, target=28.0):
    return ControllableLoad(
        name="pool", type="thermal", volume_l=7000.0, target_c=target,
        min_c=min_c, max_c=29.0, loss_w_per_k=loss, switch_penalty_ct=0.0,
        stages=[LoadStage(name="klein", power_w=400.0, heat_w=3000.0),
                LoadStage(name="gross", power_w=650.0, heat_w=4000.0,
                          requires="klein")])


def test_thermal_binary_horizon_relaxes_only_distant_blocks():
    cfg = make_config()
    pool = _pool_load()
    pool.decision_minutes = 60
    pool.binary_horizon_hours = 2
    cfg.controllable_loads = [pool]
    idx = _day_index("2026-06-10")[:20]  # fünf Stunden
    inp = _inputs(idx, ambient_temp_c=np.full(len(idx), 20.0),
                  load_state={"pool": 27.0})
    prob = pulp.LpProblem("thermal_horizon", pulp.LpMinimize)
    g_imp = [pulp.LpVariable(f"test_imp_{t}", 0) for t in range(len(idx))]
    add_controllable_loads(prob, cfg, inp, len(idx), DT_H, g_imp=g_imp)
    variables = {v.name: v for v in prob.variables()}

    assert variables["cl_pool_klein_b0"].cat == pulp.LpInteger
    assert variables["cl_pool_klein_b1"].cat == pulp.LpInteger
    assert variables["cl_pool_klein_b2"].cat == pulp.LpContinuous
    assert variables["cl_pool_gross_b4"].cat == pulp.LpContinuous


def test_thermal_holds_band_and_coupling():
    cfg = make_config()
    cfg.controllable_loads = [_pool_load()]
    idx = _day_index("2026-06-10")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 20.0), load_state={"pool": 27.0}))
    assert not res.infeasible
    temp = res.table["load_pool_temp_c"]
    klein = res.table["load_pool_klein_w"]
    gross = res.table["load_pool_gross_w"]
    # Komfort: Band im Wesentlichen gehalten
    assert temp.min() > 27.0 - 0.3, f"Band verletzt (min {temp.min():.2f} °C)"
    assert temp.max() <= 29.0 + 0.05
    # es wird geheizt (Verlust muss ersetzt werden)
    assert float(klein.sum()) * DT_H > 0.0
    # Kopplung: große WP nur, wenn kleine läuft
    assert ((gross <= 1.0) | (klein > 1.0)).all(), "große WP läuft ohne kleine"


def test_thermal_weather_gaps_use_safe_defaults():
    """NaN in optionalem Wetter darf weder PuLP noch den EMS-Lauf abbrechen."""
    cfg = make_config()
    cfg.controllable_loads = [_pool_load()]
    idx = _day_index("2026-06-10")
    n = len(idx)
    ambient = np.full(n, 20.0); ambient[10:20] = np.nan
    solar = np.full(n, 300.0); solar[30:40] = np.nan
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0,
        soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=ambient, solar_w_m2=solar,
        load_state={"pool": 27.0}))
    assert not res.infeasible and res.status == "Optimal"


def test_thermal_recovers_from_low_start():
    """Startet der Pool unter dem Band, wird kräftig (auch mit großer WP) geheizt."""
    cfg = make_config()
    cfg.controllable_loads = [_pool_load(loss=150.0)]
    idx = _day_index("2026-06-10")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 18.0), load_state={"pool": 25.0}))
    assert not res.infeasible
    temp = res.table["load_pool_temp_c"]
    gross = res.table["load_pool_gross_w"]
    klein = res.table["load_pool_klein_w"]
    assert temp.iloc[-1] > temp.iloc[0], "Pool erholt sich nicht"
    assert ((gross <= 1.0) | (klein > 1.0)).all()      # Kopplung auch hier


def test_controllable_load_covered_by_battery_not_grid():
    """Regression: eine steuerbare Last (Pool-WP) nachts (keine PV) muss aus dem
     vollen Akku gedeckt werden, nicht teuer aus dem Netz. Die Entlade-Obergrenze
    im Optimierer zählte nur die Hauslast - der Akku durfte die Last daher nicht
    decken und ihr Verbrauch kam sinnlos aus dem Netz (Modus 'limit_discharge')."""
    cfg = make_config()
    cfg.optimization.allow_grid_discharge = False
    cfg.controllable_loads = [ControllableLoad(
        name="wp", type="deferrable", power_w=1000.0, runtime_minutes=120.0,
        window_from_hour=0, window_to_hour=6, switch_penalty_ct=0.0)]
    idx = _day_index("2026-01-20")           # Winter, damit nachts geheizt wird
    len(idx)
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    # Teuer im Lauf-Fenster (0-6 h), danach billig -> Entladen JETZT ist eindeutig
    # optimal (kein Anreiz, den Akku für später/Terminalwert zu halten).
    price = np.where(hour < 6, 45.0, 8.0)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=price,
        soc=cfg.house_battery.max_soc_wh * 0.9))
    assert not res.infeasible
    t = res.table
    run = t["load_wp_w"] > 1.0
    assert run.any(), "Last läuft gar nicht"
    # Während die Last läuft: Netzbezug ~0 (Akku deckt Haus + Last), kein
    # gedrosseltes Entladen.
    assert float(t.loc[run, "grid_import_w"].max()) < 50.0, \
        "Netzbezug trotz vollem Akku - Akku darf die steuerbare Last nicht decken"
    assert not (t.loc[run, "mode"].astype(str) == "limit_discharge").any()


def test_no_disguised_grid_discharge_with_thermal_load():
    """Regression: die Entlade-Obergrenze enthält cl_power (Akku darf den Pool
    decken). Deckt aber die PV den Pool schon, darf der Akku NICHT zusätzlich
    "für den Pool" entladen - die frei werdende PV ginge ins Netz (getarntes
    Akku->Netz). Absicherung: nie gleichzeitig entladen UND einspeisen."""
    cfg = make_config()
    cfg.optimization.allow_grid_discharge = False
    cfg.controllable_loads = [_pool_load(loss=120.0)]
    idx = _day_index("2026-06-10")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=_pv_gauss(idx, 9000), load=500.0, price=30.0,
        soc=cfg.house_battery.max_soc_wh * 0.8,
        ambient_temp_c=np.full(n, 20.0), load_state={"pool": 27.0}))
    assert not res.infeasible
    t = res.table
    both = (t["batt_discharge_w"] > 5.0) & (t["grid_export_w"] > 5.0)
    assert not both.any(), "gleichzeitig entladen und einspeisen (Akku->Netz-Dump)"


def test_solar_gain_heats_pool_without_electric_heating():
    """Solarer Wärmeeintrag (surface_m2 * solar_absorption * solar_w_m2) muss die
    Pooltemperatur real anheben - unabhängig von den WP-Stufen. Aufbau: Ambient
    == Zieltemperatur (kein Verlust, kein Heizbedarf) -> die WP bleiben in BEIDEN
    Läufen aus (Heizen kostet nur, ohne Nutzen); der einzige Unterschied ist ein
    Mittags-Strahlungspuls. Nur der solare Term kann den Temperaturunterschied
    erklären."""
    cfg = make_config()
    pool = _pool_load(loss=100.0, min_c=20.0, target=25.0)
    pool.surface_m2 = 8.0
    pool.solar_absorption = 0.75
    cfg.controllable_loads = [pool]
    idx = _day_index("2026-06-10")
    n = len(idx)
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    solar = np.where((hour >= 11) & (hour < 15), 700.0, 0.0)   # Mittags-Puls
    common = dict(pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
                 ambient_temp_c=np.full(n, 25.0), load_state={"pool": 25.0})
    res_sun = Optimizer(cfg).solve(_inputs(idx, solar_w_m2=solar, **common))
    res_dark = Optimizer(cfg).solve(_inputs(idx, solar_w_m2=np.zeros(n), **common))
    assert not res_sun.infeasible and not res_dark.infeasible
    t_sun = res_sun.table["load_pool_temp_c"]
    t_dark = res_dark.table["load_pool_temp_c"]
    elec_sun = (res_sun.table["load_pool_klein_w"] + res_sun.table["load_pool_gross_w"]).sum()
    elec_dark = (res_dark.table["load_pool_klein_w"] + res_dark.table["load_pool_gross_w"]).sum()
    assert elec_sun < 1.0 and elec_dark < 1.0, \
        "WP sollte bei ambient==target gar nicht heizen (Testaufbau prüft nur Solar)"
    assert t_sun.max() - t_dark.max() > 0.5, \
        "Solar-Puls hebt die Pooltemperatur nicht spürbar an"


def test_thermal_hot_ambient_stays_feasible():
    """Regression: ist die Umgebung wärmer als das Bandmaximum, gewinnt der Pool
    auch mit allen WP AUS passiv Wärme und übersteigt max_c. Ein hartes oberes Band
    machte das Modell dann unlösbar (Infeasible) – der Fall trat live an einem
    heißen Nachmittag auf. Das obere Band muss weich sein (Komfort-Malus statt
    Sperre); die WP müssen ausbleiben (Kühlen ist nicht möglich)."""
    cfg = make_config()
    cfg.controllable_loads = [_pool_load(loss=120.0, min_c=27.0, target=28.0)]
    idx = _day_index("2026-07-15")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 33.0),          # heißer als max_c=29
        load_state={"pool": 28.9}))
    assert not res.infeasible, "heißer Nachmittag darf nicht Infeasible sein"
    temp = res.table["load_pool_temp_c"]
    klein = res.table["load_pool_klein_w"]
    gross = res.table["load_pool_gross_w"]
    assert temp.max() > 29.0, "passiver Wärmeeintrag sollte das Band übersteigen"
    # Kühlen unmöglich -> nicht heizen, wenn ohnehin zu warm
    assert float(klein.sum()) + float(gross.sum()) < 1.0, "WP heizt, obwohl zu warm"


def test_thermal_load_no_spurious_charge_block():
    """Regression: läuft eine steuerbare Last (Pool-WP) aus PV-Überschuss, ist das
    für den E3DC normaler Eigenverbrauch - der Akku lädt mit dem REST. Die vom Pool
    verbrauchte PV darf NICHT als fehlende Akku-Ladung interpretiert und als
    'Laden gesperrt/gedrosselt' gemeldet werden (kein Akku-Eingriff liegt vor)."""
    cfg = make_config()
    cfg.controllable_loads = [_pool_load(loss=100.0, min_c=27.0, target=28.0)]
    idx = _day_index("2026-06-10")
    n = len(idx)
    # Kräftige PV, moderate Hauslast, Pool startet unter Ziel -> WP heizt tagsüber
    # aus dem Überschuss, während der Akku mit dem Rest lädt.
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=_pv_gauss(idx, 9000), load=500.0, price=30.0,
        soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 20.0), load_state={"pool": 27.2}))
    assert not res.infeasible
    t = res.table
    wp = t["load_pool_klein_w"] + t["load_pool_gross_w"]
    surplus = t["pv_w"] > t["house_load_w"]
    mask = (wp > 1.0) & surplus
    assert mask.any(), "Testszenario ohne Pool-Betrieb bei Überschuss – nichts geprüft"
    spurious = t.loc[mask, "mode"].astype(str).isin(["block_charge", "limit_charge"])
    assert not spurious.any(), \
        "Pool-Verbrauch aus PV fälschlich als Lade-Sperre/-Drossel gemeldet"


@pytest.mark.slow
def test_deferrable_deadline_keeps_run_today():
    """Regression: ohne Deadline schob der Optimierer die Waschmaschine für
    Cent-Bruchteile ans Horizontende ("erst übermorgen"). Mit deadline_hours
    (Default 24) muss der Zyklus innerhalb von 24 h ab JETZT fertig sein,
    auch wenn ein späterer Tag minimal billiger wäre."""
    cfg = make_config()
    prof = [2000.0, 500.0, 500.0, 2000.0]
    cfg.controllable_loads = [ControllableLoad(
        name="wm", type="deferrable", power_profile_w=prof, runtime_minutes=60.0,
        window_from_hour=0, window_to_hour=3, switch_penalty_ct=0.0,
        deadline_hours=24.0)]
    # Schlanker 26-h-Horizont (statt 2 volle Tage): die ersten 24 h teuer, die
    # Stunden 24-26 (JENSEITS der Deadline, Fenster 0-3 am "Tag 2") billig. So
    # bleibt der Deadline-vs-billiger-Tag-Konflikt erhalten, aber das MILP ist
    # klein (schneller Test).
    start = pd.Timestamp("2026-01-15 00:00", tz=TZ)
    idx = pd.date_range(start, periods=26 * 4, freq=FREQ)
    hours = (idx - idx[0]) / pd.Timedelta(hours=1)
    beyond = np.asarray(hours >= 24.0)          # billige Stunden hinter der Deadline
    price = np.where(beyond, 10.0, 35.0)
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_wm_w"]
    assert float(w[~beyond].sum()) * DT_H > 500.0, \
        "Zyklus läuft nicht innerhalb der Deadline (erste 24 h)"
    assert float(w[beyond].sum()) < 1.0, "Zyklus trotz Deadline in den billigen Stunden"
    # Gegenprobe: ohne Deadline wandert er in die billigen Stunden 24-26
    cfg.controllable_loads[0].deadline_hours = 0.0
    res2 = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                        soc=cfg.house_battery.min_soc_wh))
    w2 = res2.table["load_wm_w"]
    assert float(w2[beyond].sum()) * DT_H > 500.0


def test_deferrable_profile_cycle_runs_once_in_cheap_slots():
    """15-min-Kurve: der ganze Zyklus wird einmal gestartet, bevorzugt günstig."""
    cfg = make_config()
    prof = [2000.0, 500.0, 500.0, 2000.0]           # 4×15 min = 1 h
    cfg.controllable_loads = [ControllableLoad(
        name="wm", type="deferrable", power_profile_w=prof, runtime_minutes=60.0,
        switch_penalty_ct=0.0)]
    # Schlanker 12-h-Horizont genügt für "läuft in den günstigen Slots".
    start = pd.Timestamp("2026-01-15 00:00", tz=TZ)
    idx = pd.date_range(start, periods=12 * 4, freq=FREQ)
    n = len(idx)
    price = np.full(n, 40.0)
    price[:8] = 5.0
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_wm_w"]
    energy = float(w.sum()) * DT_H
    assert abs(energy - sum(prof) * DT_H) < 1.0, "kein vollständiger Zyklus"
    assert float(w.iloc[:8].sum()) > float(w.iloc[8:].sum()), "Zyklus nicht in günstigen Slots"


def test_load_mqtt_map_shape():
    """load_mqtt_map liefert Dicts mit label/column/topic für die Ausgabe."""
    cfg = make_config()
    cfg.controllable_loads = [_pool_load()]
    idx = _day_index("2026-06-10")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0, soc=cfg.house_battery.min_soc_wh,
        ambient_temp_c=np.full(n, 20.0), load_state={"pool": 27.0}))
    labels = {e["label"] for e in res.load_mqtt_map}
    assert {"pool/klein", "pool/gross"} <= labels
    for e in res.load_mqtt_map:
        assert e["column"] in res.table.columns


def test_empty_loads_is_noop():
    cfg = make_config()
    idx = _day_index("2026-06-10")
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=500.0, price=30.0, soc=5000.0))
    assert not res.infeasible
    assert not any(c.startswith("load_") for c in res.table.columns)


def test_no_grid_import_is_soft_last_resort():
    """no_grid_import ist WEICH: lässt sich der Pool nur über Netz decken
    (leerer Akku, keine PV) und ist Nicht-Heizen teurer als die Strafe, zieht er
    minimal Netz (Slack) statt den ganzen Plan infeasible zu machen."""
    cfg = make_config()
    cfg.optimization.solver = "highs"
    cfg.optimization.no_grid_import_penalty_ct_kwh = 1.0   # niedrig -> Grid-Heizen erlaubt
    cfg.controllable_loads = [ControllableLoad(
        name="pool", type="thermal", enabled=True, no_grid_import=True,
        volume_l=7000, target_c=28.0, min_c=27.0, max_c=29.0,
        loss_w_per_k=200.0, decision_minutes=60,
        stages=[LoadStage("wp", 800, 3000)])]
    idx = _day_index("2026-01-20")                          # Winter, keine PV
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=30.0,
        soc=cfg.house_battery.min_soc_wh,                   # Akku leer
        ambient_temp_c=np.full(len(idx), 5.0),
        load_state={"pool": 20.0}))                          # weit unter Band
    assert not res.infeasible, "weiche Regel darf nicht infeasible werden"
    run = res.table["load_pool_wp_w"] > 1.0
    assert run.any(), "Pool läuft gar nicht"
    # In Heiz-Slots wird trotz 'kein Netz' Netzstrom gezogen (Slack aktiv).
    assert "load_pool_grid_w" in res.table.columns
    assert float(res.table.loc[run, "load_pool_grid_w"].sum()) > 1.0


def test_dashboard_renders_loads_panel(tmp_path):
    """Dashboard rendert das Lasten-Panel; deaktivierte Last -> graue Leiste."""
    from ems.dashboard import build_dashboard
    cfg = make_config()
    cfg.dashboard.output_path = str(tmp_path / "dash.html")
    cfg.dashboard.controls_enabled = True
    cfg.dashboard.config_editor_enabled = True
    cfg.dashboard.username = "admin"
    cfg.dashboard.password = "secret"
    cfg.controllable_loads = [
        ControllableLoad(name="pool", type="thermal", enabled=True, volume_l=7000,
                         stages=[LoadStage(
                             "klein", 400, 3000, feedback_topic="pool/klein"),
                                 LoadStage(
                                     "gross", 650, 4000, requires="klein",
                                     feedback_topic="pool/gross")]),
        ControllableLoad(name="Waschmaschine", type="deferrable", enabled=False,
                         power_w=2000, power_topic="washer/power",
                         feedback_on_threshold_w=10)]
    idx = pd.date_range("2026-07-09 00:00", periods=96, freq="15min", tz="Europe/Berlin")
    t = pd.DataFrame(index=idx)
    t["house_load_w"] = 500.0
    t["pv_w"] = 1000.0
    t["price_ct_kwh"] = 30.0
    t["mode"] = "auto"
    t["planned_battery_w"] = 500.0
    t["actual_battery_w"] = 350.0
    t["load_pool_klein_w"] = np.where((idx.hour >= 11) & (idx.hour < 14), 400.0, 0.0)
    t["load_pool_gross_w"] = 0.0
    t["actual_load_pool_klein_on"] = np.where(
        (idx.hour >= 12) & (idx.hour < 14), 1.0, 0.0)
    t["actual_load_pool_gross_on"] = np.nan
    t["actual_load_Waschmaschine_on"] = np.where(
        (idx.hour >= 12) & (idx.hour < 13), 1.0, 0.0)
    t["actual_load_Waschmaschine_power_w"] = np.where(
        (idx.hour >= 12) & (idx.hour < 13), 1842.0, 4.0)
    t["load_pool_temp_c"] = np.linspace(26.0, 28.0, len(idx))
    actual_temp = pd.Series(
        np.linspace(25.8, 27.7, len(idx)), index=idx)
    out = build_dashboard(
        cfg, t, total_cost_ct=-500.0,
        load_temp_actual={"pool": actual_temp})
    html = open(out, encoding="utf-8").read()
    assert "Steuerbare Lasten" in html
    assert 'id="config-link"' in html and 'href="/config"' in html
    assert "Akku-Leistung (Soll)" in html
    assert "Abweichung" in html
    assert html.count("Akku-Leistung (Soll):") == 1
    assert "Waschmaschine" in html and "deaktiviert" in html   # graue Leiste
    assert "klein" in html and "gross" in html                 # Pool-Lanes
    assert "pool \\u002f klein" in html
    assert "unbekannt" in html
    # Seit der Zusammenfuehrung hat die LEISTE eine Zeile je Stufe; der Zustand
    # kodiert das Paar aus Soll und Ist. "(Soll)"/"(Ist)" darf es dort nicht
    # mehr geben - in der KURVEN-Legende dagegen sehr wohl, deshalb wird
    # gezielt die Leiste geprueft und nicht das ganze HTML.
    import json as _json
    import re as _re
    _m = _re.search(r'Plotly\.newPlot\(\s*"[^"]+",\s*(\[.*?\]),\s*\{',
                    html, _re.S)
    _bar = [x for x in _json.loads(_m.group(1))
            if x.get("meta") == "load_timeline"][0]
    assert not any("(Soll)" in y or "(Ist)" in y for y in _bar["y"]), _bar["y"]
    assert any(y.startswith("Waschmaschine") for y in _bar["y"]), _bar["y"]
    assert "pool (Soll)" in html and "pool (Ist)" in html
    assert "Abweichung \\u0394" in html
    assert html.count("Abweichung \\u0394") == 1
    # Unified-Hover darf den letzten Istwert nicht an einen späteren
    # Prognosezeitpunkt ziehen.
    assert '"hoverdistance":1' in html
    # Ereignis-Panel: Farben für Warnungen/Schaltvorgänge vorhanden, und die
    # CSS-Klasse enthält jetzt auch die Ereignisart (kind) für die Färbung.
    assert ".event.warn span" in html and ".event.k-switch span" in html
    assert "+esc(e.kind)+" in html


def test_thermal_load_passes_plan_validation():
    """Mit aktivem Pool darf die Planprüfung weder AC-Bilanz noch
    Baseline-Vergleich fälschlich als Verstoß melden (Validator kennt Lasten)."""
    from ems.validate import validate_plan
    from tests.test_optimizer import _pv_gauss
    cfg = make_config()
    cfg.controllable_loads = [_pool_load()]
    idx = _day_index("2026-06-10")
    n = len(idx)
    inp = _inputs(idx, pv=_pv_gauss(idx, 9000), load=800.0, price=30.0,
                  soc=cfg.house_battery.max_soc_wh * 0.6,
                  ambient_temp_c=np.full(n, 20.0), load_state={"pool": 27.0})
    res = Optimizer(cfg).solve(inp)
    assert not res.infeasible
    rules = {x.rule for x in validate_plan(cfg, res, inp)}
    assert "balance.node" not in rules, "AC-Bilanz fälschlich verletzt (Lasten fehlen im Validator)"
    assert "econ.worse_than_baseline" not in rules, "Baseline ohne Pool-Last -> falsch teurer"


def test_no_grid_import_wp_never_runs_on_grid():
    """no_grid_import: läuft eine WP-Stufe, muss der Netzbezug im Slot 0 sein.
    PV-Überschuss UND Akku dürfen die WP decken - Netzstrom nie. Mit leerem
    Akku und ohne PV bleibt die WP aus, auch wenn min_c unterschritten wird."""
    cfg = make_config()
    pool = _pool_load(loss=250.0, min_c=27.0, target=28.0)
    pool.no_grid_import = True
    cfg.controllable_loads = [pool]
    # Kurzer Tagfenster-Horizont (10 h inkl. Mittag) statt voller 24 h: kleiner
    # thermischer MILP -> löst auch unter CPU-Konkurrenz mit dem laufenden
    # ems.service (15-min-Zyklus) schnell zum Optimum. Der frühere volle Tag
    # lief unter Last ins Zeitlimit -> nicht-optimal -> flaky (mehr Zeit half
    # NICHT, da der Optimalitäts-Beweis wall-clock-abhängig ist).
    idx = pd.date_range("2026-06-10 08:00", "2026-06-10 18:00", freq="15min",
                        tz="Europe/Berlin", inclusive="left")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=_pv_gauss(idx, 6000), load=800.0, price=30.0,
        soc=cfg.house_battery.max_soc_wh * 0.8,
        ambient_temp_c=np.full(n, 16.0), load_state={"pool": 26.5}))
    assert not res.infeasible
    t = res.table
    wp = (t["load_pool_klein_w"] + t["load_pool_gross_w"]).values
    imp = t["grid_import_w"].values
    on = wp > 1.0
    assert on.any(), "WP läuft nie - Szenario prüft nichts"
    assert (imp[on] <= 1.0).all(), \
        "Netzbezug in Slots, in denen die WP läuft (no_grid_import verletzt)"


def test_no_grid_import_allows_battery_heating():
    """Akku-Deckung ist erlaubt: nachts (keine PV), Akku voll, billige WP-Slots
    -> die WP darf aus dem Akku laufen, solange kein Netzbezug entsteht."""
    cfg = make_config()
    pool = _pool_load(loss=250.0, min_c=27.0, target=28.0)
    pool.no_grid_import = True
    cfg.controllable_loads = [pool]
    # Kurzer Nachtfenster-Horizont (10 h, pv=0): kleiner thermischer MILP, löst
    # auch unter CPU-Konkurrenz mit dem laufenden ems.service schnell zum
    # Optimum (voller Tag lief unter Last ins Zeitlimit -> flaky).
    idx = pd.date_range("2026-06-10 00:00", "2026-06-10 10:00", freq="15min",
                        tz="Europe/Berlin", inclusive="left")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=300.0, price=40.0,
        soc=cfg.house_battery.max_soc_wh,          # Akku voll
        ambient_temp_c=np.full(n, 10.0), load_state={"pool": 25.5}))
    assert not res.infeasible
    t = res.table
    wp = (t["load_pool_klein_w"] + t["load_pool_gross_w"]).values
    imp = t["grid_import_w"].values
    assert (wp > 1.0).any(), "WP heizt nicht aus dem (vollen) Akku"
    on = wp > 1.0
    assert (imp[on] <= 1.0).all(), "Netzbezug trotz Akku-Deckung"


def test_no_grid_import_off_allows_grid_heating():
    """Gegenprobe: ohne no_grid_import darf (bei leerem Akku) aus dem Netz
    geheizt werden, um das Band zu halten."""
    cfg = make_config()
    pool = _pool_load(loss=250.0, min_c=27.0, target=28.0)
    pool.no_grid_import = False
    cfg.controllable_loads = [pool]
    # Kurzer Nachtfenster-Horizont (10 h, pv=0): s. o. gegen Last-Flake.
    idx = pd.date_range("2026-06-10 00:00", "2026-06-10 10:00", freq="15min",
                        tz="Europe/Berlin", inclusive="left")
    n = len(idx)
    res = Optimizer(cfg).solve(_inputs(
        idx, pv=0.0, load=800.0, price=30.0,
        soc=cfg.house_battery.min_soc_wh,           # Akku leer
        ambient_temp_c=np.full(n, 10.0), load_state={"pool": 26.0}))
    assert not res.infeasible
    wp = res.table["load_pool_klein_w"] + res.table["load_pool_gross_w"]
    assert float(wp.sum()) * DT_H > 1.0, "ohne Flag darf aus dem Netz geheizt werden"


def test_controllable_load_curve_excludes_deviation_and_grid_share():
    """Die Kurve "Steuerb. Lasten" darf NUR die Stufenleistungen summieren.

    Das fruehere Namensmuster "load_*_w" fing zwei Fremdspalten ein:
      * load_deviation_w - die LASTABWEICHUNG (Ist minus Prognose der
        Hauslast). Sie erschien als steuerbare Last; im Hover stand bei
        "Steuerb. Lasten" derselbe Wert wie bei "Verbrauch (Delta)".
      * load_<name>_grid_w - der Netzanteil derselben Last, wodurch eine
        laufende Pumpe doppelt gezaehlt wurde.
    """
    import json as _json
    import pathlib
    import re as _re

    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from ems.dashboard import build_dashboard
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        min_c=26.0, max_c=32.0,
        stages=[LoadStage("klein", 400, 1000)])]
    index = pd.date_range("2026-07-29 10:00", periods=4, freq="15min",
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
        "load_Pool_klein_w": np.full(n, 400.0),      # die echte Stufenleistung
        "load_Pool_grid_w": np.full(n, 400.0),       # deren Netzanteil
        "load_deviation_w": np.full(n, 2470.0),      # voellig andere Groesse
    }, index=index)
    html = pathlib.Path(
        build_dashboard(cfg, table, total_cost_ct=0.0)).read_text(
            encoding="utf-8")
    match = _re.search(r'Plotly\.newPlot\(\s*"[^"]+",\s*(\[.*?\]),\s*\{',
                       html, _re.S)
    curves = [x for x in _json.loads(match.group(1))
              if x.get("name") == "Steuerb. Lasten"]
    assert curves, "Kurve fehlt"

    def _values(series):
        # Plotly kodiert Zahlenreihen als typisiertes Array (base64 bdata),
        # nicht als Liste - sonst findet der Test keine Werte.
        if isinstance(series, dict) and "bdata" in series:
            import base64
            return np.frombuffer(base64.b64decode(series["bdata"]),
                                 dtype=series.get("dtype", "f8"))
        return np.asarray([v for v in series
                           if isinstance(v, (int, float))], dtype=float)

    values = _values(curves[0]["y"])
    assert len(values), "keine Werte"
    # Genau die Stufenleistung - nicht 400+400+2470.
    assert float(np.nanmax(values)) == pytest.approx(400.0), values


def test_no_heating_above_the_heating_limit():
    """Oberhalb von max_c kommt keine Waerme an - hart, nicht nur teuer.

    Real gemessen: der Plan sah 660 W fuer die Pool-Waermepumpe vor, waehrend
    der Pool bei 29,5 °C stand und das Geraet bei 28,5 °C abschaltet - gemessen
    wurden 3 W Standby. Damals stand max_c auf 32 °C und beschrieb ein
    Komfortband statt der Heizgrenze; das Modell plante 1,65 kWh PV-Aufnahme
    ein, die es nicht gab. max_c IST jetzt die Heizgrenze.
    """
    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from ems.optimizer import Optimizer, OptimizerInputs
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True,
        target_c=28.0, min_c=26.0, max_c=28.5,
        thermostat=True,
        volume_l=8000, loss_w_per_k=200.0,
        stages=[LoadStage("gross", 660, 4000)])]
    index = pd.date_range("2026-07-31 10:00", periods=24, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    # Der Ueberschuss wuerde ABGEREGELT: ueber der Einspeisegrenze ist er
    # wertlos, deshalb will das Modell ihn verheizen. Ohne diese Grenze bringt
    # Heizen nichts und der Test bestuende auch ohne die Schranke - genau das
    # hat die Gegenprobe gezeigt.
    cfg.inverter.max_export_w = 3000.0
    inp = OptimizerInputs(
        index=index, house_load_w=np.full(n, 400.0), pv_w=np.full(n, 9000.0),
        price_ct_kwh=np.full(n, 30.0), feedin_ct_kwh=np.full(n, 7.0),
        initial_house_soc_wh=cfg.house_battery.max_soc_wh,
        ambient_temp_c=np.full(n, 25.0),
        load_state={"Pool": 29.5})            # ueber der Heizgrenze
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
    assert res.status == "Optimal", res.status
    geplant = res.table["load_Pool_gross_w"].sum()
    assert geplant == 0.0, f"{geplant} W oberhalb der Heizgrenze eingeplant"


def test_heating_stays_possible_below_the_limit():
    """Die Gegenrichtung: unter der Heizgrenze muss geheizt werden duerfen -
    sonst haette die Schranke die Last stillgelegt statt sie zu begrenzen.

    Der Start liegt bewusst UNTER dem Komfortband: das Modell bewertet
    Poolwaerme nicht an sich, es heizt nur, um die Untergrenze zu halten. Mit
    einem Start mitten im Band plante es zu Recht 0 W - dann haette der Test
    nichts gezeigt.
    """
    import numpy as np
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from ems.optimizer import Optimizer, OptimizerInputs
    from tests.test_synthetic import make_config

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True,
        target_c=28.0, min_c=26.0, max_c=28.5,
        thermostat=True,
        volume_l=8000, loss_w_per_k=200.0,
        stages=[LoadStage("gross", 660, 4000)])]
    index = pd.date_range("2026-07-31 10:00", periods=24, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    inp = OptimizerInputs(
        index=index, house_load_w=np.full(n, 400.0), pv_w=np.full(n, 9000.0),
        price_ct_kwh=np.full(n, 30.0), feedin_ct_kwh=np.full(n, 7.0),
        initial_house_soc_wh=cfg.house_battery.max_soc_wh,
        ambient_temp_c=np.full(n, 25.0),
        load_state={"Pool": 25.5})            # unter Band UND Heizgrenze
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
    assert res.status == "Optimal", res.status
    assert res.table["load_Pool_gross_w"].sum() > 0.0, \
        "unter der Heizgrenze wird gar nicht mehr geheizt"


# --------------------------------------------------------------------------- #
# Neuplanung: begonnene Laufzeit nicht aufgeben
# --------------------------------------------------------------------------- #
def _pool_run(*, laeuft: bool, malus: float = 5.0, start_c: float = 27.8,
              aussen: float = 22.0, target_c: float = 28.0):
    """Einen Slot-0-Zustand vorgeben und den Plan der ersten Slots liefern.

    Die Vorgaben sind nicht beliebig: Der Pool startet ÜBER dem Sollwert (28.0)
    und die Außentemperatur ist mild. Damit ist Weiterheizen wirtschaftlich
    knapp - genau die Lage, in der die Entscheidung zwischen zwei Neuplanungen
    kippte. Bei kaltem Wetter ist Heizen ohnehin zwingend und der Malus
    entscheidet nichts; ein Test dort würde nichts beweisen (erst gemessen,
    dann diese Werte gewählt).
    """
    cfg = make_config()
    last = _pool_load()
    last.switch_penalty_ct = malus
    last.target_c = target_c
    last.max_c = 28.5
    cfg.controllable_loads = [last]
    idx = _day_index("2026-06-10")
    inp = _inputs(idx, pv=0.0, price=30.0,
                  ambient_temp_c=np.full(len(idx), aussen),
                  load_state={"pool": start_c},
                  load_feedback={"pool/klein": bool(laeuft),
                                 "pool/gross": False})
    res = Optimizer(cfg).solve(inp)
    return [1 if v > 10 else 0
            for v in res.table["load_pool_klein_w"].to_numpy()[:4]]


def test_running_stage_is_not_dropped_on_replan():
    """Der Malus bremste nur das Einschalten - eine LAUFENDE Phase abzubrechen
    war gratis. Der Optimierer plante deshalb wiederholt eine Stunde Laufzeit
    und verwarf sie 30 Minuten spaeter (gemessen 01.08.2026 abends, fuenfmal).
    """
    # target_c=min_c deaktiviert hier gezielt die neue thermische Hysterese:
    # Dieser Test isoliert weiterhin allein die Wirkung des Schaltmalus.
    laufend = _pool_run(laeuft=True, target_c=27.0)
    assert laufend[0] == 1, "laufende Stufe wird sofort abgeschaltet"
    # Ohne Malus faellt die Entscheidung rein wirtschaftlich - und dann waere
    # Abschalten guenstiger. Das belegt, dass der Test die Schutzwirkung prueft
    # und nicht bloss eine ohnehin eindeutige Lage.
    assert _pool_run(laeuft=True, malus=0.0, target_c=27.0)[0] == 0, \
        "Szenario ist nicht knapp - der Test wuerde nichts beweisen"


def test_the_penalty_still_resists_starting():
    """Die Gegenrichtung darf nicht verloren gehen: aus dem Stand einzuschalten
    kostet weiterhin - sonst waere aus dem Anti-Takt-Malus ein Anschalt-Anreiz
    geworden."""
    aus_ohne = _pool_run(laeuft=False, malus=0.0)
    aus_mit = _pool_run(laeuft=False, malus=5000.0)
    assert sum(aus_mit) <= sum(aus_ohne), "hoher Malus startet MEHR"


def test_hard_limit_still_wins_over_the_penalty():
    """Oberhalb von max_c muss abgeschaltet werden, auch wenn die Stufe laeuft
    und der Malus dagegen haelt - sonst haette der Rueckwaerts-Malus die harte
    Grenze aufgeweicht."""
    plan = _pool_run(laeuft=True, malus=5000.0, start_c=29.5)
    assert plan[0] == 0, "harte Grenze durch den Malus ausgehebelt"


def test_minimum_on_time_survives_rolling_replanning():
    """Eine begonnene WP-Phase darf nicht durch den naechsten 15-min-Lauf
    beendet werden, obwohl dessen thermische Entscheidungsbloecke neu beginnen."""
    cfg = make_config()
    pool = _pool_load()
    pool.decision_minutes = 15
    pool.min_on_minutes = 60
    cfg.controllable_loads = [pool]
    idx = _day_index("2026-06-10")
    inp = _inputs(
        idx, pv=0.0, price=30.0,
        ambient_temp_c=np.full(len(idx), 22.0),
        load_state={"pool": 27.8},
        load_feedback={"pool/klein": True, "pool/gross": False},
        load_run_state={
            "pool/klein": {"on": True, "minutes": 15.0},
            "pool/gross": {"on": False, "minutes": 120.0},
        })

    res = Optimizer(cfg).solve(inp)

    assert list((res.table["load_pool_klein_w"].iloc[:3] > 10).astype(int)) \
        == [1, 1, 1]


def test_minimum_off_time_survives_rolling_replanning():
    cfg = make_config()
    pool = _pool_load()
    pool.decision_minutes = 15
    pool.min_off_minutes = 30
    cfg.controllable_loads = [pool]
    idx = _day_index("2026-06-10")
    inp = _inputs(
        idx, pv=9000.0, price=30.0,
        ambient_temp_c=np.full(len(idx), 20.0),
        load_state={"pool": 25.5},
        load_feedback={"pool/klein": False, "pool/gross": False},
        load_run_state={
            "pool/klein": {"on": False, "minutes": 15.0},
            "pool/gross": {"on": False, "minutes": 120.0},
        })

    res = Optimizer(cfg).solve(inp)

    assert res.table["load_pool_klein_w"].iloc[0] < 10


def test_running_thermal_load_heats_towards_target_not_only_minimum():
    """Zwischen min_c und target_c bleibt eine bereits laufende Heizphase
    aktiv; sonst pendelt der Regler in kurzen Takten direkt um min_c."""
    cfg = make_config()
    pool = _pool_load(min_c=27.0, target=28.0)
    pool.decision_minutes = 15
    cfg.controllable_loads = [pool]
    idx = _day_index("2026-06-10")
    inp = _inputs(
        idx, pv=9000.0, price=30.0,
        ambient_temp_c=np.full(len(idx), 22.0),
        load_state={"pool": 27.2},
        load_feedback={"pool/klein": True, "pool/gross": False},
        load_run_state={
            "pool/klein": {"on": True, "minutes": 60.0},
            "pool/gross": {"on": False, "minutes": 120.0},
        })

    res = Optimizer(cfg).solve(inp)

    assert res.table["load_pool_klein_w"].iloc[0] > 10


def test_thermal_decision_blocks_are_aligned_to_wall_clock():
    from ems.loads import _decision_block_positions

    idx = pd.date_range("2026-06-10 10:15", periods=7, freq="15min",
                        tz="Europe/Berlin")
    positions = _decision_block_positions(idx, 60)

    assert positions == [0, 0, 0, 1, 1, 1, 1]


# --------------------------------------------------------------------------- #
# Mindestlaufzeit gegen Heiz-Obergrenze
# --------------------------------------------------------------------------- #
def _pool_lock(*, min_on=60.0, gelaufen=0.0, start_c=28.1, max_c=28.5,
               solar=520.0):
    """Laufende Mindestlaufzeit bei steigender Pooltemperatur.

    Die Vorgaben stammen aus dem realen Fall vom 07.08.2026: Pool 28,1 Grad,
    Obergrenze 28,5, kraeftige Einstrahlung. Die Sonne treibt die Temperatur
    innerhalb der Sperrstunde ueber die Grenze.
    """
    cfg = make_config()
    ld = _pool_load()
    ld.max_c = max_c
    ld.surface_m2, ld.solar_absorption = 7.0, 0.52
    ld.min_on_minutes = min_on
    cfg.controllable_loads = [ld]
    idx = _day_index("2026-08-07")[:48]
    inp = _inputs(idx, pv=3000.0, price=30.0,
                  ambient_temp_c=np.full(len(idx), 26.5),
                  solar_w_m2=np.full(len(idx), solar),
                  load_state={"pool": start_c},
                  load_run_state={"pool/klein": {"on": True,
                                                 "minutes": gelaufen}})
    return Optimizer(cfg).solve(inp)


def test_minimum_runtime_never_makes_the_plan_infeasible():
    """Zwei harte Bedingungen widersprachen sich und kippten den GESAMTEN Plan.

    Die Sperre nagelte die Stufe fest (stage_on == 1), die Heiz-Obergrenze
    verlangt gleichzeitig T <= max_c solange sie laeuft. Steigt die Temperatur
    WAEHREND der Sperre darueber, ist beides unerfuellbar - am 07.08.2026 real
    passiert, mit ausgesetzter Steuerung als Folge. Die Pruefung auf T0 sah nur
    den Startwert.
    """
    assert _pool_lock().status == "Optimal"


def test_the_heating_limit_wins_over_the_minimum_runtime():
    """Rangfolge: die Mindestlaufzeit schont den Verdichter, die Obergrenze ist
    Physik. Also weicht die Vorgabe - und zwar sichtbar, nicht heimlich."""
    res = _pool_lock()
    temp = res.table["load_pool_temp_c"].to_numpy()
    an = res.table["load_pool_klein_w"].to_numpy() > 10
    ueber = temp > 28.5 + 1e-6
    assert not (an & ueber).any(), "Stufe laeuft oberhalb der Obergrenze"
    assert ueber.any(), "Szenario erreicht die Grenze nie - prueft nichts"

# Die Gegenrichtung - dass die Sperre ueberhaupt wirkt - decken bereits
# test_minimum_on_time_survives_rolling_replanning und das Gegenstueck fuer die
# Stillstandszeit ab. Ein eigener Test dafuer bestand auch OHNE die Sperre und
# haette nur Sicherheit vorgetaeuscht.


# --------------------------------------------------------------------------- #
# Komfort-Obergrenze getrennt von der Heizgrenze
# --------------------------------------------------------------------------- #
def _pool_sonne(*, comfort_max_c=None, start_c=26.4, max_c=28.5):
    """Sonniger Tag: die Einstrahlung treibt den Pool ohnehin ueber max_c.

    Nachgestellt nach dem realen Fall vom 14.08.2026 - Pool unter dem Band,
    kraeftiger Ueberschuss, und der Plan heizte trotzdem nicht.

    Rueckgabe: (Heizenergie kWh, hoechste Pooltemperatur).
    """
    cfg = make_config()
    ld = _pool_load()
    ld.max_c, ld.comfort_max_c = max_c, comfort_max_c
    ld.surface_m2, ld.solar_absorption = 7.0, 0.52
    ld.switch_penalty_ct = 0.0
    cfg.controllable_loads = [ld]
    idx = _day_index("2026-08-14")[:48]
    stunde = idx.tz_convert(TZ).hour
    sonne = np.where((stunde >= 8) & (stunde <= 18), 600.0, 0.0)
    inp = _inputs(idx, pv=9000.0, load=1500.0, price=12.0,
                  ambient_temp_c=np.full(len(idx), 26.0),
                  solar_w_m2=sonne, load_state={"pool": start_c})
    res = Optimizer(cfg).solve(inp)
    kwh = float(res.table["load_pool_klein_w"].to_numpy().sum()) * DT_H / 1000.0
    spitze = float(res.table["load_pool_temp_c"].to_numpy().max())
    return kwh, spitze


def test_comfort_ceiling_defaults_to_the_heating_limit():
    """Ohne Angabe bleibt alles wie bisher - die Aenderung ist rueckwaerts-
    kompatibel."""
    ohne, _ = _pool_sonne(comfort_max_c=None)
    gleich, _ = _pool_sonne(comfort_max_c=28.5)
    assert ohne == pytest.approx(gleich, abs=0.05)


def test_a_high_comfort_ceiling_frees_the_surplus():
    """max_c sagt, bis WOHIN geheizt wird; comfort_max_c, ab wann es stoert.

    Zusammengelegt bestrafte das Modell auch Waerme, die die Sonne gebracht
    hat - und liess deshalb Ueberschuss ungenutzt, waehrend der Pool UNTER dem
    Wunschwert lag. Am 14.08.2026 real: 8 kW Ueberschuss bei 12 ct blieben
    liegen, geheizt wurde nachts bei 32 ct (dort 1,32 gegen 6,61 kWh).

    Im synthetischen Fall faellt der Effekt kleiner aus, weil Preis- und
    Lastverlauf flach sind - die Richtung ist dieselbe.
    """
    eng, spitze = _pool_sonne(comfort_max_c=None)
    weit, _ = _pool_sonne(comfort_max_c=99.0)
    # Vorbedingung: die Sonne muss die Grenze ueberhaupt ueberschreiten, sonst
    # greift der Malus nicht und der Test prueft nichts.
    assert spitze > 28.5, "Szenario erreicht max_c nie"
    assert weit > eng * 1.25, f"Komfortgrenze ohne Wirkung ({eng:.2f} -> {weit:.2f})"


def test_the_heating_limit_holds_even_without_a_comfort_ceiling():
    """Die harte Grenze darf nicht mit der Komfortgrenze aufweichen.

    Aufbau wie test_no_heating_above_the_heating_limit - inklusive der
    Abregelungsgrenze, ohne die Heizen gar nicht lohnt und der Test nichts
    zeigte -, aber MIT hoher comfort_max_c. Genau diese Kombination ist neu:
    kein Malus nach oben, trotzdem darf oberhalb max_c nicht geheizt werden.
    """
    import pandas as pd

    from ems.config import ControllableLoad, LoadStage
    from ems.optimizer import OptimizerInputs

    cfg = make_config()
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True,
        target_c=28.0, min_c=26.0, max_c=28.5, comfort_max_c=99.0,
        thermostat=True, volume_l=8000, loss_w_per_k=200.0,
        stages=[LoadStage("gross", 660, 4000)])]
    index = pd.date_range("2026-07-31 10:00", periods=24, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    cfg.inverter.max_export_w = 3000.0        # Ueberschuss waere sonst wertlos
    inp = OptimizerInputs(
        index=index, house_load_w=np.full(n, 400.0), pv_w=np.full(n, 9000.0),
        price_ct_kwh=np.full(n, 30.0), feedin_ct_kwh=np.full(n, 7.0),
        initial_house_soc_wh=cfg.house_battery.max_soc_wh,
        ambient_temp_c=np.full(n, 25.0),
        load_state={"Pool": 29.5})            # ueber der Heizgrenze
    res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
    assert res.status == "Optimal", res.status
    geplant = float(res.table["load_Pool_gross_w"].sum())
    assert geplant == 0.0, f"{geplant} W oberhalb der Heizgrenze eingeplant"


def test_comfort_penalty_is_configurable_per_load():
    """Bis 3.7.1 war der Malus fuer die Unterschreitung der Heizgrenze eine feste
    50 im Code - hoeher als jeder Strompreis, weshalb die Grenze IMMER gewann und
    der Pool auch nachts aus dem Akku heizte (17.08.2026, Nachtpreise 33-38 ct).
    Der Wert gehoert der Last: hoch = Komfort entscheidet, niedrig = Preis.
    """
    from tests.test_synthetic import make_config

    def heiz_kwh(pen):
        cfg = make_config()
        cfg.optimization.charge_strategy = "auto"
        pool = ControllableLoad(
            name="Pool", type="thermal", enabled=True,
            target_c=28.0, min_c=27.0, max_c=28.5, comfort_max_c=99.0,
            volume_l=8000, loss_w_per_k=250.0, surface_m2=7.0,
            solar_absorption=0.5, thermostat=True, decision_minutes=60,
            binary_horizon_hours=24.0, comfort_penalty_ct_per_k_slot=pen,
            stages=[LoadStage(name="WP", power_w=1200.0, heat_w=2400.0)])
        cfg.controllable_loads = [pool]
        idx = _day_index("2026-01-15")          # Winternacht, keine PV
        n = len(idx)
        inp = _inputs(idx, pv=0.0, load=900.0, price=36.0, soc=9000.0)
        inp.ambient_temp_c = np.full(n, 8.0)     # kalt -> echter Waermeverlust
        inp.solar_w_m2 = np.zeros(n)
        inp.load_state = {"Pool": 27.0}          # genau an der Heizgrenze
        res = Optimizer(cfg, store_warm=False, stabilize_plan=False).solve(inp)
        assert not res.infeasible, res.infeasible_reason
        spalten = [c for c in res.table.columns
                   if c.startswith("load_Pool_") and c.endswith("_w")
                   and not c.endswith("_grid_w")]
        return float(res.table[spalten].sum(axis=1).sum()) * 0.25 / 1000.0

    teuer_komfort = heiz_kwh(50.0)     # Vorgabe: Grenze gewinnt
    guenstig = heiz_kwh(1.0)           # Preis gewinnt
    assert teuer_komfort > guenstig + 0.5, (
        f"niedriger Malus muss weniger heizen: {guenstig:.2f} gegen "
        f"{teuer_komfort:.2f} kWh")
    # ohne Angabe gilt weiter die Vorgabe
    assert heiz_kwh(None) == pytest.approx(teuer_komfort, abs=0.3)
