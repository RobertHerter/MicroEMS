"""Konfiguration laden und validieren.

Die Konfiguration wird aus einer YAML-Datei geladen und in typisierte
Dataclasses überführt, damit der restliche Code mit klaren Objekten arbeitet
statt mit rohen Dictionaries.
"""
from __future__ import annotations

import os
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
    # True: Horizontende bis zur darauf folgenden Mitternacht erweitern.
    # Liegt das reguläre Ende bereits auf 00:00, wird NICHT weiter verlängert.
    optimization_horizon_round_to_midnight: bool = False
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
    # False = keine InfluxDB (No-op-Repository): Betrieb komplett ohne InfluxDB,
    # Eingangsdaten kommen dann lokal/extern (RSCP, Ingest-API, Open-Meteo, ...).
    enabled: bool = True


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
    # Zellschonung: sanfte Strafe je kWh·h, die der SoC OBERHALB der Schwelle
    # gehalten wird (langes Verweilen bei ~100 % altert die Zellen). 0 = aus
    # (Default, Verhalten exakt unverändert). Getrennt von der Zyklenstrafe.
    full_hold_penalty_ct_kwh: float = 0.0
    full_hold_soc_threshold_percent: float = 95.0

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
    # Bezugsgrenze am Netzanschluss (Hausanschluss-Sicherung), z.B.
    # 3 x 35 A x 230 V = 24150 W. Harte Obergrenze für den geplanten
    # Netzbezug je Slot - verhindert, dass Netzladen + Auto + Lasten
    # zusammen mehr Anschlussleistung einplanen, als die Sicherung trägt.
    # None = keine Begrenzung.
    max_import_w: Optional[float] = None


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
class LoadStage:
    """Eine Schaltstufe einer steuerbaren Last (z.B. eine der Pool-Wärmepumpen)."""
    name: str
    power_w: float                       # elektrische Leistung (W)
    heat_w: float = 0.0                  # thermische Leistung (W), nur type=thermal
    requires: Optional[str] = None       # Name einer anderen Stufe (Kopplung: nur an, wenn jene an)
    control_topic: Optional[str] = None  # ausgehender Schaltbefehl (0/1) je Slot
    # Optionale echte Rückmeldung vom Gerät/Homey. feedback_topic liefert
    # on/off; power_topic eine gemessene elektrische Leistung in W. Liegen
    # beide vor, entscheidet die Leistung oberhalb feedback_on_threshold_w.
    feedback_topic: Optional[str] = None
    power_topic: Optional[str] = None
    feedback_on_threshold_w: float = 50.0


@dataclass
class ControllableLoad:
    """Steuerbare/verschiebbare Last. type="deferrable": muss `runtime_minutes`
    im Zeitfenster laufen (Leistung konstant oder als 15-min-Kurve). type="thermal":
    thermischer Speicher (z.B. Pool) mit Temperatur als MILP-Zustand, geheizt über
    `stages`, gehalten im Band [min_c, max_c]. Der Optimierer legt die Laufzeit in
    die günstigsten/PV-reichsten Slots."""
    name: str
    type: str = "deferrable"
    enabled: bool = True
    switch_penalty_ct: float = 5.0       # Malus je Einschaltvorgang (Anti-Takten)
    control_topic: Optional[str] = None  # ausgehender Schaltbefehl/Start-Topic
    # -- deferrable --
    power_w: float = 0.0
    power_profile_w: Optional[list] = None   # 15-min-Kurve (überschreibt power_w)
    runtime_minutes: float = 0.0
    window_from_hour: int = 0
    window_to_hour: int = 24
    # Deadline (h ab JETZT), bis wann die Laufzeit abgeschlossen sein muss.
    # Ohne Deadline würde der Optimierer den Lauf für Cent-Bruchteile ans Ende
    # des 48-h-Horizonts schieben ("Waschmaschine erst übermorgen") - wer eine
    # Last einschaltet, will sie zeitnah. 0 = keine Deadline (alte Semantik).
    deadline_hours: float = 24.0
    requires: Optional[str] = None
    # -- thermal --
    volume_l: float = 0.0                # Wasservolumen -> Wärmekapazität
    target_c: float = 28.0
    min_c: float = 27.0
    max_c: float = 29.0
    loss_w_per_k: float = 0.0            # Wärmeverlust je K (T_pool - T_außen)
    # Solarer Wärmeeintrag = surface_m2 * solar_absorption * Globalstrahlung (W/m²,
    # Open-Meteo). 0 = deaktiviert (kein Eintrag, unverändertes Verhalten).
    surface_m2: float = 0.0              # Wasseroberfläche (m²)
    solar_absorption: float = 0.75       # effektiver Wirkungsgrad der Fläche
    # Heizen ohne Netzbezug (thermische Lasten): die Stufen dürfen nur in Slots
    # laufen, in denen KEIN Netzbezug stattfindet - PV-Überschuss UND Akku
    # dürfen die WP decken, Netzstrom nie. In trüben Phasen mit leerem Akku
    # wird dann nicht geheizt, auch wenn die Temperatur unter min_c sackt
    # (weiches Band). false = der Optimierer darf auch Netzstrom nutzen, wenn
    # es wirtschaftlich ist. (YAML-Alias: pv_surplus_only, ältere Configs.)
    no_grid_import: bool = False
    # Last hat einen EIGENEN Thermostat-Cutoff (z.B. Pool-WP): das EMS-Signal ist
    # dann eine Heiz-FREIGABE, kein Zwang. Bei Ist-Temperatur >= target_c bleibt
    # die Freigabe AN (der Thermostat hält die WP ohnehin aus) - weniger Schalt-
    # spiele, und bei unerwartetem Temperaturabfall heizt die WP sofort. "Aus"
    # wird nur gesendet, wenn Heizen aktiv verhindert werden soll (T < target_c
    # und kein Heiz-Slot geplant). target_c sollte dem WP-eigenen Sollwert
    # entsprechen. false = Signal folgt 1:1 dem Heizplan (wie bisher).
    thermostat: bool = False
    temp_signal: Optional[str] = None    # InfluxDB-Signal der Ist-Temperatur (für T[0])
    stages: list = field(default_factory=list)   # [LoadStage]
    season_from: Optional[str] = None    # "MM-DD" (nur in Saison aktiv)
    season_to: Optional[str] = None
    # Entscheidungsraster (min): Schaltentscheidungen nur alle X Minuten statt je
    # Slot. Träge thermische Lasten (Pool) brauchen keine 15-min-Entscheidungen -
    # 60 min viertelt die Binärvariablen (Solver-Laufzeit!) und schont die WP-
    # Kompressoren. 0 = Default (thermal 60, deferrable = Slotraster).
    decision_minutes: int = 0
    # Rolling-Horizon für thermische Lasten: nur die nächsten X Stunden werden
    # als echte Ein/Aus-Binärentscheidungen modelliert. Weiter entfernte Blöcke
    # sind ein mittlerer Duty-Cycle [0..1]; sie werden vor ihrer Ausführung bei
    # jedem Folgelauf wieder binär. 0 = gesamter Horizont binär.
    binary_horizon_hours: float = 12.0
    feedback_required: bool = False
    feedback_max_age_minutes: float = 20.0
    # Retained/change-only-Sensoren (z.B. Shelly-Leistungsmessung) publizieren nur
    # bei Wertänderung. Bleibt der Wert konstant (WP aus -> 0 W), kommt keine
    # neue Nachricht und die Rückmeldung würde nach feedback_max_age_minutes
    # fälschlich als "veraltet" gelten. Mit true gilt der zuletzt empfangene
    # (retained) Wert als gültig, SOLANGE die MQTT-Verbindung steht - "veraltet"
    # dann nur bei Verbindungsabbruch bzw. wenn nie ein Wert kam.
    feedback_hold_while_connected: bool = False

    @property
    def capacity_wh_per_k(self) -> float:
        # Wasser: 1.163 Wh/(L·K)
        return self.volume_l * 1.163


