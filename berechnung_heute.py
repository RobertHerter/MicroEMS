"""Einmaliger Rechenlauf mit den gelieferten EOS-Daten (Stundenraster, 48 h).

Bypassed InfluxDB/MQTT: die Eingangsdaten werden direkt aus dem gelieferten
Datensatz verwendet. Erzeugt:
  * steuertabelle_heute.csv  – vollständige Steuertabelle je Stunde
  * dashboard_heute.html     – interaktive Grafik (heute + Folgetag)

Einheiten-Umrechnung:
  Preis/Einspeisung: €/Wh * 100000 = ct/kWh
  Leistung:          Wh je Stundenslot = W (Mittelwert der Stunde)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.config import load_config
from ems.dashboard import build_dashboard
from ems.optimizer import Optimizer, OptimizerInputs

# --------------------------------------------------------------------------- #
# Gelieferte Daten (48 Stundenwerte)
# --------------------------------------------------------------------------- #
PV_WH = [0,0,0,0,0,35.25,430.9,1222.45,2806.175,6252.5,7159.875,7749.050000000001,
         8126.450000000001,9212.025,9445.4,9383.575,8645.65,7167.125,5224.1,
         3266.6499999999996,1580.15,146.275,0,0,0,0,0,0,0,125.09999999999998,
         1619.15,3325.075,5260.25,7424.9,9127.675,10069.425,10687.3,11173.15,
         10958.6,10215.575,8734.05,6843.924999999999,4766.05,2859.475,991.725,
         62.075,0,0]

PRICE_EUR_WH = [0.00026865440000000003,0.00026358499999999994,0.0002584442,
    0.0002622879,0.00026056239999999996,0.0002633708,0.00027844809999999997,
    0.0002843743,0.0002350607,0.00017144329999999999,0.00010936100000000001,
    0.00010821860000000001,0.00010723090000000001,0.000105315,0.00010354190000000001,
    0.00015522360000000003,0.0001569491,0.0002112845,0.0002534581,0.0003334975,
    0.00035942759999999993,0.0003660797,0.00032166890000000003,0.0003079482,
    0.0003156492,0.00030214949999999994,0.000295426,0.00029279100000000004,
    0.00029290319999999997,0.0002986322,0.00031707379999999997,0.0003166981,
    0.0002890884,0.00024434950000000005,0.00016615630000000003,0.0001561569,
    0.0001514666,0.0001487585,0.0001485681,0.0002049452,0.00021983209999999997,
    0.00029657859999999997,0.00034225929999999997,0.0004265045,0.0004786639,
    0.00046794709999999995,0.0003716132,0.00032755939999999997]

FEEDIN_EUR_WH = [0.0000777075] * 48

LOAD_W = [1212.203464760702,1212.2333051509145,1242.4555588697854,1072.9276846645469,
    1083.1097699155885,1430.9793882065821,1369.1521131682728,1185.1849488940677,
    1587.5348295408367,1599.975472858208,1590.6969019524067,1738.174740476693,
    1810.5446145963115,1777.3959299340327,1818.6338610868042,1790.8994634181681,
    2163.6125386451113,1817.842612416212,1771.227444279905,1751.4095530782433,
    1462.1845144379627,1543.867127955779,1366.7764002153626,1060.5942926235477,
    941.5623590661875,944.9639696946339,947.1845826491307,816.5865375335862,
    782.6608130040973,874.7462237574013,1346.7781168416575,1536.5771016316583,
    1817.1848532603506,1991.7668597449017,2117.056859906872,2304.818210069844,
    2233.128972343962,2482.7465348974165,2129.789791859055,2363.4203824140204,
    2403.6100103934036,1950.1648607882507,1848.9532350212048,1694.5227884042092,
    1349.8720208862874,1553.0281362218407,1215.472430642749,1139.5040162451]

INIT_BATTERY_SOC_PCT = 38.0
INIT_CAR_SOC_PCT = 100.0


def main() -> None:
    cfg = load_config("config.yaml")
    N = 48
    assert cfg.general.n_opt_slots == N, "Horizont/Slot passt nicht zu 48 Stundenwerten"

    # Startzeit: heute 00:00 lokal
    start = pd.Timestamp.now(tz=cfg.general.timezone).normalize()
    index = pd.date_range(start, periods=N, freq="60min", tz=cfg.general.timezone)

    # Einheiten
    price_ct_kwh = np.array(PRICE_EUR_WH) * 100_000.0
    feedin_ct_kwh = np.array(FEEDIN_EUR_WH) * 100_000.0
    pv_w = np.array(PV_WH, dtype=float)
    load_w = np.array(LOAD_W, dtype=float)

    inp = OptimizerInputs(
        index=index,
        house_load_w=load_w,
        pv_w=pv_w,
        price_ct_kwh=price_ct_kwh,
        feedin_ct_kwh=feedin_ct_kwh,
        initial_house_soc_wh=INIT_BATTERY_SOC_PCT / 100.0 * cfg.house_battery.capacity_wh,
        initial_car_soc_wh=INIT_CAR_SOC_PCT / 100.0 * cfg.vehicle.capacity_wh,
        car_present=True,
    )

    res = Optimizer(cfg).solve(inp)
    t = res.table

    # CSV
    t_out = t.copy()
    t_out.index.name = "zeit"
    t_out.to_csv("steuertabelle_heute.csv")

    # Dashboard (nur die Optimierungstabelle, da keine Ist-Historie vorliegt)
    build_dashboard(cfg, t, res.total_cost_ct)

    # --- Zusammenfassung ---
    imp_kwh = (t["grid_import_w"].sum()) / 1000.0
    exp_kwh = (t["grid_export_w"].sum()) / 1000.0
    pv_kwh = pv_w.sum() / 1000.0
    load_kwh = load_w.sum() / 1000.0
    ac_charge_kwh = t["batt_ac_charge_w"].sum() / 1000.0
    dc_charge_kwh = t["batt_dc_charge_w"].sum() / 1000.0
    dis_kwh = t["batt_discharge_w"].sum() / 1000.0

    print("=" * 64)
    print(f"Status: {res.status}")
    print(f"Erwartete Netto-Stromkosten (48 h): {res.total_cost_ct/100:8.2f} EUR")
    print("-" * 64)
    print(f"Hausverbrauch gesamt:      {load_kwh:8.1f} kWh")
    print(f"PV-Erzeugung gesamt:       {pv_kwh:8.1f} kWh")
    print(f"Netzbezug gesamt:          {imp_kwh:8.1f} kWh")
    print(f"Einspeisung gesamt:        {exp_kwh:8.1f} kWh")
    print(f"Akku DC-Laden (aus PV):    {dc_charge_kwh:8.1f} kWh")
    print(f"Akku AC-Laden (aus Netz):  {ac_charge_kwh:8.1f} kWh")
    print(f"Akku Entladen:             {dis_kwh:8.1f} kWh")
    print(f"Akku-SoC Start/Ende:       {INIT_BATTERY_SOC_PCT:5.0f}% -> "
          f"{t['house_soc_percent'].iloc[-1]:5.1f}%")
    if "car_soc_percent" in t.columns:
        print(f"Auto-SoC Start/Ende:       {INIT_CAR_SOC_PCT:5.0f}% -> "
              f"{t['car_soc_percent'].iloc[-1]:5.1f}%")
    print("-" * 64)
    # Grid-Laden nur bei günstigem Preis?
    ac_slots = t[t["batt_ac_charge_w"] > 1]
    if len(ac_slots):
        print(f"Netz-Ladung des Akkus in {len(ac_slots)} Stunden, "
              f"Ø Preis dabei {ac_slots['price_ct_kwh'].mean():.1f} ct/kWh "
              f"(Gesamt-Ø {price_ct_kwh.mean():.1f} ct/kWh)")
    else:
        print("Kein Netzladen des Akkus (PV + Preisstruktur genügen).")
    print("=" * 64)

    # Kompakte Stundentabelle (nur relevante Spalten)
    show = t[["price_ct_kwh", "pv_w", "house_load_w", "batt_dc_charge_w",
              "batt_ac_charge_w", "batt_discharge_w", "car_charge_w",
              "grid_import_w", "grid_export_w", "house_soc_percent"]].copy()
    show.index = show.index.strftime("%a %H:%M")
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 60)
    print(show.round(0).to_string())


if __name__ == "__main__":
    main()
