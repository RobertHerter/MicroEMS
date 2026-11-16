#!/usr/bin/env python3
"""Map non-secret MicroEMS plant settings to the local EOS comparison instance."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import yaml


MICROEMS_CONFIG = Path("/opt/ems/config.yaml")
EOS_URL = "http://127.0.0.1:8513"


def request(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{EOS_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def main() -> None:
    config = yaml.safe_load(MICROEMS_CONFIG.read_text())
    general = config["general"]
    battery = config["house_battery"]
    inverter = config["inverter"]
    pv_model = config["pv_model"]
    arrays = pv_model["arrays"]
    inverter_power = round(float(inverter["max_ac_power_w"]))
    cycle_cost = float(config["optimization"].get("cycle_penalty_ct_kwh", 0.0)) / 100.0

    eos_settings = {
        "general": {
            "latitude": general["latitude"],
            "longitude": general["longitude"],
            "timezone": general["timezone"],
        },
        "ems": {
            "mode": "DISABLED",
            "interval": float(general["run_interval_minutes"]) * 60.0,
        },
        "prediction": {
            "hours": general["forecast_horizon_hours"],
            "historic_hours": 72,
        },
        "optimization": {
            "algorithm": "GENETIC",
            "genetic": {
                "horizon_hours": general["optimization_horizon_hours"],
                "seed": 42,
            },
        },
        "devices": {
            "max_batteries": 1,
            "batteries": [{
                "device_id": "battery1",
                "capacity_wh": battery["capacity_wh"],
                "charging_efficiency": battery["charge_efficiency"],
                "discharging_efficiency": battery["discharge_efficiency"],
                "levelized_cost_of_storage_kwh": cycle_cost,
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
            # In MicroEMS currently disabled; zero prevents EOS demo devices.
            "max_electric_vehicles": 0,
            "electric_vehicles": [],
            "max_home_appliances": 0,
            "home_appliances": [],
        },
        "pvforecast": {
            "provider": "PVForecastImport",
            "max_planes": len(arrays),
            "planes": [{
                "surface_tilt": plane["tilt"],
                "surface_azimuth": plane["azimuth"],
                "peakpower": plane["kwp"],
                "pvtechchoice": "crystSi",
                "mountingplace": "building",
                "loss": float(pv_model["system_loss"]) * 100.0,
                "trackingtype": 0,
                # Both planes are connected to the same 12 kW inverter; EOS
                # stores the shared inverter rating on every plane.
                "inverter_paco": inverter_power,
            } for plane in arrays],
        },
        "load": {"provider": "LoadImport"},
        "elecprice": {
            # The imported series is already the complete gross retail price,
            # including MicroEMS' markup, levies, VAT and time-variable grid fee.
            "provider": "ElecPriceImport",
            "charges_kwh": 0.0,
            "vat_rate": 1.0,
            "energycharts": {"bidding_zone": config["tariff"]["bidding_zone"]},
        },
        "feedintariff": {
            "provider": "FeedInTariffImport",
            "feedintarifffixed": {
                "feed_in_tariff_kwh": float(config["feed_in"]["fixed_ct_kwh"]) / 100.0,
            },
            "energycharts": {"bidding_zone": config["tariff"]["bidding_zone"]},
        },
        "weather": {"provider": "WeatherImport"},
    }
    request("PUT", "/v1/config", eos_settings)
    request("PUT", "/v1/config/file")
    print("Applied non-secret MicroEMS plant, tariff and import-provider settings to EOS.")


if __name__ == "__main__":
    main()