@dataclass
class OptimizationConfig:
    terminal_soc_value: Any = "auto"      # "auto" | float (ct/kWh)
    cycle_penalty_ct_kwh: float = 0.1
    solver: str = "cbc"                   # "cbc" oder "highs"
    solver_time_limit_s: int = 60
    # CBC-Threads. 0 = automatisch (CPU-Kerne - 1).
    solver_threads: int = 0
    # Relative MIP-Optimalitätslücke: der Solver stoppt, sobald die gefundene
    # Lösung beweisbar innerhalb dieses Anteils des Optimums liegt (0.01 = 1 %).
    # Der teure Teil eines MILP ist meist NICHT das Finden einer guten Lösung,
    # sondern der BEWEIS ihrer Optimalität – an "peak"-Tagen (Einspeise-Linien +
    # p10-Slacks + Pool-Binärvariablen) kann das Minuten dauern. Eine kleine Lücke
    # kappt diesen Beweis-Endlauf; bei Kosten von wenigen Euro sind 1 % ein paar
    # Cent und die Steuerentscheidungen praktisch identisch. 0 = exakt (kann lange
    # dauern).
    solver_mip_gap: float = 0.01
    # Absolute Optimalitätslücke (ct): Stopp, sobald die Lösung beweisbar
    # höchstens X ct vom Optimum entfernt ist. Wichtig als Ergänzung zur
    # RELATIVEN Lücke: enthält das Ziel große konstante Terme (z.B. Komfort-
    # Malus), wären "1 %" plötzlich viele Euro. Nicht zu groß wählen: die
    # Toleranz ist auch das Budget für sinnlose Mikro-Artefakte im Plan
    # (Abregel-Reste, Kleinst-Eingriffe). 0 = aus.
    solver_mip_gap_abs_ct: float = 25.0
    # Malus (ct) je Einschaltvorgang der Wallbox: verhindert, dass das Auto
    # bei zappeligen Preisen ständig ein-/ausgeschaltet wird (Schützverschleiß).
    # 0 = aus.
    car_switch_penalty_ct: float = 5.0
    # Malus (ct) je begonnenem Halteblock innerhalb einer Restlastphase sowie
    # für materielle Teilentladung bei gleichzeitigem Netzbezug. Verhindert
    # auch mehrslotige Drosselungen für Cent-Bruchteile, ohne notwendige
    # Lastdeckung generell zu verbieten.
    battery_switch_penalty_ct: float = 1.0
    # Malus (ct/kWh) auf Netzbezug, obwohl nach dem Slot noch nutzbare
    # Akkuenergie vorhanden ist. Reicht der Akku bis zum nächsten erwarteten
    # PV-Überschuss, ist Netzbezug durch Halten verboten. Andernfalls darf er
    # nur bei einem mindestens so viel höheren Folgepreis verschoben werden.
    # Bezug am Mindest-SoC bleibt zulässig. 0 = aus.
    battery_hold_penalty_ct_kwh: float = 5.0
    # Strafe (ct/kWh) je fehlender kWh zum Auto-Ziel-SoC bei Abfahrt. Das Ziel
    # ist eine WEICHE Nebenbedingung: ist es unerreichbar, lädt der Plan so
    # viel wie möglich statt komplett auf 'auto' zurückzufallen.
    car_target_penalty_ct_kwh: float = 200.0
    # Hausanschluss-Grenze (max_import_w) als WEICHE Nebenbedingung: sehr hohe
    # Strafe je kWh über der Grenze. So bleibt der Plan auch bei einer einzelnen
    # physikalisch unvermeidbaren Lastspitze lösbar (der betroffene Slot zahlt
    # die Strafe und wird als Alarm gemeldet), statt den GESAMTEN Horizont auf
    # 'auto ohne Eingriff' zu verwerfen. Muss jeden realen Preis-Spread klar
    # übersteigen, damit die Grenze nie zur Arbitrage überschritten wird.
    grid_overload_penalty_ct_kwh: float = 1000.0
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
    #   "late" = maximalen Ziel-SoC aus PV erreichen, aber so spät wie möglich.
    #   "auto" = pro Tag automatisch: viel PV-Überschuss -> peak, sonst asap. (Standard)
    charge_strategy: str = "auto"
    # Late-Modus: hohe weiche Strafe je fehlender kWh am Ende des nutzbaren
    # PV-Fensters. Das Ziel bleibt dadurch auch bei zu wenig PV lösbar.
    late_target_penalty_ct_kwh: float = 200.0
    # Zeitgewichteter Malus auf frühe PV-Ladung im Late-Modus. 0 deaktiviert
    # die zeitliche Verschiebung, ohne das Max-SoC-Ziel abzuschalten.
    late_charge_delay_ct_kwh: float = 5.0
    # Nur bei feed_in.zero_at_negative_price: Malus auf Einspeisung in Slots
    # mit negativem Bezugspreis und 0 ct Vergütung. Er muss größer als der
    # Late-Zeitmalus sein, damit Laden/Abregeln Vorrang vor Einspeisen hat.
    negative_price_export_penalty_ct_kwh: float = 10.0
    # Auto -> Peak, wenn der pessimistische Tagesueberschuss mindestens diese
    # Obergrenze der nutzbaren Akkukapazitaet erreicht. Der tatsaechliche
    # Schwellwert kann bei voraussichtlich teilgeladenem Akku kleiner sein.
    auto_peak_threshold_percent: float = 85.0
    # Glatte saisonale Abstimmung zwischen Winter- und Sommerwerten. Deaktiviert
    # bleibt auto_peak_threshold_percent/peak_charge_ramp_penalty_ct_kw wirksam.
    seasonal_peak_tuning: bool = False
    auto_peak_threshold_winter_percent: float = 95.0
    auto_peak_threshold_summer_percent: float = 75.0
    peak_charge_ramp_penalty_winter_ct_kw: float = 0.5
    peak_charge_ramp_penalty_summer_ct_kw: float = 2.0
    # Zusaetzliche freie Kapazitaet als SoC-/Lastprognose-Reserve bei der
    # dynamischen Auto-Schwelle (Prozent der nutzbaren Akkukapazitaet).
    auto_peak_soc_reserve_percent: float = 10.0
    # Bei einem sehr breiten Solcast-Band darf ein eindeutig starker
    # Erwartungswert p10 plausibilisieren: p10 muss mindestens diesen Anteil
    # der dynamischen Schwelle erreichen ...
    auto_peak_p10_floor_percent: float = 60.0
    # ... und der Erwartungsueberschuss mindestens diesen Anteil der Schwelle.
    auto_peak_expected_override_percent: float = 150.0
    # Gewicht, wie stark die Einspeise-Linie L minimiert wird.
    peak_charge_weight: float = 30.0
    # Weicher Glaettungsmalus (ct je kW Leistungssprung) fuer PV-Akkuladung an
    # Peak-Tagen. Verhindert einzelne Voll-Ladeslots zwischen Linien-Slots.
    peak_charge_ramp_penalty_ct_kw: float = 2.0
    # Harte Obergrenze fuer den ANSTIEG der Peak-Ladeleistung je Slot. Ein
    # Abfall bei vollem Akku bleibt frei. 0 = keine harte Grenze.
    peak_charge_max_ramp_w: float = 1500.0
    # Kleiner Malus (ct je kW Abweichung) gegen Aenderungen am zuletzt
    # publizierten Fahrplan. Wirkt nur innerhalb plan_stability_hours und wird
    # von materiellen wirtschaftlichen Aenderungen ueberstimmt. 0 = aus.
    plan_change_penalty_ct_kw: float = 0.25
    plan_stability_hours: float = 4.0
    # WR-Sockellast: fixer Verlust (W), der bei JEDEM Entlade-Slot dem Akku
    # entnommen wird (Wechselrichter-Eigenverbrauch). Macht Kleinstleistungs-
    # Entladen unwirtschaftlich. 0 = aus.
    standby_discharge_w: float = 0.0
    # Mindest-Entladeleistung (W): Entladen ist 0 ODER >= diesem Wert (semi-
    # kontinuierlich) - verhindert stundenlanges Trickle-Entladen. 0 = aus.
    min_discharge_w: float = 0.0
    # Leistungs-Reserve (%): plant Akku-Lade/-Entladeleistung und WR-Durchsatz
    # nur bis (100 - x) % der Nennwerte. Lässt dem Echtzeit-Regler Reserve für
    # Sub-Slot-Lastspitzen (15-min-Mittelung sieht diese nicht). Hinweis: kann an
    # sonnigen Tagen minimale PV-Abregelung verursachen. 0 = aus.
    power_headroom_percent: float = 0.0
    # Abend-Reserve: optionaler Mindest-SoC (% der Gesamtkapazität), den der Akku
    # im Fenster [start, end) mindestens halten SOLL, damit er nicht vor der
    # teuren Abendspitze leerläuft (Ursache der sporadischen "19:00-Entladesperre":
    # der Akku war abends knapp). WEICHE Nebenbedingung – ein Malus (ct/kWh) je
    # fehlender kWh unter der Reserve, KEINE harte Grenze -> nie infeasible und von
    # einem echten Preisvorteil überstimmbar. Das Fenster schützt nur den VORLAUF;
    # NACH end ist die Reserve frei, sodass der Akku gezielt in die Spitze entlädt.
    # Über-Mitternacht-Fenster (start > end) werden unterstützt. 0 = aus.
    evening_reserve_soc_percent: float = 0.0
    evening_reserve_start: time = time(16, 0)
    evening_reserve_end: time = time(20, 0)
    evening_reserve_penalty_ct_kwh: float = 3.0
    # Adaptive Abend-Reserve: Höhe und Fenster NICHT fest, sondern je Tag aus den
    # Daten. Höhe = Energie, um die Restlast (Last - PV) während der abendlichen
    # Preisspitze aus dem Akku zu decken (gedeckelt auf die nutzbare Kapazität);
    # Fenster = ab evening_reserve_hold_from_hour bis zum Beginn der Spitze (danach
    # frei -> Entladung in die Spitze). Übersteuert soc_percent/start/end. Der Peak
    # gilt nur, wenn das Abend-Preismaximum >= price_factor x Tages-Median liegt
    # (an flachen Tagen keine Reserve). Braucht evening_reserve_penalty_ct_kwh > 0.
    evening_reserve_auto: bool = False
    evening_reserve_hold_from_hour: int = 11
    evening_reserve_price_factor: float = 1.15


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
    # Konservativer Betriebswert, falls überhaupt keine verwertbare
    # Verbrauchshistorie vorhanden ist (nie mit 0 W optimieren).
    fallback_load_w: float = 1500.0
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
    # Alte Sammelparameter bleiben als Lade-/Abwärtskompatibilitäts-Fallback.
    intraday_window_hours: float = 3.0     # Fenster für das Ist/Prognose-Verhältnis
    intraday_decay_hours: float = 6.0      # Halbwertszeit des Abklingens
    intraday_max_factor: float = 1.5       # Faktor-Begrenzung (und 1/x nach unten)
    # Last und PV reagieren sehr verschieden: Lastspitzen brauchen Robustheit,
    # PV-Rampen an Sonnenauf-/untergang ein Mindestleistungs-Gate und kürzeres
    # Gedächtnis. Totzone unterdrückt Rauschen, max_step begrenzt Sprünge je Lauf.
    intraday_load_window_hours: float = 3.0
    intraday_pv_window_hours: float = 3.0
    intraday_load_decay_hours: float = 3.0
    intraday_pv_decay_hours: float = 1.5
    intraday_load_max_factor: float = 1.5
    intraday_pv_max_factor: float = 1.5
    intraday_load_deadband: float = 0.10
    intraday_pv_deadband: float = 0.10
    intraday_load_max_step: float = 0.10
    intraday_pv_max_step: float = 0.10
    intraday_pv_min_power_w: float = 1000.0
    # Geschätzte (noch unbekannte) Folgetag-Preise zur Mitte stauchen:
    # p' = m + (p - m) * (1 - price_damping). Verhindert, dass auf
    # prognostizierte Preistäler/-spitzen spekuliert wird. 0 = aus, 1 = flach.
    price_damping: float = 0.3
    # Prognosemethode: "similar_days" (Ähnliche-Tage-Mittelung, Standard) oder "ml" (Machine Learning mit HistGradientBoostingRegressor).
    method: str = "similar_days"
    # Empirisches Lastband aus der Streuung vergleichbarer historischer Slots.
    load_uncertainty_enabled: bool = True
    load_uncertainty_low_quantile: float = 0.10
    load_uncertainty_high_quantile: float = 0.90
    load_uncertainty_min_samples: int = 12


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
    api_enabled: bool = True
    # E3/DC-Livewerte im Dashboard. Der Server begrenzt die RSCP-Abfragen
    # unabhängig von der Zahl geöffneter Browser auf höchstens eine je Intervall.
    # 0 deaktiviert die Live-Anzeige und den Endpunkt /api/live.json.
    live_refresh_seconds: float = 5.0
    username: str = ""
    password: str = ""
    # POST-Ingest-Endpunkte (/api/ingest/<kind>): Live- und Historienwerte extern
    # einspielen (Betrieb ohne RSCP/InfluxDB). Auth = username/password (Basic).
    ingest_enabled: bool = False
    # Interaktive Steuerung im Dashboard (/api/control/*): Lasten an/aus + Parameter,
    # Optimierungsmodus, manuelles Laden/Entladen. Auth = username/password (Basic).
    # Standard AUS – manuelles Laden/Entladen greift real in den Akku ein.
    controls_enabled: bool = False
    # Rate-Limit für /api/control/* (Steuerbefehle je Minute, Sliding Window).
    # Schützt den realen Steuerpfad vor versehentlichem/böswilligem Befehls-
    # sturm. 0 = unbegrenzt.
    control_rate_limit_per_min: int = 60


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
    solver_runtime_alert_seconds: float = 60.0
    solver_runtime_factor: float = 3.0
    solver_runtime_baseline_runs: int = 12
    execution_audit_enabled: bool = True
    execution_grid_tolerance_w: float = 1500.0
    execution_battery_tolerance_w: float = 1500.0
    execution_soc_tolerance_percent: float = 5.0
    execution_alert_consecutive: int = 2
    # E3/DC stellt abgeschlossene 15-min-Zaehleraggregate erst zeitversetzt
    # bereit. Das Audit prueft daher den Slot, dessen Start mindestens so lange
    # zurueckliegt. 75 min entspricht der beobachteten E3/DC-Verzoegerung.
    execution_meter_delay_minutes: float = 75.0
    # Schnelle vorlaeufige Ausfuehrungspruefung aus E3/DC-Livewerten. Kurze
    # Luecken werden interpoliert, die Bewertung nutzt einen robusten Median.
    execution_live_enabled: bool = True
    execution_live_sample_seconds: float = 5.0
    execution_live_window_seconds: float = 60.0
    execution_live_max_gap_seconds: float = 15.0
    execution_live_settle_seconds: float = 30.0
    execution_live_consecutive: int = 3
    # Karenz (min) nach dem (Neu-)Start: solange wird das Ausführungs-Audit
    # ausgesetzt, weil die RSCP-Steuerung erst wieder gesetzt/eingependelt werden
    # muss (sonst falsche "Planabweichung Akku" direkt nach dem Start). 0 = aus.
    execution_audit_startup_grace_minutes: float = 5.0
    # Netz als Ausführungsfehler werten? Standard AUS: die Netzleistung ist ein
    # reines Bilanz-Residuum (Netz = Last - PV + Akku). Weicht PV/Last von der
    # Prognose ab (z.B. PV über Prognose im Peak), gleicht der E3DC die Differenz
    # übers Netz aus - das ist KEIN Steuerfehler, sondern Prognoseabweichung, die
    # der Akku-Check ohnehin abbildet. Netz bleibt informativ im Report.
    execution_audit_grid: bool = False
    # Rein diagnostischer Vergleich aller Optimierungsmodi nach jedem Lauf.
    shadow_compare_enabled: bool = True
    shadow_recommend_min_savings_eur: float = 0.05
    # Alarm, wenn seit X Minuten kein Zyklus mehr ERFOLGREICH durchlief (Dienst
    # hängt, RSCP/Netz klemmt) – der systemd-Watchdog fängt nur Totalausfälle.
    # 0 = auto (2,5 × run_interval_minutes). Negativ = aus.
    cycle_staleness_alert_minutes: float = 0.0


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
    # Abruf-Verteilung:
    #   "daytime" = hauptsächlich tagsüber, gleichmäßig über das lokale Fenster
    #               [window_start_hour, window_end_hour) (PV-Nowcasting im Fokus)
    #   "24h"     = rund um die Uhr gleichmäßig (hält auch den Folgetag frisch)
    distribution: str = "daytime"
    window_start_hour: int = 5            # nur bei distribution="daytime"
    window_end_hour: int = 22


