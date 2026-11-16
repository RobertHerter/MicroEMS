#!/usr/bin/env python3
"""Configure the isolated EOS instance to use its own native forecasts."""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yaml


MICROEMS_CONFIG = Path("/opt/ems/config.yaml")
MICROEMS_DB = Path("/opt/ems/e3dc_history.sqlite")
EOS_URL = "http://127.0.0.1:8523"


def request(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{EOS_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def annual_house_energy_kwh() -> float:
    connection = sqlite3.connect(f"file:{MICROEMS_DB}?mode=ro", uri=True)
    latest_raw = connection.execute("SELECT MAX(ts) FROM house_load").fetchone()[0]
    latest = pd.Timestamp(latest_raw)
    since = (latest - timedelta(days=365)).isoformat()
    watts = connection.execute(
        "SELECT SUM(w) FROM house_load WHERE ts >= ? AND ts <= ?", (since, latest_raw)
    ).fetchone()[0]
    connection.close()
    return round(float(watts or 0.0) * 0.25 / 1000.0, 1)


def main() -> None:
    config = yaml.safe_load(MICROEMS_CONFIG.read_text())
    general = config["general"]
    battery = config["house_battery"]
    inverter = config["inverter"]
    pv_model = config["pv_model"]
    arrays = pv_model["arrays"]
    inverter_power = round(float(inverter["max_ac_power_w"]))
    tariff = config["tariff"]
    # EOS supports one constant surcharge, not MicroEMS' §14a time windows.
    charges_eur_kwh = (
        float(tariff["markup_ct_kwh"])
        + float(tariff["levies_ct_kwh"])
        + float(tariff["grid_fee_default_ct_kwh"])
    ) / 100.0

    eos_settings = {
        "general": {
            "latitude": general["latitude"],
            "longitude": general["longitude"],
            "timezone": general["timezone"],
        },
        "ems": {
            # Native forecasts are refreshed by the systemd timer. No control loop.
            "mode": "DISABLED",
            "interval": float(general["run_interval_minutes"]) * 60.0,
        },
        "prediction": {
            "hours": general["forecast_horizon_hours"],
            "historic_hours": 24 * 35,
        },
        "optimization": {
            "algorithm": "GENETIC",
            "genetic": {
                "horizon_hours": general["optimization_horizon_hours"],
                # The native forecast comparison needs more search depth than
                # the default to avoid isolated, uneconomic discharge blocks.
                "generations": 1200,
                "seed": 42,
            },
        },
        "measurement": {
            "historic_hours": 24 * 365 * 2,
            "load_emr_keys": ["e3dc-house-emr"],
            "pv_production_emr_keys": ["e3dc-pv-emr"],
            "grid_import_emr_keys": ["e3dc-grid-import-emr"],
            "grid_export_emr_keys": ["e3dc-grid-export-emr"],
        },
        "devices": {
            "max_batteries": 1,
            "batteries": [{
                "device_id": "battery1",
                "capacity_wh": battery["capacity_wh"],
                "charging_efficiency": battery["charge_efficiency"],
                "discharging_efficiency": battery["discharge_efficiency"],
                "max_charge_power_w": battery["max_charge_w"],
                "min_charge_power_w": 50,
                "charge_rates": [0.0, 0.25, 0.5, 0.75, 1.0],
                "min_soc_percentage": battery["min_soc_percent"],
                "max_soc_percentage": battery["max_soc_percent"],
            }],
            "max_inverters": 1,
            "inverters": [{
                "device_id": "e3dc-inverter",
                "battery_id": "battery1",
                "max_power_w": inverter["max_ac_power_w"],
                "max_ac_charge_power_w": battery["max_ac_charge_w"],
                "ac_to_dc_efficiency": battery["ac_charge_efficiency"],
                "dc_to_ac_efficiency": 1.0,
            }],
            "max_electric_vehicles": 0,
            "electric_vehicles": [],
            "max_home_appliances": 0,
            "home_appliances": [],
        },
        "pvforecast": {
            # The Akkudoktor forecast endpoint currently returns HTTP 500 for
            # this valid east-facing plane; use EOS' built-in keyless fallback.
            "provider": "PVForecastForecastSolar",
            "max_planes": len(arrays),
            "planes": [{
                "surface_tilt": plane["tilt"],
                "surface_azimuth": plane["azimuth"],
                "peakpower": plane["kwp"],
                "pvtechchoice": "crystSi",
                "mountingplace": "building",
                "loss": float(pv_model["system_loss"]) * 100.0,
                "trackingtype": 0,
                "inverter_paco": inverter_power,
            } for plane in arrays],
        },
        "load": {
            "provider": "LoadAkkudoktorAdjusted",
            "loadakkudoktor": {
                "loadakkudoktor_year_energy_kwh": annual_house_energy_kwh(),
            },
        },
        "elecprice": {
            "provider": "ElecPriceEnergyCharts",
            "charges_kwh": charges_eur_kwh,
            "vat_rate": 1.0 + float(tariff["vat_percent"]) / 100.0,
            "energycharts": {"bidding_zone": tariff["bidding_zone"]},
        },
        "feedintariff": {
            "provider": "FeedInTariffFixed",
            "feedintarifffixed": {
                "feed_in_tariff_kwh": float(config["feed_in"]["fixed_ct_kwh"]) / 100.0,
            },
        },
        "weather": {"provider": "OpenMeteo"},
    }
    request("PUT", "/v1/config", eos_settings)
    request("PUT", "/v1/config/file")
    print(
        "Configured native EOS providers; annual load="
        f"{eos_settings['load']['loadakkudoktor']['loadakkudoktor_year_energy_kwh']} kWh."
    )


if __name__ == "__main__":
    main()
