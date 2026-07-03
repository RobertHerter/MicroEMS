"""Orchestrierung des EMS-Laufs.

Ablauf je Zyklus:
  1. Verbrauchsprognose (72 h) aus Historie erzeugen und in InfluxDB schreiben.
  2. Eingangsdaten für den Optimierungshorizont (48 h) lesen
     (Preis & PV-Vorhersage für die Zukunft, aktueller Haus-/Auto-SoC).
  3. MILP-Optimierung -> Steuertabelle.
  4. Steuerbefehle per MQTT an Homey publizieren.
  5. Steuertabelle + prognostizierte SoCs/Zustände in InfluxDB schreiben.
  6. Dashboard erzeugen.

Aufruf:
  python -m ems.main --config config.yaml          # einmalig
  python -m ems.main --config config.yaml --loop    # Dauerbetrieb (Intervall aus config)
"""
from __future__ import annotations

import argparse
import logging
import time as _time
from datetime import timedelta

import numpy as np
import pandas as pd

from .config import Config, load_config
from .dashboard import build_dashboard
from .forecast import LoadForecaster, load_history
from .homey_mqtt import HomeyMqttPublisher
from .influx import InfluxRepository
from .optimizer import Optimizer, OptimizerInputs

log = logging.getLogger("ems.main")

CONTROL_COLS = [
    # E3DC-Steuerbefehle (Limits nur bei Abweichung vom Eigenverbrauch):
    "batt_charge_limit_w", "batt_discharge_limit_w",
    "batt_grid_charge_w", "batt_grid_discharge_w",
    "charge_limited", "discharge_limited",
    # rohe Optimierer-Leistungen (Referenz/Analyse):
    "batt_dc_charge_w", "batt_ac_charge_w", "batt_discharge_w",
    "car_charge_w", "grid_import_w", "grid_export_w",
]
PREDICTION_COLS = [
    "house_soc_wh", "house_soc_percent", "car_soc_wh", "car_soc_percent",
    "slot_cost_ct", "price_ct_kwh", "feedin_ct_kwh", "pv_w", "house_load_w",
]


def _now_slot(config: Config) -> pd.Timestamp:
    freq = f"{config.general.slot_minutes}min"
    return pd.Timestamp.now(tz=config.general.timezone).floor(freq)


def run_once(config: Config) -> None:
    repo = InfluxRepository(config)
    try:
        now = _now_slot(config)
        freq = f"{config.general.slot_minutes}min"
        opt_end = now + timedelta(hours=config.general.optimization_horizon_hours)

        # --- 1) Verbrauchsprognose (72 h) -------------------------------- #
        log.info("Lade Verbrauchs-Historie und erstelle Prognose ...")
        history = load_history(repo, config, now)
        forecaster = LoadForecaster(config)
        load_fc = forecaster.forecast(history, now, config.general.n_forecast_slots)
        repo.write_frame(
            "load_forecast",
            pd.DataFrame({"house_load_w": load_fc.values}, index=load_fc.index),
        )
        log.info("Verbrauchsprognose (%d Slots) in InfluxDB geschrieben.", len(load_fc))

        # Optimierungshorizont = erste 48 h der Prognose
        opt_index = pd.date_range(now, periods=config.general.n_opt_slots, freq=freq,
                                  tz=config.general.timezone)
        house_load = load_fc.reindex(opt_index).ffill().bfill().values

        # --- 2) Eingangsdaten lesen ------------------------------------- #
        pv = repo.read_slots("pv_forecast", now, opt_end).reindex(opt_index).ffill().bfill()
        price = repo.read_slots("electricity_price", now, opt_end).reindex(opt_index).ffill().bfill()

        if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
            feedin = repo.read_slots("feed_in_tariff", now, opt_end).reindex(opt_index)
            feedin = feedin.ffill().bfill().fillna(config.feed_in.fixed_ct_kwh)
        else:
            feedin = pd.Series(config.feed_in.fixed_ct_kwh, index=opt_index)

        # Anfangs-SoC Haus
        lookback = now - timedelta(hours=6)
        soc_pct = repo.read_scalar_latest("battery_soc", lookback, now)
        if soc_pct is None:
            log.warning("Kein Haus-SoC gefunden – nehme min_soc an.")
            soc_pct = config.house_battery.min_soc_percent
        init_house_soc = soc_pct / 100.0 * config.house_battery.capacity_wh

        # Anfangs-SoC Auto (optional)
        init_car_soc = None
        car_present = False
        if config.vehicle.enabled and repo.signal_available("vehicle_soc"):
            car_pct = repo.read_scalar_latest("vehicle_soc", lookback, now)
            if car_pct is not None:
                init_car_soc = car_pct / 100.0 * config.vehicle.capacity_wh
                car_present = True
                log.info("Auto erkannt, SoC %.1f%%.", car_pct)

        # --- 3) Optimierung --------------------------------------------- #
        log.info("Starte MILP-Optimierung (%d Slots) ...", len(opt_index))
        inputs = OptimizerInputs(
            index=opt_index,
            house_load_w=np.asarray(house_load, dtype=float),
            pv_w=pv.values.astype(float),
            price_ct_kwh=price.values.astype(float),
            feedin_ct_kwh=feedin.values.astype(float),
            initial_house_soc_wh=init_house_soc,
            initial_car_soc_wh=init_car_soc,
            car_present=car_present,
        )
        result = Optimizer(config).solve(inputs)
        log.info("Optimierung: %s, erwartete Netto-Kosten %.2f € (Horizont).",
                 result.status, result.total_cost_ct / 100.0)

        # --- 4) MQTT (best effort – darf den Lauf nicht abbrechen) ------ #
        try:
            HomeyMqttPublisher(config).publish(result.table, now)
        except Exception as exc:
            log.warning("MQTT-Ausgabe fehlgeschlagen (%s) – InfluxDB-Writeback "
                        "und Dashboard werden trotzdem erstellt.", exc)

        # --- 5) InfluxDB-Writeback -------------------------------------- #
        ctrl = result.table[[c for c in CONTROL_COLS if c in result.table.columns]]
        repo.write_frame("control_table", ctrl)
        pred = result.table[[c for c in PREDICTION_COLS if c in result.table.columns]]
        repo.write_frame("predicted_state", pred)
        log.info("Steuertabelle + Prognosezustände in InfluxDB geschrieben.")

        # --- 6) Dashboard ----------------------------------------------- #
        if config.dashboard.enabled:
            display = _build_display_table(repo, config, now, result.table)
            build_dashboard(config, display, result.total_cost_ct)
    finally:
        repo.close()


