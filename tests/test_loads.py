"""Tests der steuerbaren Lasten (controllable_loads): deferrable + thermal."""
from __future__ import annotations

import numpy as np
import pytest
import pandas as pd
import pulp

from ems.config import ControllableLoad, LoadStage
from ems.loads import add_controllable_loads
from ems.optimizer import Optimizer
from tests.test_optimizer import _day_index, _inputs, _pv_gauss
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
    n = len(idx)
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
        window_from_hour=8, window_to_hour=22, switch_penalty_ct=0.0,
        deadline_hours=24.0)]
    idx = _day_index("2026-01-15", days=2)
    n = len(idx)
    hour = np.asarray(idx.hour + idx.minute / 60.0, dtype=float)
    day2 = np.arange(n) >= n // 2
    # Tag 2 ist DEUTLICH billiger -> ohne Deadline liefe die WM dort.
    price = np.where(day2, 10.0, 35.0)
    res = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                       soc=cfg.house_battery.min_soc_wh))
    assert not res.infeasible
    w = res.table["load_wm_w"]
    assert float(w.iloc[:n // 2].sum()) * DT_H > 500.0, \
        "Zyklus läuft nicht innerhalb der Deadline (Tag 1)"
    assert float(w.iloc[n // 2:].sum()) < 1.0, "Zyklus trotz Deadline auf Tag 2"
    # Gegenprobe: ohne Deadline wandert er auf den billigen Tag 2
    cfg.controllable_loads[0].deadline_hours = 0.0
    res2 = Optimizer(cfg).solve(_inputs(idx, pv=0.0, load=300.0, price=price,
                                        soc=cfg.house_battery.min_soc_wh))
    w2 = res2.table["load_wm_w"]
    assert float(w2.iloc[n // 2:].sum()) * DT_H > 500.0


def test_deferrable_profile_cycle_runs_once_in_cheap_slots():
    """15-min-Kurve: der ganze Zyklus wird einmal gestartet, bevorzugt günstig."""
    cfg = make_config()
    prof = [2000.0, 500.0, 500.0, 2000.0]           # 4×15 min = 1 h
    cfg.controllable_loads = [ControllableLoad(
        name="wm", type="deferrable", power_profile_w=prof, runtime_minutes=60.0,
        switch_penalty_ct=0.0)]
    idx = _day_index("2026-01-15")
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


def test_dashboard_renders_loads_panel(tmp_path):
    """Dashboard rendert das Lasten-Panel; deaktivierte Last -> graue Leiste."""
    from ems.dashboard import build_dashboard
    cfg = make_config()
    cfg.dashboard.output_path = str(tmp_path / "dash.html")
    cfg.controllable_loads = [
        ControllableLoad(name="pool", type="thermal", enabled=True, volume_l=7000,
                         stages=[LoadStage("klein", 400, 3000),
                                 LoadStage("gross", 650, 4000, requires="klein")]),
        ControllableLoad(name="Waschmaschine", type="deferrable", enabled=False,
                         power_w=2000)]
    idx = pd.date_range("2026-07-09 00:00", periods=96, freq="15min", tz="Europe/Berlin")
    t = pd.DataFrame(index=idx)
    t["house_load_w"] = 500.0
    t["pv_w"] = 1000.0
    t["price_ct_kwh"] = 30.0
    t["mode"] = "auto"
    t["load_pool_klein_w"] = np.where((idx.hour >= 11) & (idx.hour < 14), 400.0, 0.0)
    t["load_pool_gross_w"] = 0.0
    out = build_dashboard(cfg, t, total_cost_ct=-500.0)
    html = open(out, encoding="utf-8").read()
    assert "Steuerbare Lasten" in html
    assert "Waschmaschine" in html and "deaktiviert" in html   # graue Leiste
    assert "klein" in html and "gross" in html                 # Pool-Lanes


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
