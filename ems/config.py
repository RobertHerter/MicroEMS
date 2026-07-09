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
    # Neuberechnung auf das Uhr-Raster synchronisiert (:00/:15/:30/:45); dieser
    # Versatz (Sekunden nach der Marke) gibt neuen Messwerten Zeit anzukommen.
    run_offset_seconds: int = 10

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
    # Solarspitzengesetz (§51 EEG, Neuanlagen): keine Vergütung in Stunden mit
    # negativem Börsenpreis. True = Einspeisung wird dort mit 0 ct bewertet.
    zero_at_negative_price: bool = False


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
    # AC-Laden (Netz) läuft zusätzlich durch den Wechselrichter und ist real
    # schlechter als DC-Laden aus PV (~0.92 statt ~0.96). None -> wie
    # charge_efficiency (altes Verhalten).
    ac_charge_efficiency: Optional[float] = None
    # physikalische Gesamt-Ladeleistung (DC + AC zusammen). None -> max(dc, ac)
    max_charge_w: Optional[float] = None

    @property
    def eff_ac_charge(self) -> float:
        if self.ac_charge_efficiency is not None:
            return self.ac_charge_efficiency
        return self.charge_efficiency

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
    # Einspeisebegrenzung am Netzanschlusspunkt (60/70%-Regel bzw. §9 EEG).
    # None = keine Begrenzung. Der Optimierer plant dann keine Einspeise-
    # Erlöse ein, die real abgeregelt würden.
    max_export_w: Optional[float] = None


@dataclass
class VehicleConfig:
    enabled: bool
    capacity_wh: float
    max_charge_w: float
    min_charge_w: float
    min_soc_percent: float
    target_soc_percent: float
    departure_time: time
    # Abweichende Abfahrtzeit je Wochentag (0=Mo .. 6=So); Wert None = an dem
    # Tag keine Abfahrt. None (gesamt) = departure_time gilt täglich.
    departure_times: Optional[Dict[int, Optional[time]]] = None
    charge_efficiency: float = 0.92
    # Ladekurve: ab diesem SoC (%) sinkt die max. Ladeleistung linear bis auf
    # min_charge_w bei 100 % (reale Autos tapern oberhalb ~80 %). 100 = aus.
    taper_start_soc_percent: float = 100.0

    @property
    def min_soc_wh(self) -> float:
        return self.capacity_wh * self.min_soc_percent / 100.0

    @property
    def target_soc_wh(self) -> float:
        return self.capacity_wh * self.target_soc_percent / 100.0

    def departure_for_weekday(self, weekday: int) -> Optional[time]:
        """Abfahrtzeit für den Wochentag (0=Mo..6=So), None = keine Abfahrt."""
        if self.departure_times is None:
            return self.departure_time
        return self.departure_times.get(weekday, self.departure_time)

    @property
    def has_any_departure(self) -> bool:
        return any(self.departure_for_weekday(d) is not None for d in range(7))


