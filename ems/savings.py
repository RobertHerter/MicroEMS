"""Ersparnis-Tracking: Ist-Kosten vs. simulierte "Ohne-EMS"-Baseline.

Für jeden abgeschlossenen Slot werden die tatsächlichen Netzkosten (gemessener
Netzbezug/-einspeisung x Preis) mit einer Simulation verglichen, was der E3DC
im reinen Eigenverbrauchsmodus (ohne EMS-Eingriffe) getan hätte. Die Baseline
führt dazu einen eigenen hypothetischen Akku-SoC über die Zeit fort (Zustand
in einer kleinen JSON-Datei). Die kumulierte Differenz ist die Ersparnis durch
das EMS - wird sie dauerhaft negativ, stimmt etwas am Modell.

Vorzeichen-Konvention grid_power: positiv = Netzbezug, negativ = Einspeisung
(bei Bedarf über scale: -1 am Signal anpassen).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd

from .config import Config
from .optimizer import natural_battery_step

log = logging.getLogger("ems.savings")

REQUIRED_SIGNALS = ("pv_generation", "house_consumption", "grid_power",
                    "electricity_price")


class SavingsTracker:
    def __init__(self, config: Config):
        self.cfg = config
        self.path = config.savings.state_path

    # ------------------------------------------------------------------ #
    def _load_state(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:  # pragma: no cover
            log.warning("Ersparnis-Status %s nicht lesbar (%s) – initialisiere neu.",
                        self.path, exc)
            return None

    def _save_state(self, state: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    def update(self, repo, now: pd.Timestamp) -> Optional[float]:
        """Verrechnet alle seit dem letzten Lauf abgeschlossenen Slots.

        `now` muss auf das Slot-Raster gefloort sein (wie in run_once).
        Rückgabe: kumulierte Ersparnis in EUR (Baseline - Ist), oder None,
        wenn das Tracking nicht möglich ist.
        """
        if not self.cfg.savings.enabled:
            return None
        missing = [s for s in REQUIRED_SIGNALS if not repo.signal_available(s)]
        if missing:
            log.info("Ersparnis-Tracking: Signale %s fehlen – übersprungen.", missing)
            return None

        hb = self.cfg.house_battery
        dt = self.cfg.general.dt_hours
        kwh = dt / 1000.0

        state = self._load_state()
        if state is None:
            soc_pct = repo.read_scalar_latest("battery_soc",
                                              now - timedelta(hours=6), now)
            soc = (soc_pct / 100.0 * hb.capacity_wh
                   if soc_pct is not None else hb.min_soc_wh)
            state = {"last_ts": now.isoformat(), "baseline_soc_wh": float(soc),
                     "cum_actual_ct": 0.0, "cum_baseline_ct": 0.0}
            self._save_state(state)
            log.info("Ersparnis-Tracking initialisiert (Baseline-SoC %.0f Wh).", soc)
            return 0.0

        last = pd.Timestamp(state["last_ts"])
        if last.tzinfo is None:
            last = last.tz_localize(self.cfg.general.timezone)
        else:
            # ISO-Offset (+02:00) -> konfigurierte Zone normalisieren
            last = last.tz_convert(self.cfg.general.timezone)
        if now <= last:
            return (state["cum_baseline_ct"] - state["cum_actual_ct"]) / 100.0
        # Nach langem Stillstand höchstens 7 Tage nachholen.
        if now - last > pd.Timedelta(days=7):
            log.warning("Ersparnis-Tracking: Lücke > 7 Tage – überspringe bis %s.",
                        (now - pd.Timedelta(days=7)).isoformat())
            last = now - pd.Timedelta(days=7)

        pv = repo.read_slots("pv_generation", last, now, fill=False)
        load = repo.read_slots("house_consumption", last, now, fill=False)
        grid = repo.read_slots("grid_power", last, now, fill=False)
        price = repo.read_slots("electricity_price", last, now, fill=False)
        if self.cfg.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
            feedin = repo.read_slots("feed_in_tariff", last, now, fill=False)
        else:
            feedin = pd.Series(self.cfg.feed_in.fixed_ct_kwh, index=pv.index)

        soc = float(state["baseline_soc_wh"])
        rows, idx_out = [], []
        for ts in pv.index:
            vals = (pv.get(ts), load.get(ts), grid.get(ts),
                    price.get(ts), feedin.get(ts))
            if any(v is None or not np.isfinite(v) for v in vals):
                continue  # Datenlücke: Slot überspringen, Baseline-SoC halten
            pv_t, load_t, grid_t, price_t, feedin_t = (float(v) for v in vals)

            soc, _ch, _dis, b_imp, b_exp = natural_battery_step(
                soc, pv_t, load_t, hb, dt)
            baseline_ct = (b_imp * price_t - b_exp * feedin_t) * kwh
            a_imp, a_exp = max(0.0, grid_t), max(0.0, -grid_t)
            actual_ct = (a_imp * price_t - a_exp * feedin_t) * kwh

            state["cum_baseline_ct"] += baseline_ct
            state["cum_actual_ct"] += actual_ct
            rows.append({
                "baseline_cost_ct": round(baseline_ct, 4),
                "actual_cost_ct": round(actual_ct, 4),
                "saved_ct": round(baseline_ct - actual_ct, 4),
                "cum_saved_eur": round(
                    (state["cum_baseline_ct"] - state["cum_actual_ct"]) / 100.0, 4),
                "baseline_soc_percent": round(100.0 * soc / hb.capacity_wh, 2),
            })
            idx_out.append(ts)

        state["baseline_soc_wh"] = float(soc)
        state["last_ts"] = now.isoformat()
        self._save_state(state)

        if rows:
            repo.write_frame("savings",
                             pd.DataFrame(rows, index=pd.DatetimeIndex(idx_out)))
        cum_eur = (state["cum_baseline_ct"] - state["cum_actual_ct"]) / 100.0
        log.info("Ersparnis-Tracking: %d Slots verrechnet, kumuliert %.2f €.",
                 len(rows), cum_eur)
        return cum_eur