@dataclass
class PvArray:
    """Ein Panel-Feld mit eigener Ausrichtung. azimuth in pvlib-Konvention:
    0=Nord, 90=Ost, 180=Süd, 270=West. tilt = Neigung gegen die Horizontale
    (0=flach, 90=senkrecht). kwp = installierte DC-Nennleistung (kWp)."""
    name: str
    kwp: float
    tilt: float
    azimuth: float


@dataclass
class PvModelConfig:
    """Freie PV-Ertragsprognose mit pvlib + Open-Meteo (Alternative zu Solcast,
    kein API-Key/Kontingent). Nutzt weather.latitude/longitude. Mehrere Arrays
    (Ausrichtungen) werden je Slot summiert - dieselbe pv_forecast-Tabelle und
    derselbe Kalibrierpfad (kalibrierung.py) wie bei Solcast."""
    enabled: bool = False
    # shadow: pvlib-Prognose NUR zum Vergleich mitrechnen (Dashboard-Overlay
    # Solcast vs. pv_model), OHNE den Optimierer zu beeinflussen. So lässt sich
    # das Modell im Live-Betrieb neben Solcast bewerten, bevor man umschaltet.
    shadow: bool = False
    arrays: list = field(default_factory=list)     # [PvArray]
    # PVWatts: Temperaturkoeffizient der Leistung (1/°C, negativ) und pauschale
    # Systemverluste (Verkabelung, Wechselrichter, Verschmutzung; 0..1 = Anteil,
    # der VERLOREN geht). Ab Werk konservative Standardwerte.
    temp_coeff_per_c: float = -0.0035
    system_loss: float = 0.14
    # Heuristische Unsicherheitsbänder um den Punktwert (pvlib liefert - anders
    # als Solcast - keine echten Quantile). p10 = pv*(1-low), p90 = pv*(1+high).
    # p10 dimensioniert die Einspeise-Linie an Peak-Tagen -> konservativ tief.
    p10_uncertainty: float = 0.35
    p90_uncertainty: float = 0.15