@dataclass
class OptimizationConfig:
    terminal_soc_value: Any = "auto"      # "auto" | float (ct/kWh)
    cycle_penalty_ct_kwh: float = 0.1
    solver_time_limit_s: int = 60
    # CBC-Threads. 0 = automatisch (CPU-Kerne - 1).
    solver_threads: int = 0
    # Malus (ct) je Einschaltvorgang der Wallbox: verhindert, dass das Auto
    # bei zappeligen Preisen ständig ein-/ausgeschaltet wird (Schützverschleiß).
    # 0 = aus.
    car_switch_penalty_ct: float = 5.0
    # Strafe (ct/kWh) je fehlender kWh zum Auto-Ziel-SoC bei Abfahrt. Das Ziel
    # ist eine WEICHE Nebenbedingung: ist es unerreichbar, lädt der Plan so
    # viel wie möglich statt komplett auf 'auto' zurückzufallen.
    car_target_penalty_ct_kwh: float = 200.0
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
    #   "peak" = Einspeise-Linie L (pro Tag): Einspeisung auf L deckeln (darunter
    #            einspeisen), PV-Spitze über L lädt den Akku; L minimal, sodass
    #            der Akku voll wird. (Standard)
    #   "asap" = Akku so früh wie möglich aus PV voll laden (Eigenverbrauch).
    #   "auto" = pro Tag automatisch: viel PV-Überschuss -> peak, sonst asap. (Standard)
    charge_strategy: str = "auto"
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
    # Rezenz: jüngere Historie exponentiell höher gewichten (Halbwertszeit in
    # Tagen). Verhaltensänderungen schlagen so zeitnah durch. 0 = aus.
    half_life_days: float = 120.0
    # Intraday-Korrektur: Ist/Prognose-Verhältnis der letzten Stunden auf die
    # Zukunft anwenden (mit Lead-Time abklingend). Fängt Tagesabweichungen
    # (Besuch, Wetter), die das Ähnliche-Tage-Modell nicht sehen kann.
    intraday_enabled: bool = True
    intraday_window_hours: float = 3.0     # Fenster für das Ist/Prognose-Verhältnis
    intraday_decay_hours: float = 6.0      # Halbwertszeit des Abklingens
    intraday_max_factor: float = 1.5       # Faktor-Begrenzung (und 1/x nach unten)
    # Geschätzte (noch unbekannte) Folgetag-Preise zur Mitte stauchen:
    # p' = m + (p - m) * (1 - price_damping). Verhindert, dass auf
    # prognostizierte Preistäler/-spitzen spekuliert wird. 0 = aus, 1 = flach.
    price_damping: float = 0.3


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
    # Nur für ems/schedule (Info). Sollwerte werden nie retained (Fail-safe:
    # Broker darf nach EMS-Ausfall keine veralteten Steuerbefehle ausliefern).
    retain: bool = True
    # ems/schedule schlank halten (Homey kappt große Payloads): nur diese
    # Felder je Slot und höchstens so viele Stunden. schedule_max_hours=0 = alle.
    schedule_fields: list = field(default_factory=lambda: [
        "batt_charge_limit_w", "batt_discharge_limit_w", "batt_grid_charge_w",
        "batt_grid_discharge_w", "car_charge_w", "mode", "house_soc_percent",
        "price_ct_kwh"])
    schedule_max_hours: int = 24


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
class SavingsConfig:
    # Ersparnis-Tracking: Ist-Kosten vs. simulierte "Ohne-EMS"-Baseline je Slot
    # (braucht die Signale pv_generation, house_consumption, grid_power, Preis).
    enabled: bool = True
    state_path: str = "./savings_state.json"


@dataclass
class MonitoringConfig:
    # Predicted-vs-Actual-SoC-Drift (ems/drift.py).
    drift_enabled: bool = True
    drift_window_hours: float = 12.0
    drift_alert_percent: float = 8.0     # Warnung ab dieser MAE (Prozentpunkte)


@dataclass
class WeatherConfig:
    # Temperatur direkt von Open-Meteo (kostenlos, kein Key) statt InfluxDB.
    enabled: bool = False
    latitude: float = 0.0
    longitude: float = 0.0
    past_days: int = 92          # Forecast-API-Fenster (max 92)
    forecast_days: int = 4


@dataclass
class SolcastSource:
    api_key: str
    resource_id: str


@dataclass
class SolcastConfig:
    # PV-Vorhersage direkt von Solcast (rooftop sites) statt InfluxDB.
    enabled: bool = False
    # "sum"  = Resourcen sind verschiedene Arrays (Ost/West) -> je Slot addieren
    # "mean" = dieselbe Anlage über mehrere Keys -> redundante Forecasts mitteln
    combine: str = "sum"
    calls_per_key_per_day: int = 10       # Free-Tier: 10/Key/Tag
    sources: list = field(default_factory=list)   # [SolcastSource]
    # Abruf-Verteilung: gleichmäßig über dieses lokale Stundenfenster (Tageslicht).
    # 0..24 = ganzer Tag. Je Quelle wird key_budget/(Quellen je Key) mal abgerufen.
    window_start_hour: int = 5
    window_end_hour: int = 22


@dataclass
class GridFeeWindow:
    """Ein §14a-EnWG-Zeitfenster: Netzentgelt (ct/kWh netto) für die Slots, die
    ALLE gesetzten Filter erfüllen. Nicht gesetzte Filter (None) = egal.
    Ausgewertet in lokaler Zeit. Das erste passende Fenster der Liste gewinnt."""
    ct_kwh: float
    hours: Optional[list] = None       # Stunden 0..23 (lokale Zeit)
    months: Optional[list] = None      # Monate 1..12
    date_from: Optional[str] = None    # "MM-DD" inkl.; mit date_to (Wrap Jahresende ok)
    date_to: Optional[str] = None      # "MM-DD" inkl.
    weekdays: Optional[list] = None    # 0=Mo .. 6=So


