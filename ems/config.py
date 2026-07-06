"""Konfiguration laden und validieren.

Die Konfiguration wird aus einer YAML-Datei geladen und in typisierte
Dataclasses überführt, damit der restliche Code mit klaren Objekten arbeitet
statt mit rohen Dictionaries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any, Dict, Optional

import yaml


# --------------------------------------------------------------------------- #
# Teil-Konfigurationen
# --------------------------------------------------------------------------- #
@dataclass
class GeneralConfig:
    timezone: str = "Europe/Berlin"
    optimization_horizon_hours: int = 48
    forecast_horizon_hours: int = 72
    slot_minutes: int = 15
    run_interval_minutes: int = 15

    @property
    def slots_per_hour(self) -> int:
        return 60 // self.slot_minutes

    @property
    def dt_hours(self) -> float:
        return self.slot_minutes / 60.0

    @property
    def n_opt_slots(self) -> int:
        return int(self.optimization_horizon_hours * self.slots_per_hour)

    @property
    def n_forecast_slots(self) -> int:
        return int(self.forecast_horizon_hours * self.slots_per_hour)


@dataclass
class SignalSpec:
    measurement: str
    field: str
    optional: bool = False
    tags: Dict[str, str] = field(default_factory=dict)
    # DB-seitige Aggregation je Slot: "mean" (Leistung/Preis, rasterunabhängig),
    # "last" (Ladezustand), "sum", "max", "min".
    aggregation: str = "mean"
    # Retention Policy (InfluxDB 1.x), z.B. "autogen" für autogen.solcast.
    retention_policy: Optional[str] = None
    # Einheiten-Umrechnung in interne Einheit (W, ct/kWh, %): wert*scale+offset.
    # Beispiel Preis in €/kWh -> ct/kWh: scale=100. In €/Wh -> ct/kWh: scale=100000.
    scale: float = 1.0
    offset: float = 0.0
    # Füllmethode, wenn das Eingangsraster gröber ist als das Slot-Raster
    # (z.B. stündlicher Preis auf 15-min-Slots):
    #   "interpolate" = lineare Zeit-Interpolation (glatte Signale: Last, PV)
    #   "hold"        = letzter Wert halten (stufige Signale: Preis, Tarif)
    fill_method: str = "interpolate"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignalSpec":
        return cls(
            measurement=d["measurement"],
            field=d["field"],
            optional=bool(d.get("optional", False)),
            tags=dict(d.get("tags", {})),
            aggregation=str(d.get("aggregation", "mean")),
            retention_policy=d.get("retention_policy"),
            scale=float(d.get("scale", 1.0)),
            offset=float(d.get("offset", 0.0)),
            fill_method=str(d.get("fill_method", "interpolate")),
        )


@dataclass
class InfluxConfig:
    version: int
    v1: Dict[str, Any]
    v2: Dict[str, Any]
    signals: Dict[str, SignalSpec]
    outputs: Dict[str, str]


@dataclass
class FeedInConfig:
    mode: str = "fixed"           # "fixed" | "db"
    fixed_ct_kwh: float = 8.0


@dataclass
class HouseBatteryConfig:
    capacity_wh: float
    max_dc_charge_w: float
    max_ac_charge_w: float
    max_discharge_w: float
    min_soc_percent: float
    max_soc_percent: float
    charge_efficiency: float = 0.96
    discharge_efficiency: float = 0.96
    # physikalische Gesamt-Ladeleistung (DC + AC zusammen). None -> max(dc, ac)
    max_charge_w: Optional[float] = None

    @property
    def max_total_charge_w(self) -> float:
        if self.max_charge_w is not None:
            return self.max_charge_w
        return max(self.max_dc_charge_w, self.max_ac_charge_w)

    @property
    def min_soc_wh(self) -> float:
        return self.capacity_wh * self.min_soc_percent / 100.0

    @property
    def max_soc_wh(self) -> float:
        return self.capacity_wh * self.max_soc_percent / 100.0


@dataclass
class InverterConfig:
    max_ac_power_w: float


@dataclass
class VehicleConfig:
    enabled: bool
    capacity_wh: float
    max_charge_w: float
    min_charge_w: float
    min_soc_percent: float
    target_soc_percent: float
    departure_time: time
    charge_efficiency: float = 0.92

    @property
    def min_soc_wh(self) -> float:
        return self.capacity_wh * self.min_soc_percent / 100.0

    @property
    def target_soc_wh(self) -> float:
        return self.capacity_wh * self.target_soc_percent / 100.0


@dataclass
class OptimizationConfig:
    terminal_soc_value: Any = "auto"      # "auto" | float (ct/kWh)
    cycle_penalty_ct_kwh: float = 0.1
    solver_time_limit_s: int = 60
    # Eigenverbrauchs-Priorität: Opportunitätskosten (ct/kWh) für Netzeinspeisung.
    # Da die Einspeisevergütung meist deutlich unter dem Wert gespeicherter Energie
    # liegt, wird der Akku aus PV-Überschuss zuerst gefüllt; erst der Überlauf
    # (Akku voll bzw. Ladeleistung erschöpft) wird eingespeist. Löst KEIN Netzladen
    # aus, da der Netzbezug davon unberührt bleibt. 0 = reine Cash-Optimierung.
    export_priority_ct_kwh: float = 0.0
    # Akku ins Netz entladen erlauben? Bei fester Einspeisevergütung nie sinnvoll
    # (Akkuenergie ist mehr wert als die Einspeisung). False = Akku entlädt nur
    # für Last/Auto, nie ins Netz (verhindert unwirtschaftliches "Dumpen").
    allow_grid_discharge: bool = False
    # Ladestrategie:
    #   "asap" = Akku so früh wie möglich aus PV voll laden (Eigenverbrauch),
    #   "peak" = Einspeise-Linie L: Einspeisung auf L deckeln (darunter einspeisen),
    #            PV-Spitze über L lädt den Akku; L minimal, sodass Akku voll wird.
    charge_strategy: str = "asap"
    # Gewicht, wie stark die Einspeise-Linie L minimiert wird.
    peak_charge_weight: float = 30.0


@dataclass
class ForecastConfig:
    lookback_days: int = 730
    holiday_country: str = "DE"
    holiday_subdivision: Optional[str] = None
    weight_same_weekday: float = 3.0
    weight_same_daytype: float = 2.0
    weight_same_month: float = 1.5
    weight_same_season: float = 1.0
    min_samples: int = 3
    # Globaler Korrekturfaktor auf die Verbrauchsprognose (aus kalibrierung.py).
    # 1.0 = keine Korrektur; z.B. 1.05 = Prognose 5 % anheben.
    correction_factor: float = 1.0
    # Temperatur-Ähnlichkeit: historische Tage mit ähnlicher Temperatur höher
    # gewichten (Heiz-/Kühllast). weight_same_temp = Stärke, temp_sigma = Breite
    # (°C) des Gauß-Kerns. 0 = Temperatur ignorieren.
    weight_same_temp: float = 2.0
    temp_sigma: float = 4.0


@dataclass
class MqttConfig:
    enabled: bool = True
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    base_topic: str = "ems"
    publish_setpoints: bool = True
    publish_schedule_json: bool = True
    qos: int = 1
    retain: bool = True


@dataclass
class DashboardConfig:
    enabled: bool = True
    output_path: str = "./dashboard.html"
    # HTTP-Server im Dienstmodus (--loop): Dashboard im Browser abrufbar machen
    serve: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class CalibrationConfig:
    # Zeitabhängige PV-Korrektur (Profil aus kalibrierung.py) anwenden?
    enabled: bool = False
    pv_profile: str = "./kalibrierung_profil.yaml"


@dataclass
class Config:
    general: GeneralConfig
    influxdb: InfluxConfig
    feed_in: FeedInConfig
    house_battery: HouseBatteryConfig
    inverter: InverterConfig
    vehicle: VehicleConfig
    optimization: OptimizationConfig
    forecast: ForecastConfig
    mqtt: MqttConfig
    dashboard: DashboardConfig
    calibration: CalibrationConfig


# --------------------------------------------------------------------------- #
# Laden
# --------------------------------------------------------------------------- #
def _parse_time(value: str) -> time:
    hh, mm = str(value).split(":")[:2]
    return time(int(hh), int(mm))


def load_config(path: str) -> Config:
    """Lädt und validiert die YAML-Konfiguration."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    g = raw.get("general", {})
    general = GeneralConfig(
        timezone=g.get("timezone", "Europe/Berlin"),
        optimization_horizon_hours=int(g.get("optimization_horizon_hours", 48)),
        forecast_horizon_hours=int(g.get("forecast_horizon_hours", 72)),
        slot_minutes=int(g.get("slot_minutes", 15)),
        run_interval_minutes=int(g.get("run_interval_minutes", 15)),
    )
    if 60 % general.slot_minutes != 0:
        raise ValueError("slot_minutes muss ein Teiler von 60 sein (z.B. 5,10,15,30,60).")

    inf = raw["influxdb"]
    signals = {name: SignalSpec.from_dict(spec) for name, spec in inf["signals"].items()}
    influxdb = InfluxConfig(
        version=int(inf["version"]),
        v1=inf.get("v1", {}),
        v2=inf.get("v2", {}),
        signals=signals,
        outputs=inf["outputs"],
    )
    if influxdb.version not in (1, 2):
        raise ValueError("influxdb.version muss 1 oder 2 sein.")

    fi = raw.get("feed_in", {})
    feed_in = FeedInConfig(
        mode=fi.get("mode", "fixed"),
        fixed_ct_kwh=float(fi.get("fixed_ct_kwh", 8.0)),
    )
    if feed_in.mode not in ("fixed", "db"):
        raise ValueError("feed_in.mode muss 'fixed' oder 'db' sein.")

    hb = raw["house_battery"]
    house_battery = HouseBatteryConfig(
        capacity_wh=float(hb["capacity_wh"]),
        max_dc_charge_w=float(hb["max_dc_charge_w"]),
        max_ac_charge_w=float(hb["max_ac_charge_w"]),
        max_discharge_w=float(hb["max_discharge_w"]),
        min_soc_percent=float(hb["min_soc_percent"]),
        max_soc_percent=float(hb["max_soc_percent"]),
        charge_efficiency=float(hb.get("charge_efficiency", 0.96)),
        discharge_efficiency=float(hb.get("discharge_efficiency", 0.96)),
        max_charge_w=(float(hb["max_charge_w"]) if hb.get("max_charge_w") is not None else None),
    )

    inverter = InverterConfig(max_ac_power_w=float(raw["inverter"]["max_ac_power_w"]))

    v = raw.get("vehicle", {})
    vehicle = VehicleConfig(
        enabled=bool(v.get("enabled", False)),
        capacity_wh=float(v.get("capacity_wh", 0)),
        max_charge_w=float(v.get("max_charge_w", 0)),
        min_charge_w=float(v.get("min_charge_w", 0)),
        min_soc_percent=float(v.get("min_soc_percent", 0)),
        target_soc_percent=float(v.get("target_soc_percent", 80)),
        departure_time=_parse_time(v.get("departure_time", "07:00")),
        charge_efficiency=float(v.get("charge_efficiency", 0.92)),
    )

    o = raw.get("optimization", {})
    optimization = OptimizationConfig(
        terminal_soc_value=o.get("terminal_soc_value", "auto"),
        cycle_penalty_ct_kwh=float(o.get("cycle_penalty_ct_kwh", 0.1)),
        solver_time_limit_s=int(o.get("solver_time_limit_s", 60)),
        export_priority_ct_kwh=float(o.get("export_priority_ct_kwh", 0.0)),
        allow_grid_discharge=bool(o.get("allow_grid_discharge", False)),
        charge_strategy=str(o.get("charge_strategy", "asap")),
        peak_charge_weight=float(o.get("peak_charge_weight", 30.0)),
    )

    f = raw.get("forecast", {})
    forecast = ForecastConfig(
        lookback_days=int(f.get("lookback_days", 730)),
        holiday_country=f.get("holiday_country", "DE"),
        holiday_subdivision=f.get("holiday_subdivision"),
        weight_same_weekday=float(f.get("weight_same_weekday", 3.0)),
        weight_same_daytype=float(f.get("weight_same_daytype", 2.0)),
        weight_same_month=float(f.get("weight_same_month", 1.5)),
        weight_same_season=float(f.get("weight_same_season", 1.0)),
        min_samples=int(f.get("min_samples", 3)),
        correction_factor=float(f.get("correction_factor", 1.0)),
        weight_same_temp=float(f.get("weight_same_temp", 2.0)),
        temp_sigma=float(f.get("temp_sigma", 4.0)),
    )

    m = raw.get("mqtt", {})
    mqtt = MqttConfig(
        enabled=bool(m.get("enabled", True)),
        host=m.get("host", "localhost"),
        port=int(m.get("port", 1883)),
        username=m.get("username", ""),
        password=m.get("password", ""),
        base_topic=m.get("base_topic", "ems"),
        publish_setpoints=bool(m.get("publish_setpoints", True)),
        publish_schedule_json=bool(m.get("publish_schedule_json", True)),
        qos=int(m.get("qos", 1)),
        retain=bool(m.get("retain", True)),
    )

    d = raw.get("dashboard", {})
    dashboard = DashboardConfig(
        enabled=bool(d.get("enabled", True)),
        output_path=d.get("output_path", "./dashboard.html"),
        serve=bool(d.get("serve", True)),
        host=d.get("host", "0.0.0.0"),
        port=int(d.get("port", 8080)),
    )

    cal = raw.get("calibration", {})
    calibration = CalibrationConfig(
        enabled=bool(cal.get("enabled", False)),
        pv_profile=cal.get("pv_profile", "./kalibrierung_profil.yaml"),
    )

    return Config(
        general=general,
        influxdb=influxdb,
        feed_in=feed_in,
        house_battery=house_battery,
        inverter=inverter,
        vehicle=vehicle,
        optimization=optimization,
        forecast=forecast,
        mqtt=mqtt,
        dashboard=dashboard,
        calibration=calibration,
    )