@dataclass
class PvSourceSelectionConfig:
    """Automatische Wahl zwischen Solcast und pvlib-Schattenmodell."""
    enabled: bool = False
    lookback_days: int = 30
    min_samples: int = 96
    min_improvement_percent: float = 2.0


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
    # Anlagengrenzen (WR-/Lade-/Entladeleistung, Entlade-Startschwelle) beim
    # Start vom Gerät lesen und die Config-Werte überschreiben (immer korrekt,
    # auch nach Firmware-/Hardwareänderung). Kapazität/SoC bleiben aus der Config.
    autoread_limits: bool = False
    control_enabled: bool = False        # Steuerung per RSCP (greift real ein!)
    # Gesendete Lade-/Entladelimits unmittelbar über get_power_settings vom
    # Gerät zurücklesen. Abweichung/Lesefehler löst einen Steueralarm aus.
    verify_control: bool = True
    control_verify_tolerance_w: float = 100.0
    control_alarm_repeat_minutes: float = 60.0
    # Optionale permanente PV-Leistungsbegrenzung. Separat aktivieren, da nicht
    # jeder E3DC-Benutzer EMS_REQ_SET_DERATE_PERCENT schreiben darf.
    curtailment_control_enabled: bool = False
    curtailment_normal_percent: float = 100.0
    curtailment_verify_tolerance_percent: float = 1.0
    grid_sign: float = 1.0               # Vorzeichen Netz (+ = Bezug)
    batt_sign: float = 1.0               # Vorzeichen Akku (+ = Laden)
    # Verbrauchsprognose aus lokaler SQLite (per RSCP gefüllt) statt InfluxDB.
    history_source: bool = False
    history_db_path: str = "./e3dc_history.sqlite"
    history_backfill_days: int = 730     # Tiefe des einmaligen Backfills
    # E3DC-DB-Werte kurz nach Slotende können noch unvollständig sein. Nur
    # gereifte Fenster lesen und einige Stunden überlappend erneut schreiben.
    history_settle_minutes: int = 60
    history_overlap_hours: int = 3