@dataclass
class TariffConfig:
    # Bezugspreis direkt aus Energy-Charts (Spot) + Tarifmodell statt InfluxDB.
    enabled: bool = False
    type: str = "dynamic"              # "dynamic" (spotbasiert) | "fixed" (konstant)
    fixed_ct_kwh: float = 30.0         # bei type=fixed: konstanter BRUTTO-Preis
    bidding_zone: str = "DE-LU"        # Energy-Charts-Gebotszone
    markup_percent: float = 0.0        # Aufschlag auf den Spot in %
    markup_ct_kwh: float = 0.0         # zusätzlicher fixer Marge-Aufschlag (netto ct/kWh)
    levies_ct_kwh: float = 0.0         # Steuern & Abgaben (netto ct/kWh, additiv)
    vat_percent: float = 19.0          # MwSt auf die Nettosumme
    grid_fee_mode: str = "included"    # "static" | "included" | "14a"
    grid_fee_ct_kwh: float = 0.0       # bei static: konstantes Netzentgelt (netto)
    grid_fee_default_ct_kwh: float = 0.0  # bei 14a: Fallback, wenn kein Fenster passt
    grid_fee_windows: list = field(default_factory=list)  # bei 14a: [GridFeeWindow]
    history_backfill_days: int = 400   # Tiefe des einmaligen Spot-Backfills


@dataclass
class E3DCRscpConfig:
    # Optionale direkte RSCP-Anbindung des E3DC (Bibliothek pye3dc).
    enabled: bool = False
    host: str = ""                       # IP des E3DC im LAN
    username: str = ""                   # E3DC-Portal-Login
    password: str = ""
    key: str = ""                        # RSCP-Passwort (im E3DC gesetzt)
    read_live: bool = True               # Live-SoC/PV/Last für den Lauf nutzen
    control_enabled: bool = False        # Steuerung per RSCP (greift real ein!)
    grid_sign: float = 1.0               # Vorzeichen Netz (+ = Bezug)
    batt_sign: float = 1.0               # Vorzeichen Akku (+ = Laden)
    # Verbrauchsprognose aus lokaler SQLite (per RSCP gefüllt) statt InfluxDB.
    history_source: bool = False
    history_db_path: str = "./e3dc_history.sqlite"
    history_backfill_days: int = 730     # Tiefe des einmaligen Backfills


