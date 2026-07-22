"""Nachvollziehbare Kurzbegruendungen fuer Akku-Planentscheidungen."""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_plan_explanations(table: pd.DataFrame, config) -> pd.DataFrame:
    """Ergaenzt den finalen Plan um Erklaerung, Energiemenge und Cent-Schaetzung.

    Die Werte sind bewusst als Modellschaetzung gekennzeichnet: Sie erklaeren
    die lokale Entscheidung des bereits geloesten Fahrplans, ersetzen aber
    keinen vollstaendigen Gegenfaktual-Solverlauf je Slot.
    """
    if table.empty or "mode" not in table:
        return table
    t = table.copy()
    n = len(t)
    dt = float(config.general.dt_hours)
    hb = config.house_battery
    price = t.get("price_ct_kwh", pd.Series(0.0, index=t.index)).to_numpy(float)
    pv = t.get("pv_w", pd.Series(0.0, index=t.index)).to_numpy(float)
    load = t.get("house_load_w", pd.Series(0.0, index=t.index)).to_numpy(float)
    modes = t["mode"].fillna("auto").astype(str).to_numpy()

    reasons = [""] * n
    energy = np.full(n, np.nan)
    value = np.full(n, np.nan)
    reference = [""] * n

    def ct(x: float) -> str:
        return f"{x:.1f}".replace(".", ",")

    def when(i: int) -> str:
        return t.index[i].strftime("%a %H:%M").replace(
            "Mon", "Mo").replace("Tue", "Di").replace("Wed", "Mi").replace(
            "Thu", "Do").replace("Fri", "Fr").replace("Sat", "Sa").replace(
            "Sun", "So")

    def future_high(i: int, stop_at_surplus: bool = False):
        end = min(n, i + max(2, int(round(24.0 / dt))))
        if stop_at_surplus:
            for k in range(i + 1, end):
                if pv[k] > load[k] + 100.0:
                    end = k
                    break
        if end <= i + 1:
            return None, None
        j = i + 1 + int(np.argmax(price[i + 1:end]))
        return j, float(price[j])

    for i, mode in enumerate(modes):
        if mode == "auto":
            continue
        if mode == "peak":
            dc = max(0.0, float(t.iloc[i].get("batt_dc_charge_w", 0.0)))
            energy[i] = dc * dt * hb.charge_efficiency / 1000.0
            line = t.iloc[i].get("export_line_w", np.nan)
            line_text = (f" auf {float(line):,.0f} W".replace(",", ".")
                         if pd.notna(line) else "")
            reasons[i] = ("PV-Ladung wird über den Tag verteilt, um die "
                          f"Einspeisespitze{line_text} zu begrenzen.")
        elif mode == "late":
            natural = max(0.0, pv[i] - load[i])
            actual = max(0.0, float(t.iloc[i].get("batt_dc_charge_w", 0.0)))
            energy[i] = max(0.0, natural - actual) * dt / 1000.0
            reasons[i] = ("PV wird zunächst eingespeist, damit der maximale "
                          "Ziel-SoC erst am Ende des PV-Fensters erreicht wird.")
        elif mode == "grid_charge":
            ac = max(0.0, float(t.iloc[i].get("batt_ac_charge_w", 0.0)))
            input_kwh = ac * dt / 1000.0
            energy[i] = input_kwh * hb.eff_ac_charge
            j, high = future_high(i)
            if j is not None:
                delivered = energy[i] * hb.discharge_efficiency
                value[i] = delivered * high - input_kwh * price[i]
                reference[i] = t.index[j].isoformat()
                reasons[i] = (f"Netzladen bei {ct(price[i])} ct/kWh für "
                              f"{when(j)} mit {ct(high)} ct/kWh.")
            else:
                reasons[i] = f"Netzladen bei {ct(price[i])} ct/kWh."
        elif mode in {"hold", "limit_discharge"}:
            imported = max(0.0, float(t.iloc[i].get("grid_import_w", 0.0)))
            energy[i] = imported * dt / 1000.0
            j, high = future_high(i, stop_at_surplus=True)
            if j is not None:
                gross = energy[i] * max(0.0, high - price[i])
                hold_cost = energy[i] * float(getattr(
                    config.optimization, "battery_hold_penalty_ct_kwh", 0.0)
                    or 0.0)
                switch_cost = 0.0
                if mode == "limit_discharge":
                    switch_cost = float(getattr(
                        config.optimization, "battery_switch_penalty_ct", 0.0)
                        or 0.0)
                elif i == 0 or modes[i - 1] not in {"hold", "limit_discharge"}:
                    # Ein zusammenhaengender Hold-Block verursacht nur einen
                    # festen realen Eingriff, nicht einen je 15-min-Slot.
                    switch_cost = float(getattr(
                        config.optimization, "battery_switch_penalty_ct", 0.0)
                        or 0.0)
                value[i] = gross - hold_cost - switch_cost
                reference[i] = t.index[j].isoformat()
                action = "gesperrt" if mode == "hold" else "gedrosselt"
                reasons[i] = (f"Entladen wird {action}, um Akkuenergie von "
                              f"{ct(price[i])} auf {ct(high)} ct/kWh am "
                              f"{when(j)} zu verschieben.")
            else:
                reasons[i] = "Akkuenergie wird für die weitere Restlast reserviert."
        elif mode in {"block_charge", "limit_charge"}:
            natural = max(0.0, pv[i] - load[i])
            actual = max(0.0, float(t.iloc[i].get("batt_dc_charge_w", 0.0)))
            energy[i] = max(0.0, natural - actual) * dt / 1000.0
            feedin = float(t.iloc[i].get("feedin_ct_kwh", 0.0))
            value[i] = energy[i] * feedin
            reasons[i] = ("PV-Laden wird begrenzt, damit Kapazität für eine "
                          "spätere Erzeugungsspitze frei bleibt.")
        elif mode == "grid_discharge":
            power = max(0.0, float(t.iloc[i].get("batt_grid_discharge_w", 0.0)))
            energy[i] = power * dt / 1000.0
            feedin = float(t.iloc[i].get("feedin_ct_kwh", 0.0))
            value[i] = energy[i] * feedin
            reasons[i] = "Akkuenergie wird gezielt ins Netz abgegeben."

    t["decision_reason"] = reasons
    t["decision_energy_kwh"] = np.round(energy, 4)
    t["decision_value_ct"] = np.round(value, 3)
    t["decision_reference_time"] = reference
    return t