@dataclass
class ReportConfig:
    # Debug-Report-Button im Dashboard: lädt den Schnappschuss herunter und
    # öffnet das Mailprogramm vorausgefüllt (Anhang manuell). Kein SMTP nötig.
    enabled: bool = False
    mail_to: str = ""                    # Vorbelegung im Mailprogramm
    snapshot_path: str = "./last_run_debug.json"


@dataclass
class RecalcConfig:
    """Sofort-Neuberechnung bei großer Live-Abweichung vom Plan. Zwischen den
    15-Min-Marken wird periodisch die gemessene Netzleistung mit dem Sollwert des
    laufenden Slots verglichen; überschreitet die Abweichung die Schwelle (Wolke
    bricht PV ein, großer Verbraucher springt an), rechnet der Dienst sofort neu,
    statt auf die nächste Marke zu warten. Ergänzt die Intraday-Korrektur."""
    enabled: bool = False
    deviation_w: float = 3000.0
    check_seconds: float = 90.0


@dataclass
class SanityConfig:
    """Plausibilitäts-Grenzen für externe Eingaben (ems/sanity.py). Greift nur
    bei klar unmöglichen Werten und schützt so den Echtbetrieb vor einem
    einzelnen API-Ausreißer."""
    enabled: bool = True
    # Preis-Absolutgrenzen (ct/kWh); außerhalb -> durch Median ersetzt.
    price_min_ct: float = -100.0
    price_max_ct: float = 200.0
    # Spike-Erkennung: |Preis| über diesem Vielfachen des robusten Medians ->
    # ebenfalls ersetzt. 0 = aus.
    price_spike_median_factor: float = 10.0
    # PV-Obergrenze (W). 0 = automatisch aus installierter kWp (pv_model), sonst
    # keine Grenze. Negative PV wird immer auf 0 gesetzt.
    pv_max_w: float = 0.0
    # Hauslast-Obergrenze (W). 0 = keine Grenze. Negative Last -> immer 0.
    load_max_w: float = 0.0


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
    pv_model: PvModelConfig = field(default_factory=PvModelConfig)
    pv_source_selection: PvSourceSelectionConfig = field(
        default_factory=PvSourceSelectionConfig)
    sanity: SanityConfig = field(default_factory=SanityConfig)
    recalc: RecalcConfig = field(default_factory=RecalcConfig)
    controllable_loads: list = field(default_factory=list)   # [ControllableLoad]


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


def _load_slug(name: str) -> str:
    """Wie ems.loads._slug (ohne Import-Zyklus): Name -> Spalten-/Overlay-Schlüssel."""
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "load"


def parse_controllable_loads(raw, overrides: Optional[dict] = None) -> list:
    """controllable_loads aus der YAML-Liste in ControllableLoad-Objekte.

    `overrides` (aus config_overrides.yaml, Schlüssel = Last-Slug) überschreibt
    Dashboard-editierbare Felder (enabled + Kernparameter), da die Basis-Liste
    nicht per Name deep-gemergt werden kann."""
    if not raw:
        return []
    out = []
    for w in raw:
        stages = [LoadStage(
            name=str(s["name"]), power_w=float(s["power_w"]),
            heat_w=float(s.get("heat_w", 0.0)),
            requires=(str(s["requires"]) if s.get("requires") else None),
            # control_topic (neu), mqtt_topic als Lese-Fallback für Altbestände.
            control_topic=(str(s.get("control_topic") or s.get("mqtt_topic"))
                           if (s.get("control_topic") or s.get("mqtt_topic"))
                           else None),
            feedback_topic=(str(s["feedback_topic"])
                            if s.get("feedback_topic") else None),
            power_topic=(str(s["power_topic"])
                         if s.get("power_topic") else None),
            feedback_on_threshold_w=float(
                s.get("feedback_on_threshold_w", 50.0)),
        ) for s in (w.get("stages") or [])]
        prof = w.get("power_profile_w")
        win = w.get("window", {}) or {}
        seas = w.get("season", {}) or {}
        load = ControllableLoad(
            name=str(w["name"]),
            type=str(w.get("type", "deferrable")),
            enabled=bool(w.get("enabled", True)),
            switch_penalty_ct=float(w.get("switch_penalty_ct", 5.0)),
            control_topic=(str(w.get("control_topic") or w.get("mqtt_topic"))
                           if (w.get("control_topic") or w.get("mqtt_topic"))
                           else None),
            power_w=float(w.get("power_w", 0.0)),
            power_profile_w=([float(x) for x in prof] if prof else None),
            runtime_minutes=float(w.get("runtime_minutes", 0.0)),
            window_from_hour=int(win.get("from", w.get("window_from_hour", 0))),
            window_to_hour=int(win.get("to", w.get("window_to_hour", 24))),
            deadline_hours=float(w.get("deadline_hours", 24.0)),
            requires=(str(w["requires"]) if w.get("requires") else None),
            volume_l=float(w.get("volume_l", 0.0)),
            target_c=float(w.get("target_c", 28.0)),
            min_c=float(w.get("min_c", 27.0)),
            max_c=float(w.get("max_c", 29.0)),
            loss_w_per_k=float(w.get("loss_w_per_k", 0.0)),
            surface_m2=float(w.get("surface_m2", 0.0)),
            solar_absorption=float(w.get("solar_absorption", 0.75)),
            thermostat=bool(w.get("thermostat", False)),
            no_grid_import=bool(w.get("no_grid_import",
                                      w.get("pv_surplus_only", False))),
            temp_signal=(str(w["temp_signal"]) if w.get("temp_signal") else None),
            stages=stages,
            season_from=(str(w.get("season_from") or seas.get("from"))
                         if (w.get("season_from") or seas.get("from")) else None),
            season_to=(str(w.get("season_to") or seas.get("to"))
                       if (w.get("season_to") or seas.get("to")) else None),
            decision_minutes=int(w.get("decision_minutes", 0)),
            binary_horizon_hours=float(w.get("binary_horizon_hours", 12.0)),
            feedback_required=bool(w.get("feedback_required", False)),
            feedback_max_age_minutes=float(
                w.get("feedback_max_age_minutes", 20.0)),
            feedback_hold_while_connected=bool(
                w.get("feedback_hold_while_connected", False)),
        )
        if load.type not in ("deferrable", "thermal"):
            raise ValueError(f"controllable_loads['{load.name}'].type muss "
                             f"'deferrable' oder 'thermal' sein.")
        ov = (overrides or {}).get(_load_slug(load.name))
        if isinstance(ov, dict):
            _ALLOWED = {"enabled", "target_c", "min_c", "max_c", "power_w",
                        "power_profile_w",
                        "runtime_minutes", "window_from_hour", "window_to_hour",
                        "surface_m2", "solar_absorption", "deadline_hours",
                        # von der Thermomodell-Kalibrierung geschrieben
                        # (ems/pool_calibration.py --apply)
                        "loss_w_per_k"}
            for k, v in ov.items():
                if k in _ALLOWED and hasattr(load, k):
                    setattr(load, k, v)
            stage_heat = ov.get("stage_heat_w")
            if isinstance(stage_heat, dict):
                for stage in load.stages:
                    value = stage_heat.get(_load_slug(stage.name))
                    if value is not None:
                        stage.heat_w = float(value)
        out.append(load)
    return out