def _build_display_table(repo, config, now, opt_table) -> pd.DataFrame:
    """Kombiniert die heutigen Ist-Werte (0 Uhr .. jetzt) mit der Optimierungstabelle."""
    freq = f"{config.general.slot_minutes}min"
    day_start = now.normalize()
    past_index = pd.date_range(day_start, now, freq=freq, tz=config.general.timezone,
                               inclusive="left")
    cols = list(opt_table.columns)
    past = pd.DataFrame(index=past_index, columns=cols, dtype="float64")
    if len(past_index) > 0:
        try:
            past["house_load_w"] = repo.read_slots("house_consumption", day_start, now).reindex(past_index)
            past["pv_w"] = repo.read_slots("pv_generation", day_start, now).reindex(past_index)
            past["price_ct_kwh"] = repo.read_slots("electricity_price", day_start, now).reindex(past_index)
            soc = repo.read_slots("battery_soc", day_start, now).reindex(past_index)
            past["house_soc_percent"] = soc
        except Exception as exc:  # pragma: no cover
            log.warning("Ist-Werte für Dashboard unvollständig: %s", exc)
    # Steuerbefehle in der Vergangenheit = 0
    for c in ["batt_dc_charge_w", "batt_ac_charge_w", "batt_discharge_w",
              "car_charge_w", "grid_import_w", "grid_export_w"]:
        if c in past.columns:
            past[c] = past[c].fillna(0.0)
    return pd.concat([past, opt_table])


def start_dashboard_server(config: Config) -> None:
    """Startet im Dienstmodus einen kleinen HTTP-Server, der das Dashboard
    im Browser abrufbar macht (http://<host>:<port>/). Läuft als Daemon-Thread."""
    import functools
    import http.server
    import os
    import threading

    out = os.path.abspath(config.dashboard.output_path)
    directory = os.path.dirname(out) or "."
    fname = os.path.basename(out)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
                self.path = "/" + fname
            return super().do_GET()

        def log_message(self, *a):  # ruhig bleiben
            pass

    handler = functools.partial(Handler, directory=directory)
    try:
        srv = http.server.ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)
    except OSError as exc:
        log.warning("Dashboard-Server konnte nicht starten (%s).", exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Dashboard-Server läuft: http://%s:%d/ (Datei: %s)",
             config.dashboard.host, config.dashboard.port, fname)


def main() -> None:
    parser = argparse.ArgumentParser(description="EMS – Energy Management System")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur Konfiguration")
    parser.add_argument("--loop", action="store_true", help="Dauerbetrieb im Intervall")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)

    if not args.loop:
        run_once(config)
        return

    if config.dashboard.enabled and config.dashboard.serve:
        start_dashboard_server(config)

    interval = config.general.run_interval_minutes * 60
    log.info("Dauerbetrieb: Intervall %d min.", config.general.run_interval_minutes)
    while True:
        t0 = _time.time()
        try:
            run_once(config)
        except Exception:  # pragma: no cover
            log.exception("Fehler im EMS-Zyklus – fahre fort.")
        sleep_s = max(5.0, interval - (_time.time() - t0))
        _time.sleep(sleep_s)


if __name__ == "__main__":
    main()