@dataclass
class ReportConfig:
    # Debug-Report-Button im Dashboard: lädt den Schnappschuss herunter und
    # öffnet das Mailprogramm vorausgefüllt (Anhang manuell). Kein SMTP nötig.
    enabled: bool = False
    mail_to: str = ""                    # Vorbelegung im Mailprogramm
    snapshot_path: str = "./last_run_debug.json"


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
    savings: SavingsConfig = field(default_factory=SavingsConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    e3dc_rscp: E3DCRscpConfig = field(default_factory=E3DCRscpConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    tariff: TariffConfig = field(default_factory=TariffConfig)
    solcast: SolcastConfig = field(default_factory=SolcastConfig)


# --------------------------------------------------------------------------- #
# Laden
# --------------------------------------------------------------------------- #
def _parse_time(value: str) -> time:
    hh, mm = str(value).split(":")[:2]
    return time(int(hh), int(mm))


_WEEKDAY_KEYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6,
                 "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5,
                 "sun": 6}
_NO_DEPARTURE = (None, "", "-", "none", "keine")


def parse_departure_times(raw, base: time) -> Optional[Dict[int, Optional[time]]]:
    """Parst vehicle.departure_times: Wochentag-Schlüssel (mo..so / mon..sun /
    0..6) -> "HH:MM" oder null/"-"/"keine" (= keine Abfahrt an dem Tag).
    Nicht genannte Tage behalten die Basis-Abfahrtzeit."""
    if not raw:
        return None
    out: Dict[int, Optional[time]] = {d: base for d in range(7)}
    for key, value in dict(raw).items():
        k = str(key).strip().lower()
        if k in _WEEKDAY_KEYS:
            day = _WEEKDAY_KEYS[k]
        elif k.isdigit() and 0 <= int(k) <= 6:
            day = int(k)
        else:
            raise ValueError(f"Unbekannter Wochentag '{key}' in "
                             f"vehicle.departure_times (mo..so erwartet).")
        if value in _NO_DEPARTURE or str(value).strip().lower() in _NO_DEPARTURE:
            out[day] = None
        else:
            out[day] = _parse_time(value)
    return out


def _parse_hours(raw) -> Optional[list]:
    """Stundenliste: akzeptiert Ints und Bereiche "H-H" (Ende exklusiv, Wanduhr).
    Beispiel: [22, "6-9", "16-20"] -> {22, 6,7,8, 16,17,18,19}."""
    if raw is None:
        return None
    hours: set = set()
    for item in (raw if isinstance(raw, (list, tuple)) else [raw]):
        if isinstance(item, str) and "-" in item:
            a, b = item.split("-", 1)
            a, b = int(a), int(b)
            hours.update(range(a, b) if b > a else list(range(a, 24)) + list(range(0, b)))
        else:
            hours.add(int(item))
    return sorted(h % 24 for h in hours)


def parse_grid_fee_windows(raw) -> list:
    """§14a-Fenster aus der YAML-Liste in GridFeeWindow-Objekte überführen."""
    if not raw:
        return []
    out = []
    for w in raw:
        out.append(GridFeeWindow(
            ct_kwh=float(w["ct_kwh"]),
            hours=_parse_hours(w.get("hours")),
            months=[int(m) for m in w["months"]] if w.get("months") else None,
            date_from=(str(w["date_from"]) if w.get("date_from") else None),
            date_to=(str(w["date_to"]) if w.get("date_to") else None),
            weekdays=[int(d) for d in w["weekdays"]] if w.get("weekdays") else None,
        ))
    return out


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
        run_offset_seconds=int(g.get("run_offset_seconds", 10)),
    )
    if 60 % general.slot_minutes != 0:
        raise ValueError("slot_minutes muss ein Teiler von 60 sein (z.B. 5,10,15,30,60).")

    inf = raw["influxdb"]
    signals = {name: SignalSpec.from_dict(spec) for name, spec in inf["signals"].items()}
    outputs = dict(inf["outputs"])
    outputs.setdefault("savings", "ems_savings")
    outputs.setdefault("drift", "ems_drift")
    influxdb = InfluxConfig(
        version=int(inf["version"]),
        v1=inf.get("v1", {}),
        v2=inf.get("v2", {}),
        signals=signals,
        outputs=outputs,
    )
    if influxdb.version not in (1, 2):
        raise ValueError("influxdb.version muss 1 oder 2 sein.")

    fi = raw.get("feed_in", {})
    feed_in = FeedInConfig(
        mode=fi.get("mode", "fixed"),
        fixed_ct_kwh=float(fi.get("fixed_ct_kwh", 8.0)),
        zero_at_negative_price=bool(fi.get("zero_at_negative_price", False)),
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
        ac_charge_efficiency=(float(hb["ac_charge_efficiency"])
                              if hb.get("ac_charge_efficiency") is not None else None),
        max_charge_w=(float(hb["max_charge_w"]) if hb.get("max_charge_w") is not None else None),
    )

    inv = raw["inverter"]
    inverter = InverterConfig(
        max_ac_power_w=float(inv["max_ac_power_w"]),
        max_export_w=(float(inv["max_export_w"])
                      if inv.get("max_export_w") is not None else None),
    )

    v = raw.get("vehicle", {})
    vehicle = VehicleConfig(
        enabled=bool(v.get("enabled", False)),
        capacity_wh=float(v.get("capacity_wh", 0)),
        max_charge_w=float(v.get("max_charge_w", 0)),
        min_charge_w=float(v.get("min_charge_w", 0)),
        min_soc_percent=float(v.get("min_soc_percent", 0)),
        target_soc_percent=float(v.get("target_soc_percent", 80)),
        departure_time=_parse_time(v.get("departure_time", "07:00")),
        departure_times=parse_departure_times(
            v.get("departure_times"), _parse_time(v.get("departure_time", "07:00"))),
        charge_efficiency=float(v.get("charge_efficiency", 0.92)),
        taper_start_soc_percent=float(v.get("taper_start_soc_percent", 100.0)),
    )

    o = raw.get("optimization", {})
    optimization = OptimizationConfig(
        terminal_soc_value=o.get("terminal_soc_value", "auto"),
        cycle_penalty_ct_kwh=float(o.get("cycle_penalty_ct_kwh", 0.1)),
        solver_time_limit_s=int(o.get("solver_time_limit_s", 60)),
        solver_threads=int(o.get("solver_threads", 0)),
        car_switch_penalty_ct=float(o.get("car_switch_penalty_ct", 5.0)),
        car_target_penalty_ct_kwh=float(o.get("car_target_penalty_ct_kwh", 200.0)),
        export_priority_ct_kwh=float(o.get("export_priority_ct_kwh", 0.0)),
        allow_grid_discharge=bool(o.get("allow_grid_discharge", False)),
        charge_strategy=str(o.get("charge_strategy", "auto")),
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
        half_life_days=float(f.get("half_life_days", 120.0)),
        intraday_enabled=bool(f.get("intraday_enabled", True)),
        intraday_window_hours=float(f.get("intraday_window_hours", 3.0)),
        intraday_decay_hours=float(f.get("intraday_decay_hours", 6.0)),
        intraday_max_factor=float(f.get("intraday_max_factor", 1.5)),
        price_damping=float(f.get("price_damping", 0.3)),
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
        schedule_fields=(list(m["schedule_fields"])
                         if m.get("schedule_fields") is not None
                         else MqttConfig().schedule_fields),
        schedule_max_hours=int(m.get("schedule_max_hours", 24)),
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

    sav = raw.get("savings", {})
    savings = SavingsConfig(
        enabled=bool(sav.get("enabled", True)),
        state_path=sav.get("state_path", "./savings_state.json"),
    )

    mon = raw.get("monitoring", {})
    monitoring = MonitoringConfig(
        drift_enabled=bool(mon.get("drift_enabled", True)),
        drift_window_hours=float(mon.get("drift_window_hours", 12.0)),
        drift_alert_percent=float(mon.get("drift_alert_percent", 8.0)),
    )

    rep = raw.get("report", {})
    report = ReportConfig(
        enabled=bool(rep.get("enabled", False)),
        mail_to=rep.get("mail_to", ""),
        snapshot_path=rep.get("snapshot_path", "./last_run_debug.json"),
    )

    w = raw.get("weather", {})
    weather = WeatherConfig(
        enabled=bool(w.get("enabled", False)),
        latitude=float(w.get("latitude", 0.0)),
        longitude=float(w.get("longitude", 0.0)),
        past_days=int(w.get("past_days", 92)),
        forecast_days=int(w.get("forecast_days", 4)),
    )

    t = raw.get("tariff", {})
    tariff = TariffConfig(
        enabled=bool(t.get("enabled", False)),
        type=str(t.get("type", "dynamic")),
        fixed_ct_kwh=float(t.get("fixed_ct_kwh", 30.0)),
        bidding_zone=str(t.get("bidding_zone", "DE-LU")),
        markup_percent=float(t.get("markup_percent", 0.0)),
        markup_ct_kwh=float(t.get("markup_ct_kwh", 0.0)),
        levies_ct_kwh=float(t.get("levies_ct_kwh", 0.0)),
        vat_percent=float(t.get("vat_percent", 19.0)),
        grid_fee_mode=str(t.get("grid_fee_mode", "included")),
        grid_fee_ct_kwh=float(t.get("grid_fee_ct_kwh", 0.0)),
        grid_fee_default_ct_kwh=float(t.get("grid_fee_default_ct_kwh", 0.0)),
        grid_fee_windows=parse_grid_fee_windows(t.get("grid_fee_windows")),
        history_backfill_days=int(t.get("history_backfill_days", 400)),
    )
    if tariff.type not in ("dynamic", "fixed"):
        raise ValueError("tariff.type muss 'dynamic' oder 'fixed' sein.")
    if tariff.grid_fee_mode not in ("static", "included", "14a"):
        raise ValueError("tariff.grid_fee_mode muss 'static', 'included' oder '14a' sein.")

    sc = raw.get("solcast", {})
    solcast = SolcastConfig(
        enabled=bool(sc.get("enabled", False)),
        combine=str(sc.get("combine", "sum")),
        calls_per_key_per_day=int(sc.get("calls_per_key_per_day", 10)),
        sources=[SolcastSource(api_key=str(s["api_key"]),
                               resource_id=str(s["resource_id"]))
                 for s in (sc.get("sources") or [])],
        window_start_hour=int(sc.get("window_start_hour", 5)),
        window_end_hour=int(sc.get("window_end_hour", 22)),
    )
    if solcast.combine not in ("sum", "mean"):
        raise ValueError("solcast.combine muss 'sum' oder 'mean' sein.")

    e = raw.get("e3dc_rscp", {})
    e3dc_rscp = E3DCRscpConfig(
        enabled=bool(e.get("enabled", False)),
        host=e.get("host", ""),
        username=e.get("username", ""),
        password=e.get("password", ""),
        key=e.get("key", ""),
        read_live=bool(e.get("read_live", True)),
        control_enabled=bool(e.get("control_enabled", False)),
        grid_sign=float(e.get("grid_sign", 1.0)),
        batt_sign=float(e.get("batt_sign", 1.0)),
        history_source=bool(e.get("history_source", False)),
        history_db_path=e.get("history_db_path", "./e3dc_history.sqlite"),
        history_backfill_days=int(e.get("history_backfill_days", 730)),
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
        savings=savings,
        monitoring=monitoring,
        report=report,
        e3dc_rscp=e3dc_rscp,
        weather=weather,
        tariff=tariff,
        solcast=solcast,
    )