def _overrides_path(config_path: str) -> str:
    """Pfad der Overlay-Datei neben der config.yaml (Dashboard-Änderungen)."""
    base = os.path.dirname(os.path.abspath(config_path)) or "."
    return os.path.join(base, "config_overrides.yaml")


def _deep_merge(base: dict, over: dict) -> dict:
    """`over` rekursiv über `base` legen (verändert `base`, gibt es zurück)."""
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def save_override(config_path: str, dotted_key: str, value) -> None:
    """Persistiert EINEN Wert (z.B. "optimization.charge_strategy") in die Overlay-
    Datei config_overrides.yaml. config.yaml (kommentiert) bleibt unberührt;
    load_config merged die Overlays beim nächsten Laden über die Basis."""
    p = _overrides_path(config_path)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    node = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):   # kollidierender Skalar -> überschreiben
            node = {}
    node[parts[-1]] = value
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, p)


def load_config(path: str) -> Config:
    """Lädt und validiert die YAML-Konfiguration.

    Eine optionale Overlay-Datei ``config_overrides.yaml`` (vom Dashboard
    geschrieben) wird rekursiv über die Basis gelegt – so überdauern interaktive
    Änderungen (Lasten an/aus + Parameter, Optimierungsmodus) einen Neustart,
    ohne die kommentierte config.yaml anzutasten."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    try:
        with open(_overrides_path(path), "r", encoding="utf-8") as fh:
            overrides = yaml.safe_load(fh)
        if isinstance(overrides, dict):
            _deep_merge(raw, overrides)
    except (OSError, yaml.YAMLError):
        pass

    g = raw.get("general", {})
    general = GeneralConfig(
        timezone=g.get("timezone", "Europe/Berlin"),
        optimization_horizon_hours=int(g.get("optimization_horizon_hours", 48)),
        optimization_horizon_round_to_midnight=bool(g.get(
            "optimization_horizon_round_to_midnight", False)),
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
        enabled=bool(inf.get("enabled", True)),
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
        full_hold_penalty_ct_kwh=float(hb.get("full_hold_penalty_ct_kwh", 0.0)),
        full_hold_soc_threshold_percent=float(
            hb.get("full_hold_soc_threshold_percent", 95.0)),
    )

    inv = raw["inverter"]
    inverter = InverterConfig(
        max_ac_power_w=float(inv["max_ac_power_w"]),
        max_export_w=(float(inv["max_export_w"])
                      if inv.get("max_export_w") is not None else None),
        max_import_w=(float(inv["max_import_w"])
                      if inv.get("max_import_w") is not None else None),
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
        solver=str(o.get("solver", "cbc")),
        solver_time_limit_s=int(o.get("solver_time_limit_s", 60)),
        solver_threads=int(o.get("solver_threads", 0)),
        solver_mip_gap=float(o.get("solver_mip_gap", 0.01)),
        solver_mip_gap_abs_ct=float(o.get("solver_mip_gap_abs_ct", 25.0)),
        car_switch_penalty_ct=float(o.get("car_switch_penalty_ct", 5.0)),
        battery_switch_penalty_ct=float(o.get("battery_switch_penalty_ct", 1.0)),
        # Alter Schluessel bleibt als Lese-Fallback kompatibel; seine Einheit
        # war frueher ct/Slot, wird ab jetzt als ct/kWh interpretiert.
        battery_hold_penalty_ct_kwh=float(o.get(
            "battery_hold_penalty_ct_kwh", o.get("battery_hold_penalty_ct", 5.0))),
        car_target_penalty_ct_kwh=float(o.get("car_target_penalty_ct_kwh", 200.0)),
        grid_overload_penalty_ct_kwh=float(o.get(
            "grid_overload_penalty_ct_kwh", 1000.0)),
        export_priority_ct_kwh=float(o.get("export_priority_ct_kwh", 0.0)),
        allow_grid_discharge=bool(o.get("allow_grid_discharge", False)),
        charge_strategy=str(o.get("charge_strategy", "auto")),
        late_target_penalty_ct_kwh=float(o.get(
            "late_target_penalty_ct_kwh", 200.0)),
        late_charge_delay_ct_kwh=float(o.get(
            "late_charge_delay_ct_kwh", 5.0)),
        negative_price_export_penalty_ct_kwh=float(o.get(
            "negative_price_export_penalty_ct_kwh", 10.0)),
        auto_peak_threshold_percent=float(o.get("auto_peak_threshold_percent", 85.0)),
        seasonal_peak_tuning=bool(o.get("seasonal_peak_tuning", False)),
        auto_peak_threshold_winter_percent=float(o.get(
            "auto_peak_threshold_winter_percent", 95.0)),
        auto_peak_threshold_summer_percent=float(o.get(
            "auto_peak_threshold_summer_percent", 75.0)),
        peak_charge_ramp_penalty_winter_ct_kw=float(o.get(
            "peak_charge_ramp_penalty_winter_ct_kw", 0.5)),
        peak_charge_ramp_penalty_summer_ct_kw=float(o.get(
            "peak_charge_ramp_penalty_summer_ct_kw", 2.0)),
        auto_peak_soc_reserve_percent=float(o.get("auto_peak_soc_reserve_percent", 10.0)),
        auto_peak_p10_floor_percent=float(o.get(
            "auto_peak_p10_floor_percent", 60.0)),
        auto_peak_expected_override_percent=float(o.get(
            "auto_peak_expected_override_percent", 150.0)),
        peak_charge_weight=float(o.get("peak_charge_weight", 30.0)),
        peak_charge_ramp_penalty_ct_kw=float(o.get(
            "peak_charge_ramp_penalty_ct_kw", 2.0)),
        peak_charge_max_ramp_w=float(o.get(
            "peak_charge_max_ramp_w", 1500.0)),
        plan_change_penalty_ct_kw=float(o.get(
            "plan_change_penalty_ct_kw", 0.25)),
        plan_stability_hours=float(o.get("plan_stability_hours", 4.0)),
        standby_discharge_w=float(o.get("standby_discharge_w", 0.0)),
        min_discharge_w=float(o.get("min_discharge_w", 0.0)),
        power_headroom_percent=float(o.get("power_headroom_percent", 0.0)),
        evening_reserve_soc_percent=float(o.get("evening_reserve_soc_percent", 0.0)),
        evening_reserve_start=_parse_time(str(o.get("evening_reserve_start", "16:00"))),
        evening_reserve_end=_parse_time(str(o.get("evening_reserve_end", "20:00"))),
        evening_reserve_penalty_ct_kwh=float(o.get(
            "evening_reserve_penalty_ct_kwh", 3.0)),
        evening_reserve_auto=bool(o.get("evening_reserve_auto", False)),
        evening_reserve_hold_from_hour=int(o.get("evening_reserve_hold_from_hour", 11)),
        evening_reserve_price_factor=float(o.get("evening_reserve_price_factor", 1.15)),
    )
    # Invariante bei aktiver Negativpreis-Nullung: der Einspeise-Malus muss den
    # Late-Zeitmalus übersteigen, sonst spart der Optimierer im Late-Modus den
    # (bis zu late_charge_delay großen) Frühlade-Malus, indem er EINSPEIST statt
    # zu laden/abzuregeln – genau das Gegenteil des gewollten Verhaltens. Ein
    # (auch per Overlay gesetzter) kleinerer Wert würde das still umkehren.
    if (feed_in.zero_at_negative_price
            and optimization.negative_price_export_penalty_ct_kwh
            <= optimization.late_charge_delay_ct_kwh):
        raise ValueError(
            "optimization.negative_price_export_penalty_ct_kwh "
            f"({optimization.negative_price_export_penalty_ct_kwh}) muss größer "
            "als optimization.late_charge_delay_ct_kwh "
            f"({optimization.late_charge_delay_ct_kwh}) sein, wenn "
            "feed_in.zero_at_negative_price aktiv ist – sonst wird bei "
            "Negativpreis eingespeist statt geladen/abgeregelt.")

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
        fallback_load_w=float(f.get("fallback_load_w", 1500.0)),
        weight_same_temp=float(f.get("weight_same_temp", 2.0)),
        temp_sigma=float(f.get("temp_sigma", 4.0)),
        half_life_days=float(f.get("half_life_days", 120.0)),
        intraday_enabled=bool(f.get("intraday_enabled", True)),
        intraday_window_hours=float(f.get("intraday_window_hours", 3.0)),
        intraday_decay_hours=float(f.get("intraday_decay_hours", 6.0)),
        intraday_max_factor=float(f.get("intraday_max_factor", 1.5)),
        intraday_load_window_hours=float(f.get(
            "intraday_load_window_hours", f.get("intraday_window_hours", 3.0))),
        intraday_pv_window_hours=float(f.get(
            "intraday_pv_window_hours", f.get("intraday_window_hours", 3.0))),
        intraday_load_decay_hours=float(f.get(
            "intraday_load_decay_hours", f.get("intraday_decay_hours", 3.0))),
        intraday_pv_decay_hours=float(f.get(
            "intraday_pv_decay_hours", f.get("intraday_decay_hours", 1.5))),
        intraday_load_max_factor=float(f.get(
            "intraday_load_max_factor", f.get("intraday_max_factor", 1.5))),
        intraday_pv_max_factor=float(f.get(
            "intraday_pv_max_factor", f.get("intraday_max_factor", 1.5))),
        intraday_load_deadband=float(f.get("intraday_load_deadband", 0.10)),
        intraday_pv_deadband=float(f.get("intraday_pv_deadband", 0.10)),
        intraday_load_max_step=float(f.get("intraday_load_max_step", 0.10)),
        intraday_pv_max_step=float(f.get("intraday_pv_max_step", 0.10)),
        intraday_pv_min_power_w=float(f.get("intraday_pv_min_power_w", 1000.0)),
        price_damping=float(f.get("price_damping", 0.3)),
        method=str(f.get("method", "similar_days")),
        load_uncertainty_enabled=bool(
            f.get("load_uncertainty_enabled", True)),
        load_uncertainty_low_quantile=float(
            f.get("load_uncertainty_low_quantile", 0.10)),
        load_uncertainty_high_quantile=float(
            f.get("load_uncertainty_high_quantile", 0.90)),
        load_uncertainty_min_samples=int(
            f.get("load_uncertainty_min_samples", 12)),
    )
    if forecast.method not in ("similar_days", "ml"):
        raise ValueError("forecast.method muss 'similar_days' oder 'ml' sein.")
    if not (0 <= forecast.load_uncertainty_low_quantile <
            forecast.load_uncertainty_high_quantile <= 1):
        raise ValueError("Lastprognose-Quantile müssen 0 <= low < high <= 1 sein.")

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
        api_enabled=bool(d.get("api_enabled", True)),
        live_refresh_seconds=float(d.get("live_refresh_seconds", 5.0)),
        username=str(d.get("username", "")),
        password=str(d.get("password", "")),
        ingest_enabled=bool(d.get("ingest_enabled", False)),
        controls_enabled=bool(d.get("controls_enabled", False)),
        control_rate_limit_per_min=int(d.get("control_rate_limit_per_min", 60)),
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
        solver_runtime_alert_seconds=float(
            mon.get("solver_runtime_alert_seconds", 60.0)),
        solver_runtime_factor=float(mon.get("solver_runtime_factor", 3.0)),
        solver_runtime_baseline_runs=int(
            mon.get("solver_runtime_baseline_runs", 12)),
        execution_audit_enabled=bool(mon.get("execution_audit_enabled", True)),
        execution_grid_tolerance_w=float(
            mon.get("execution_grid_tolerance_w", 1500.0)),
        execution_battery_tolerance_w=float(
            mon.get("execution_battery_tolerance_w", 1500.0)),
        execution_soc_tolerance_percent=float(
            mon.get("execution_soc_tolerance_percent", 5.0)),
        execution_alert_consecutive=int(
            mon.get("execution_alert_consecutive", 2)),
        execution_meter_delay_minutes=float(
            mon.get("execution_meter_delay_minutes", 75.0)),
        execution_live_enabled=bool(mon.get("execution_live_enabled", True)),
        execution_live_sample_seconds=float(
            mon.get("execution_live_sample_seconds", 5.0)),
        execution_live_window_seconds=float(
            mon.get("execution_live_window_seconds", 60.0)),
        execution_live_max_gap_seconds=float(
            mon.get("execution_live_max_gap_seconds", 15.0)),
        execution_live_settle_seconds=float(
            mon.get("execution_live_settle_seconds", 30.0)),
        execution_live_consecutive=int(
            mon.get("execution_live_consecutive", 3)),
        execution_audit_grid=bool(mon.get("execution_audit_grid", False)),
        execution_audit_startup_grace_minutes=float(
            mon.get("execution_audit_startup_grace_minutes", 5.0)),
        shadow_compare_enabled=bool(mon.get("shadow_compare_enabled", True)),
        shadow_recommend_min_savings_eur=float(
            mon.get("shadow_recommend_min_savings_eur", 0.05)),
        cycle_staleness_alert_minutes=float(
            mon.get("cycle_staleness_alert_minutes", 0.0)),
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
        distribution=str(sc.get("distribution", "daytime")),
        window_start_hour=int(sc.get("window_start_hour", 5)),
        window_end_hour=int(sc.get("window_end_hour", 22)),
    )
    if solcast.combine not in ("sum", "mean"):
        raise ValueError("solcast.combine muss 'sum' oder 'mean' sein.")

    pm = raw.get("pv_model", {})
    pv_model = PvModelConfig(
        enabled=bool(pm.get("enabled", False)),
        shadow=bool(pm.get("shadow", False)),
        arrays=[PvArray(name=str(a.get("name", f"array{i}")),
                        kwp=float(a["kwp"]), tilt=float(a["tilt"]),
                        azimuth=float(a["azimuth"]))
                for i, a in enumerate(pm.get("arrays") or [])],
        temp_coeff_per_c=float(pm.get("temp_coeff_per_c", -0.0035)),
        system_loss=float(pm.get("system_loss", 0.14)),
        p10_uncertainty=float(pm.get("p10_uncertainty", 0.35)),
        p90_uncertainty=float(pm.get("p90_uncertainty", 0.15)),
    )
    ps = raw.get("pv_source_selection", {})
    pv_source_selection = PvSourceSelectionConfig(
        enabled=bool(ps.get("enabled", False)),
        lookback_days=int(ps.get("lookback_days", 30)),
        min_samples=int(ps.get("min_samples", 96)),
        min_improvement_percent=float(
            ps.get("min_improvement_percent", 2.0)),
    )
    sn = raw.get("sanity", {})
    sanity = SanityConfig(
        enabled=bool(sn.get("enabled", True)),
        price_min_ct=float(sn.get("price_min_ct", -100.0)),
        price_max_ct=float(sn.get("price_max_ct", 200.0)),
        price_spike_median_factor=float(sn.get("price_spike_median_factor", 10.0)),
        pv_max_w=float(sn.get("pv_max_w", 0.0)),
        load_max_w=float(sn.get("load_max_w", 0.0)),
    )
    rc = raw.get("recalc", {})
    recalc = RecalcConfig(
        enabled=bool(rc.get("enabled", False)),
        deviation_w=float(rc.get("deviation_w", 3000.0)),
        check_seconds=float(rc.get("check_seconds", 90.0)),
    )
    if pv_model.enabled and solcast.enabled:
        raise ValueError("solcast und pv_model nicht gleichzeitig aktivieren "
                         "(beide schreiben die PV-Prognose).")
    if (pv_model.enabled or pv_model.shadow) and not pv_model.arrays:
        raise ValueError("pv_model (enabled/shadow) ohne arrays - mind. ein "
                         "Panel-Feld nötig.")

    controllable_loads = parse_controllable_loads(
        raw.get("controllable_loads"), raw.get("controllable_loads_overrides"))
    if solcast.distribution not in ("daytime", "24h"):
        raise ValueError("solcast.distribution muss 'daytime' oder '24h' sein.")

    e = raw.get("e3dc_rscp", {})
    e3dc_rscp = E3DCRscpConfig(
        enabled=bool(e.get("enabled", False)),
        host=e.get("host", ""),
        username=e.get("username", ""),
        password=e.get("password", ""),
        key=e.get("key", ""),
        read_live=bool(e.get("read_live", True)),
        autoread_limits=bool(e.get("autoread_limits", False)),
        control_enabled=bool(e.get("control_enabled", False)),
        verify_control=bool(e.get("verify_control", True)),
        control_verify_tolerance_w=float(e.get(
            "control_verify_tolerance_w", 100.0)),
        control_alarm_repeat_minutes=float(e.get(
            "control_alarm_repeat_minutes", 60.0)),
        curtailment_control_enabled=bool(e.get(
            "curtailment_control_enabled", False)),
        curtailment_normal_percent=float(e.get(
            "curtailment_normal_percent", 100.0)),
        curtailment_verify_tolerance_percent=float(e.get(
            "curtailment_verify_tolerance_percent", 1.0)),
        grid_sign=float(e.get("grid_sign", 1.0)),
        batt_sign=float(e.get("batt_sign", 1.0)),
        history_source=bool(e.get("history_source", False)),
        history_db_path=e.get("history_db_path", "./e3dc_history.sqlite"),
        history_backfill_days=int(e.get("history_backfill_days", 730)),
        history_settle_minutes=int(e.get("history_settle_minutes", 60)),
        history_overlap_hours=int(e.get("history_overlap_hours", 3)),
    )
    if not 0.0 <= e3dc_rscp.curtailment_normal_percent <= 100.0:
        raise ValueError(
            "e3dc_rscp.curtailment_normal_percent muss zwischen 0 und 100 liegen.")
    if e3dc_rscp.curtailment_verify_tolerance_percent < 0.0:
        raise ValueError(
            "e3dc_rscp.curtailment_verify_tolerance_percent muss >= 0 sein.")
    if (e3dc_rscp.curtailment_control_enabled
            and not e3dc_rscp.control_enabled):
        raise ValueError(
            "e3dc_rscp.curtailment_control_enabled benötigt control_enabled=true.")

    config = Config(
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
        pv_model=pv_model,
        pv_source_selection=pv_source_selection,
        sanity=sanity,
        recalc=recalc,
        controllable_loads=controllable_loads,
    )
    config._source_path = os.path.abspath(path)
    try:
        config._overrides_mtime = os.path.getmtime(_overrides_path(path))
    except OSError:
        config._overrides_mtime = 0.0
    return config
