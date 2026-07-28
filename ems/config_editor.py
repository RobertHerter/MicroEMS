"""Sicherer, schemafreier Webeditor für die EMS-YAML-Konfiguration."""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
from datetime import datetime

import yaml

log = logging.getLogger("ems.config_editor")

_SAVE_LOCK = threading.Lock()
MAX_CONFIG_BYTES = 2 * 1024 * 1024

TOP_LABELS = {
    "general": "Allgemein",
    "influxdb": "InfluxDB und Signale",
    "feed_in": "Einspeisevergütung",
    "tariff": "Stromtarif",
    "solcast": "Solcast",
    "pv_model": "PV-Modell",
    "pv_source_selection": "Automatische PV-Quellenwahl",
    "sanity": "Plausibilitätsprüfung",
    "recalc": "Automatische Neuberechnung",
    "controllable_loads": "Steuerbare Lasten",
    "house_battery": "Hausakku",
    "inverter": "Wechselrichter und Netz",
    "vehicle": "Elektrofahrzeug",
    "optimization": "Optimierung",
    "forecast": "Last- und Preisprognose",
    "mqtt": "MQTT",
    "savings": "Ersparnis",
    "monitoring": "Überwachung und Alarme",
    "report": "Bericht und Debugdaten",
    "weather": "Wetterdaten",
    "e3dc_rscp": "E3/DC RSCP",
    "dashboard": "Dashboard",
    "calibration": "Kalibrierung",
}

TOP_DESCRIPTIONS = {
    "general": "Zeitzone, Standort, Feiertage und Zeitraster des gesamten EMS.",
    "influxdb": "Datenbankverbindung, Eingangssignale und Ziel-Messreihen.",
    "controllable_loads": "Verschiebbare und thermische Verbraucher anlegen, bearbeiten oder entfernen.",
    "optimization": "Solver, Kostenmodell, Akkuprioritäten und Ladestrategie.",
    "forecast": "Hauslastmodell, Live-Nowcast, Unsicherheiten und Modellvergleich.",
    "e3dc_rscp": "Direkte E3/DC-Verbindung, Gerätegrenzen und Steuerfreigaben.",
    "dashboard": "Webserver, Zugriffsschutz, Liveanzeige und Bedienfunktionen.",
}

ENUMS = {
    "feed_in.mode": ["fixed", "db"],
    "tariff.type": ["dynamic", "fixed"],
    "tariff.grid_fee_mode": ["included", "static", "14a"],
    "solcast.combine": ["sum", "mean"],
    "solcast.distribution": ["daytime", "24h"],
    "optimization.solver": ["highs", "cbc"],
    "optimization.charge_strategy": ["auto", "asap", "peak", "late"],
    "forecast.method": ["similar_days", "ml"],
    "controllable_loads[].type": ["deferrable", "thermal"],
    "pv_model.weather_models[]": ["best_match", "dwd_icon", "ecmwf_ifs"],
}

LOAD_DESCRIPTIONS = {
    "controllable_loads[].name": "Eindeutiger Anzeigename der Last.",
    "controllable_loads[].type": "Deferrable = verschiebbarer Lauf; thermal = thermischer Speicher.",
    "controllable_loads[].enabled": "Last in Prognose, Optimierung und Steuerung berücksichtigen.",
    "controllable_loads[].control_topic": "MQTT-Topic für Freigabe oder Startbefehl.",
    "controllable_loads[].switch_penalty_ct": "Kostenmalus je Einschaltvorgang gegen häufiges Takten.",
    "controllable_loads[].power_w": "Konstante elektrische Leistung in Watt.",
    "controllable_loads[].power_profile_w": "Leistungskurve je EMS-Slot ab Start.",
    "controllable_loads[].runtime_minutes": "Erforderliche Gesamtlaufzeit.",
    "controllable_loads[].window_from_hour": "Früheste lokale Startstunde.",
    "controllable_loads[].window_to_hour": "Späteste lokale Endstunde.",
    "controllable_loads[].deadline_hours": "Lauf muss innerhalb dieser Stunden abgeschlossen sein.",
    "controllable_loads[].volume_l": "Thermisch wirksames Wasservolumen in Litern.",
    "controllable_loads[].target_c": "Zieltemperatur beziehungsweise Thermostat-Sollwert.",
    "controllable_loads[].min_c": "Untere Komfortgrenze.",
    "controllable_loads[].max_c": "Obere Temperaturgrenze.",
    "controllable_loads[].loss_w_per_k": "Wärmeverlust in Watt je Kelvin Differenz zur Außenluft.",
    "controllable_loads[].surface_m2": "Wirksame Oberfläche für solaren Wärmeeintrag.",
    "controllable_loads[].solar_absorption": "Absorptionsfaktor der Oberfläche von 0 bis 1.",
    "controllable_loads[].thermostat": "EMS gibt Heizen frei; das Gerät regelt den tatsächlichen Lauf selbst.",
    "controllable_loads[].thermostat_cutoff_c": "Temperatur, ab der das Gerät SELBST abschaltet - oft höher als die Zieltemperatur. Darunter heizt es weiter, dort schaltet das EMS die Freigabe ab. 0 = Zieltemperatur verwenden.",
    "controllable_loads[].no_grid_import": "Netzstrom für diese thermische Last stark vermeiden.",
    "controllable_loads[].temp_signal": "MQTT-Topic der gemessenen Temperatur.",
    "controllable_loads[].decision_minutes": "Zeitabstand thermischer Schaltentscheidungen.",
    "controllable_loads[].binary_horizon_hours": "Binärer Planungshorizont; fernere Slots als Duty-Cycle.",
    "controllable_loads[].season.from": "Saisonbeginn als MM-DD.",
    "controllable_loads[].season.to": "Saisonende als MM-DD.",
    "controllable_loads[].stages[].name": "Eindeutiger Name der Schaltstufe.",
    "controllable_loads[].stages[].power_w": "Elektrische Leistung der Stufe.",
    "controllable_loads[].stages[].heat_w": "Abgegebene thermische Leistung.",
    "controllable_loads[].stages[].requires": "Diese andere Stufe muss gleichzeitig aktiv sein.",
    "controllable_loads[].stages[].control_topic": "MQTT-Schaltbefehl für diese Stufe.",
    "controllable_loads[].stages[].feedback_topic": "MQTT-Rückmeldung für Ein/Aus.",
    "controllable_loads[].stages[].power_topic": "MQTT-Topic der gemessenen Leistung.",
    "controllable_loads[].stages[].feedback_on_threshold_w": "Ab dieser Leistung gilt die Stufe als aktiv.",
}

# Die Signal-Bloecke unter influxdb.signals sind gleich aufgebaut, heissen aber
# je Anlage anders. Die Beschreibungen werden daher je vorhandenem Signal
# expandiert (siehe editor_payload) statt einzeln gepflegt.
SIGNAL_FIELD_DESCRIPTIONS = {
    "measurement": "InfluxDB-Measurement, aus dem dieses Signal gelesen wird.",
    "field": "Feldname innerhalb des Measurements.",
    "tags.topic": "Tag-Filter, meist das MQTT-Topic der Quelle.",
    "aggregation": "Verdichtung je Slot: mean für Leistungen/Preise, last für Zustände wie den Ladestand.",
    "fill_method": "Lücken füllen: interpolate für glatte Verläufe, hold für stufige Werte wie Preise.",
    "retention_policy": "InfluxDB-1-Retention-Policy, falls nicht die Standard-Policy.",
    "optional": "Fehlt das Signal, läuft das EMS trotzdem weiter.",
    "scale": "Faktor zur Umrechnung in die interne Einheit (W, Wh, ct/kWh, %).",
    "offset": "Konstanter Versatz nach der Skalierung.",
}

# Alle uebrigen Felder, die nicht schon durch einen Kommentar in der
# Beispiel-Config beschrieben sind. Gruppenkommentare dort decken jeweils nur
# den unmittelbar folgenden Schluessel ab - die Geschwister brauchen einen
# eigenen Text, sonst stehen sie im Editor unerklaert.
FIELD_DESCRIPTIONS = {
    # -- allgemein / Standort ------------------------------------------------
    "general.timezone": "Zeitzone für Slots, Fahrpläne und Auswertungen (IANA-Name).",
    "general.longitude": "Längengrad des Standorts für Sonnenstand und Wetterabruf.",
    # -- InfluxDB-Zugang -----------------------------------------------------
    "influxdb.v1.port": "Port der InfluxDB-1-Instanz.",
    "influxdb.v1.username": "Benutzername für InfluxDB 1 (leer, wenn nicht nötig).",
    "influxdb.v1.password": "Passwort für InfluxDB 1.",
    "influxdb.v1.database": "Datenbankname in InfluxDB 1.",
    "influxdb.v1.ssl": "Verbindung über HTTPS aufbauen.",
    "influxdb.v1.verify_ssl": "Zertifikat prüfen; bei selbstsigniertem Zertifikat abschalten.",
    "influxdb.v2.url": "Basis-URL der InfluxDB-2-Instanz.",
    "influxdb.v2.token": "API-Token mit Lese- und Schreibrecht.",
    "influxdb.v2.org": "Organisation in InfluxDB 2.",
    "influxdb.v2.bucket": "Bucket, in dem die Messwerte liegen.",
    # -- Akku / Fahrzeug -----------------------------------------------------
    "house_battery.min_soc_percent": "Untere Entladegrenze des Hausakkus in Prozent.",
    "house_battery.max_soc_percent": "Obere Ladegrenze des Hausakkus in Prozent.",
    "vehicle.capacity_wh": "Nutzbare Kapazität der Fahrzeugbatterie in Wh.",
    "vehicle.max_charge_w": "Maximale Ladeleistung der Wallbox in Watt.",
    "vehicle.min_soc_percent": "Ladezustand, der im Fahrzeug nicht unterschritten wird.",
    "vehicle.target_soc_percent": "Ziel-Ladezustand, der bis zur Abfahrt erreicht sein soll.",
    # -- Optimierung ---------------------------------------------------------
    "optimization.auto_peak_soc_reserve_percent": "Zusätzlich freigehaltene Kapazität als Reserve bei der automatischen Peak-Entscheidung.",
    "optimization.auto_peak_expected_override_percent": "Ein deutlich höherer Erwartungsüberschuss darf ein sehr breites p10-Band überstimmen.",
    "optimization.auto_peak_threshold_winter_percent": "Peak-Schwelle am 21. Dezember; dazwischen wird glatt interpoliert.",
    "optimization.auto_peak_threshold_summer_percent": "Peak-Schwelle am 21. Juni; dazwischen wird glatt interpoliert.",
    "optimization.peak_charge_ramp_penalty_winter_ct_kw": "Malus auf Ladesprünge im Winter; glättet das Laden entlang der Einspeise-Linie.",
    "optimization.peak_charge_ramp_penalty_summer_ct_kw": "Malus auf Ladesprünge im Sommer; glättet das Laden entlang der Einspeise-Linie.",
    "optimization.late_charge_delay_ct_kwh": "Zeitgewichteter Malus auf frühes Laden im Modus „Spät laden“. 0 = keine Verschiebung.",
    "optimization.plan_stability_hours": "So viele Stunden des bisherigen Fahrplans werden gegen kleine Umplanungen geschützt. 0 = aus.",
    # -- Prognose ------------------------------------------------------------
    "forecast.weight_same_month": "Gewicht für historische Tage aus demselben Monat bei der Ähnlichkeitssuche.",
    "forecast.weight_same_season": "Gewicht für historische Tage aus derselben Jahreszeit.",
    "forecast.temp_sigma": "Breite des Temperatur-Ähnlichkeitskerns in Kelvin. 0 = Temperatur ignorieren.",
    "forecast.load_uncertainty_low_quantile": "Unteres Quantil des empirischen Lastbands (Dashboard-Fläche).",
    "forecast.load_uncertainty_high_quantile": "Oberes Quantil des empirischen Lastbands.",
    "forecast.load_uncertainty_min_samples": "So viele vergleichbare Slots müssen vorliegen, bevor das Band gezeigt wird.",
    "forecast.intraday_load_window_hours": "Zeitfenster, über das die Ist-Abweichung der Hauslast gemittelt wird.",
    "forecast.intraday_load_deadband": "Kleine Abweichungen darunter werden ignoriert (Rauschunterdrückung).",
    "forecast.intraday_load_max_factor": "Obergrenze des Korrekturfaktors für die Hauslast.",
    "forecast.intraday_load_max_step": "Maximale Änderung des Korrekturfaktors je 15-min-Lauf.",
    "forecast.intraday_load_decay_hours": "Zeitkonstante, über die die Korrektur wieder abklingt.",
    "forecast.intraday_pv_window_hours": "Zeitfenster, über das die Ist-Abweichung der PV gemittelt wird.",
    "forecast.intraday_pv_deadband": "Kleine PV-Abweichungen darunter werden ignoriert.",
    "forecast.intraday_pv_max_factor": "Obergrenze des Korrekturfaktors für die PV.",
    "forecast.intraday_pv_max_step": "Maximale Änderung des PV-Korrekturfaktors je Lauf.",
    "forecast.intraday_pv_decay_hours": "Zeitkonstante, über die die PV-Korrektur abklingt.",
    "forecast.disaggregation_lookback_days": "Rückblick, aus dem die reale Laufzeit steuerbarer Lasten herausgerechnet wird.",
    "forecast.disaggregation_min_samples": "Mindestzahl echter Rückmeldungen, bevor herausgerechnet wird.",
    "forecast.live_nowcast_retention_days": "Aufbewahrung der 5-Sekunden-Livewerte in der lokalen Historie.",
    "forecast.live_nowcast_min_coverage_seconds": "Mindestabdeckung je 15-min-Slot, damit der Livewert als belastbar gilt.",
    "forecast.live_nowcast_max_gap_seconds": "Größere Lücken beenden die Verdichtung eines Slots.",
    "forecast.load_ensemble_lookback_days": "Rückblick für die Bewertung der Prognoseverfahren gegeneinander.",
    "forecast.load_ensemble_min_folds": "So viele unabhängige Vergleichstage sind nötig, bevor das Ensemble greift.",
    "forecast.load_ensemble_horizon_hours[]": "Vorlaufzeit-Grenzen, für die getrennte Gewichte gelernt werden.",
    "forecast.load_ensemble_min_weight": "Mindestgewicht je Verfahren, damit keines ganz verschwindet.",
    "forecast.load_ensemble_archive_stride_hours": "Abstand, in dem Prognosen zum späteren Vergleich archiviert werden.",
    "forecast.load_ensemble_ml_retrain_hours": "Mindestabstand zwischen zwei Trainings des ML-Schattenmodells.",
    "forecast.temperature_residual_min_folds": "Nötige Vergleichstage, bevor die Temperatur-Restkorrektur genutzt wird.",
    "forecast.temperature_residual_min_samples": "Nötige Einzelwerte für die Temperatur-Restkorrektur.",
    "forecast.temperature_residual_max_adjustment_percent": "Obergrenze der Korrektur aus dem Heiz-/Kühl-Residual.",
    # -- PV-Modell / Quellenwahl --------------------------------------------
    "pv_model.enabled": "Freie PV-Prognose mit pvlib aktivieren (Alternative oder Ergänzung zu Solcast).",
    "pv_model.arrays[].name": "Bezeichnung des Teilgenerators, z. B. „Ost“ oder „West“.",
    "pv_model.arrays[].kwp": "Installierte Leistung dieses Teilgenerators in kWp.",
    "pv_model.arrays[].tilt": "Neigung der Module in Grad (0 = waagerecht).",
    "pv_model.arrays[].azimuth": "Ausrichtung in Grad (180 = Süden).",
    "pv_model.weather_models[]": "Wettermodelle, die unabhängig gerechnet und danach gewichtet kombiniert werden.",
    "pv_model.ensemble_min_samples": "Nötige Vergleichswerte, bevor die Modellgewichte angepasst werden.",
    "pv_model.ensemble_horizon_hours[]": "Vorlaufzeit-Grenzen für getrennt gelernte Modellgewichte.",
    "pv_model.ensemble_min_weight": "Mindestgewicht je Wettermodell, damit keines ganz verschwindet.",
    "pv_source_selection.enabled": "pvlib und Solcast laufend gegen die echten Erträge bewerten und die bessere Quelle wählen.",
    "pv_source_selection.lookback_days": "Zeitraum, über den die Quellen verglichen werden.",
    "pv_source_selection.min_samples": "Nötige Vergleichswerte, bevor gewechselt wird.",
    "pv_source_selection.min_improvement_percent": "So viel besser muss die andere Quelle sein, damit gewechselt wird.",
    # -- Solcast / Wetter ----------------------------------------------------
    "solcast.enabled": "PV-Prognose von Solcast abrufen (API-Schlüssel nötig).",
    "solcast.calls_per_key_per_day": "Abrufbudget je API-Schlüssel und Tag (Free-Tier meist 10).",
    "solcast.window_end_hour": "Ende des Zeitfensters, über das die Abrufe verteilt werden.",
    "solcast.sources[].api_key": "Solcast-API-Schlüssel.",
    "solcast.sources[].resource_id": "Kennung der Solcast-Anlage (rooftop site).",
    "weather.enabled": "Temperatur und Einstrahlung von Open-Meteo abrufen (ohne Schlüssel).",
    "weather.forecast_days": "Wie viele Tage Wettervorhersage geladen werden.",
    "weather.past_days": "Wie viele vergangene Tage mitgeladen werden.",
    # -- Tarif ---------------------------------------------------------------
    "tariff.enabled": "Bezugspreis aus Spotpreis und Tarifmodell berechnen statt aus der InfluxDB lesen.",
    "tariff.history_backfill_days": "Wie weit die Preishistorie beim ersten Lauf nachgeladen wird.",
    "tariff.grid_fee_windows[].ct_kwh": "Netzentgelt in diesem Zeitfenster (netto ct/kWh).",
    "tariff.grid_fee_windows[].hours[]": "Stunden, für die dieses Fenster gilt (Ende ausschließlich).",
    "tariff.grid_fee_windows[].months[]": "Monate, für die dieses Fenster gilt.",
    # -- E3DC / MQTT ---------------------------------------------------------
    "e3dc_rscp.password": "RSCP-Passwort des E3DC (im Gerät unter Benutzerprofil gesetzt).",
    "e3dc_rscp.read_live": "Aktuelle Messwerte direkt vom Gerät lesen statt über die Datenbank.",
    "e3dc_rscp.history_db_path": "Pfad der lokalen SQLite-Historie (Messwerte, Archive, Ereignisse).",
    "e3dc_rscp.history_backfill_days": "Wie viele Tage Gerätehistorie beim Start nachgeladen werden.",
    "mqtt.enabled": "Sollwerte per MQTT ausgeben.",
    "mqtt.host": "Adresse des MQTT-Brokers.",
    "mqtt.port": "Port des MQTT-Brokers (meist 1883, mit TLS 8883).",
    "mqtt.username": "Benutzername am Broker.",
    "mqtt.password": "Passwort am Broker.",
    "mqtt.base_topic": "Präfix aller vom EMS veröffentlichten Topics.",
    "mqtt.qos": "MQTT-Dienstgüte: 0 = einmal senden, 1 = bestätigt, 2 = genau einmal.",
    "mqtt.schedule_fields[]": "Spalten der Steuertabelle, die als Zeitplan veröffentlicht werden.",
    # -- Dashboard / Berichte / Sonstiges ------------------------------------
    "dashboard.enabled": "HTML-Dashboard erzeugen.",
    "dashboard.output_path": "Dateipfad der erzeugten Dashboard-Seite.",
    "dashboard.host": "Adresse, auf der der Dashboard-Server lauscht (0.0.0.0 = alle).",
    "dashboard.port": "Port des Dashboard-Servers.",
    "monitoring.drift_enabled": "Abweichung zwischen prognostiziertem und echtem Ladestand überwachen.",
    "monitoring.drift_window_hours": "Zeitfenster, über das die Drift gemittelt wird.",
    "monitoring.drift_alert_percent": "Ab dieser mittleren Abweichung in Prozentpunkten wird gewarnt.",
    "monitoring.execution_soc_tolerance_percent": "Zulässige Abweichung des Ladestands bei der Ausführungsprüfung.",
    "monitoring.execution_battery_tolerance_w": "Zulässige Abweichung der Akkuleistung bei der Ausführungsprüfung.",
    "monitoring.execution_grid_tolerance_w": "Zulässige Abweichung der Netzleistung bei der Ausführungsprüfung.",
    "monitoring.execution_alert_consecutive": "So viele Prüfungen müssen nacheinander scheitern, bevor Alarm ausgelöst wird.",
    "monitoring.solver_runtime_baseline_runs": "Zahl der Läufe, aus denen die typische Solver-Laufzeit gebildet wird.",
    "monitoring.solver_runtime_factor": "Ab diesem Vielfachen der typischen Laufzeit gilt der Solver als langsam.",
    "calibration.enabled": "Wöchentlich kalibriertes Korrekturprofil für PV und Hauslast anwenden.",
    "calibration.pv_profile": "Pfad des Korrekturprofils (wird von der Kalibrierung geschrieben).",
    "recalc.enabled": "Bei großer Abweichung zwischen Plan und Ist sofort neu rechnen, statt auf den nächsten Zyklus zu warten.",
    "report.enabled": "Debug-Schnappschüsse speichern und den Versand-Knopf im Dashboard zeigen.",
    "report.mail_to": "Vorbelegte Empfängeradresse beim Öffnen des Mailprogramms.",
    "report.snapshot_path": "Dateipfad des jeweils letzten Debug-Schnappschusses.",
    "savings.enabled": "Ersparnis gegen eine „Ohne-EMS“-Baseline mitschreiben.",
    "savings.state_path": "Datei, in der der Ersparnis-Stand fortgeschrieben wird.",
    # -- weitere Felder aus der Beispiel-Config ------------------------------
    "house_battery.capacity_wh": "Nennkapazität des Hausakkus in Wh (Basis der SoC-Prozentwerte).",
    "house_battery.full_hold_soc_threshold_percent": "Ab diesem Ladestand zählt der Akku als „voll gehalten“ (Zellschonung).",
    "vehicle.enabled": "Fahrzeug in der Optimierung berücksichtigen.",
    "influxdb.v1.host": "Adresse der InfluxDB-1-Instanz.",
    "dashboard.password": "Passwort der Dashboard-Anmeldung (Basic Auth).",
    "e3dc_rscp.enabled": "Direkte Verbindung zum E3DC per RSCP aufbauen.",
    "e3dc_rscp.control_enabled": "Den Speicher wirklich steuern (greift real ein), nicht nur lesen.",
    "e3dc_rscp.batt_sign": "Vorzeichen der Akkuleistung umdrehen, falls das Gerät Entladen positiv meldet.",
    "e3dc_rscp.grid_sign": "Vorzeichen der Netzleistung umdrehen, falls das Gerät Einspeisung positiv meldet.",
    "e3dc_rscp.curtailment_normal_percent": "Normalwert der PV-Leistungsbegrenzung, auf den nach einem Eingriff zurückgestellt wird.",
    "e3dc_rscp.curtailment_verify_tolerance_percent": "Zulässige Abweichung beim Zurücklesen der eingestellten Begrenzung.",
    "monitoring.execution_live_sample_seconds": "Abtastrate der schnellen Ausführungsprüfung aus E3DC-Livewerten.",
    "monitoring.execution_live_window_seconds": "Zeitfenster, über das der robuste Median dieser Prüfung gebildet wird.",
    "monitoring.execution_live_max_gap_seconds": "Größere Messlücken werden nicht mehr interpoliert.",
    "monitoring.execution_live_settle_seconds": "Einpendelzeit nach einem Steuereingriff, bevor bewertet wird.",
    "monitoring.execution_live_consecutive": "So viele Abweichungen nacheinander lösen den vorläufigen Alarm aus.",
    "optimization.auto_peak_threshold_percent": "Ab diesem Anteil der nutzbaren Kapazität gilt ein Tag als Peak-Tag.",
    "optimization.auto_peak_p10_floor_percent": "Mindestanteil, den das pessimistische p10 der Schwelle erreichen muss, damit ein starker Erwartungswert sie plausibilisieren darf.",
    "optimization.evening_reserve_start": "Beginn des Abendfensters, in dem die Reserve gehalten wird.",
    "optimization.evening_reserve_end": "Ende des Fensters; danach darf der Akku gezielt in die Spitze entladen.",
    "optimization.evening_reserve_hold_from_hour": "Ab dieser Stunde wird die automatisch bestimmte Abend-Reserve gehalten.",
    "optimization.evening_reserve_price_factor": "Reserve nur, wenn das Abend-Preismaximum mindestens dieses Vielfache des Tagesmedians erreicht.",
    "pv_model.p90_uncertainty": "Breite des oberen PV-Bandes; wird aus echten Residuen kalibriert.",
    "solcast.combine": "Mehrere Solcast-Quellen verrechnen: sum für getrennte Teilgeneratoren, mean für dieselbe Anlage.",
    "sanity.enabled": "Externe Eingangswerte auf Plausibilität prüfen und Ausreißer ersetzen.",
    "sanity.price_max_ct": "Obergrenze für plausible Strompreise; darüber wird der Median eingesetzt.",
    "tariff.grid_fee_ct_kwh": "Konstantes Netzentgelt bei grid_fee_mode „static“ (netto ct/kWh).",
    "tariff.grid_fee_windows[].date_from": "Beginn des Zeitraums als MM-DD (Jahreswechsel wird unterstützt).",
    "tariff.grid_fee_windows[].date_to": "Ende des Zeitraums als MM-DD.",
    # -- steuerbare Lasten: Restfelder ---------------------------------------
    "controllable_loads[].window.from": "Früheste lokale Startstunde.",
    "controllable_loads[].window.to": "Späteste lokale Endstunde.",
    "controllable_loads[].power_profile_w[]": "Leistungswert eines 15-min-Schritts der Startkurve.",
    "controllable_loads[].feedback_required": "Alarm auslösen, wenn die echte Rückmeldung der Last fehlt.",
    "controllable_loads[].feedback_max_age_minutes": "Ab diesem Alter gilt eine Rückmeldung als veraltet.",
    "controllable_loads[].feedback_hold_while_connected": "Für Sensoren, die nur bei Änderung senden: letzter Wert gilt, solange die MQTT-Verbindung steht.",
}

LOAD_TEMPLATES = {
    "deferrable": {
        "name": "Neue verschiebbare Last",
        "type": "deferrable",
        "enabled": True,
        "power_w": 1000,
        "runtime_minutes": 60,
        "window": {"from": 8, "to": 20},
        "deadline_hours": 24,
        "switch_penalty_ct": 5,
        "control_topic": "",
    },
    "thermal": {
        "name": "Neue thermische Last",
        "type": "thermal",
        "enabled": True,
        "volume_l": 1000,
        "target_c": 28.0,
        "min_c": 27.0,
        "max_c": 29.0,
        "loss_w_per_k": 0.0,
        "surface_m2": 0.0,
        "solar_absorption": 0.75,
        "thermostat": False,
        # Abschaltpunkt des Geraets; 0 = wie target_c (siehe Beschreibung).
        "thermostat_cutoff_c": 0.0,
        "no_grid_import": False,
        "decision_minutes": 60,
        "binary_horizon_hours": 12,
        "switch_penalty_ct": 5,
        "temp_signal": "",
        "stages": [{
            "name": "Stufe 1", "power_w": 1000, "heat_w": 3000,
            "control_topic": "", "feedback_topic": "", "power_topic": "",
        }],
    },
}


def editor_allowed(config) -> bool:
    dashboard = config.dashboard
    return bool(
        dashboard.controls_enabled
        and getattr(dashboard, "config_editor_enabled", False)
        and dashboard.username and dashboard.password)


def editor_block_reason(config) -> str | None:
    dashboard = config.dashboard
    if not dashboard.controls_enabled:
        return "dashboard.controls_enabled ist deaktiviert"
    if not getattr(dashboard, "config_editor_enabled", False):
        return "dashboard.config_editor_enabled ist deaktiviert"
    if not (dashboard.username and dashboard.password):
        return "Für den Konfigurationseditor müssen dashboard.username und dashboard.password gesetzt sein"
    return None


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def _revision(config_path: str) -> str:
    from .config import _overrides_path
    digest = hashlib.sha256()
    digest.update(_read_bytes(config_path))
    digest.update(b"\0overlay\0")
    digest.update(_read_bytes(_overrides_path(config_path)))
    return digest.hexdigest()


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError("Die YAML-Wurzel muss ein Objekt sein.")
    return value


def _load_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_") or "load"


def _effective_document(config_path: str) -> tuple[dict, bool]:
    """Basis und lokale Overrides zu einem speicherbaren Dokument vereinigen."""
    from .config import _deep_merge, _overrides_path
    base = _load_yaml(config_path)
    try:
        overlay = _load_yaml(_overrides_path(config_path))
    except (OSError, ValueError, yaml.YAMLError):
        overlay = {}
    effective = _deep_merge(copy.deepcopy(base), copy.deepcopy(overlay))
    load_overrides = effective.pop("controllable_loads_overrides", {}) or {}
    if isinstance(load_overrides, dict):
        for load in effective.get("controllable_loads") or []:
            override = load_overrides.get(_load_slug(load.get("name", "")))
            if not isinstance(override, dict):
                continue
            for key, value in override.items():
                if key == "stage_heat_w" and isinstance(value, dict):
                    for stage in load.get("stages") or []:
                        stage_value = value.get(_load_slug(stage.get("name", "")))
                        if stage_value is not None:
                            stage["heat_w"] = stage_value
                elif key in load:
                    load[key] = value
    return effective, bool(overlay)


def _clean_comment(value: str) -> str:
    value = value.strip().lstrip("#").strip()
    if not value or set(value) <= {"-", "="}:
        return ""
    return value


def extract_descriptions(text: str) -> dict:
    """Vorhergehende und Inline-YAML-Kommentare grob einem Schlüssel zuordnen."""
    descriptions, stack, pending = {}, [], []
    key_re = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:(.*)$")
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            cleaned = _clean_comment(stripped)
            if cleaned:
                pending.append(cleaned)
            continue
        match = key_re.match(raw)
        if not match:
            if stripped:
                pending = []
            continue
        indent = len(match.group(1).replace("\t", "  "))
        key, tail = match.group(2), match.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([entry[1] for entry in stack] + [key])
        inline = ""
        if " #" in tail:
            inline = _clean_comment(tail.split(" #", 1)[1])
        parts = pending[-4:] + ([inline] if inline else [])
        if parts:
            descriptions[path] = " ".join(dict.fromkeys(parts))
        pending = []
        value = tail.split(" #", 1)[0].strip()
        if not value or value in ("|", ">"):
            stack.append((indent, key))
    return descriptions


def _description_payload(config_path: str) -> dict:
    descriptions = {}
    example = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                           "config.example.yaml")
    for path in (example, config_path):
        try:
            descriptions.update(extract_descriptions(
                _read_bytes(path).decode("utf-8")))
        except UnicodeDecodeError:
            pass
    descriptions.update(TOP_DESCRIPTIONS)
    descriptions.update(LOAD_DESCRIPTIONS)
    descriptions.update(FIELD_DESCRIPTIONS)
    return descriptions


def _expand_signal_descriptions(document, descriptions) -> None:
    """influxdb.signals.<name>.<feld> je vorhandenem Signal beschreiben.

    Die Bloecke sind gleich aufgebaut, heissen aber je Anlage anders - ohne
    diese Expansion staenden alle Signalfelder unerklaert im Editor."""
    signals = (document.get("influxdb") or {}).get("signals")
    if not isinstance(signals, dict):
        return
    for name, block in signals.items():
        if not isinstance(block, dict):
            continue
        for leaf, text in SIGNAL_FIELD_DESCRIPTIONS.items():
            key = f"influxdb.signals.{name}.{leaf}"
            descriptions.setdefault(key, text)   # eigener Kommentar gewinnt


def editor_payload(config_path: str) -> dict:
    document, had_overrides = _effective_document(config_path)
    descriptions = _description_payload(config_path)
    _expand_signal_descriptions(document, descriptions)
    return {
        "config": document,
        "revision": _revision(config_path),
        "descriptions": descriptions,
        "labels": TOP_LABELS,
        "enums": ENUMS,
        "load_templates": LOAD_TEMPLATES,
        "had_overrides": had_overrides,
    }


def validate_document(document) -> dict:
    if not isinstance(document, dict):
        raise ValueError("config muss ein JSON/YAML-Objekt sein")
    encoded = yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False).encode("utf-8")
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ValueError("Konfiguration ist größer als 2 MiB")
    from .config import load_config
    handle = tempfile.NamedTemporaryFile(
        mode="wb", prefix="ems-config-validate-", suffix=".yaml",
        delete=False)
    try:
        with handle:
            handle.write(encoded)
        parsed = load_config(handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    warnings = []
    if (parsed.dashboard.config_editor_enabled
            and not (parsed.dashboard.username and parsed.dashboard.password)):
        warnings.append(
            "Konfigurationseditor bleibt ohne dashboard.username/password gesperrt.")
    return {
        "valid": True,
        "blocks": len(document),
        "controllable_loads": len(parsed.controllable_loads),
        "warnings": warnings,
    }


def _atomic_write(path: str, content: bytes, mode: int) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".ems-config-", suffix=".tmp",
                               dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def save_document(config_path: str, document, expected_revision: str) -> dict:
    """Validieren, sichern und atomar speichern; Overrides werden konsolidiert."""
    from .config import _overrides_path
    validation = validate_document(document)
    with _SAVE_LOCK:
        current_revision = _revision(config_path)
        if expected_revision != current_revision:
            raise RuntimeError(
                "Die Konfiguration wurde zwischenzeitlich geändert. "
                "Bitte Seite neu laden.")
        content = (
            "# EMS-Konfiguration – über den Dashboard-Editor gespeichert.\n"
            "# Feldbeschreibungen und Standardwerte sind weiterhin im Editor "
            "und in config.example.yaml verfügbar.\n\n"
        ).encode("utf-8") + yaml.safe_dump(
            document, allow_unicode=True, sort_keys=False,
            default_flow_style=False).encode("utf-8")
        if len(content) > MAX_CONFIG_BYTES:
            raise ValueError("Konfiguration ist größer als 2 MiB")
        backup_dir = os.path.join(
            os.path.dirname(os.path.abspath(config_path)), "backup",
            "config")
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(
            config_path,
            os.path.join(backup_dir, f"config-{stamp}.yaml"))
        override_path = _overrides_path(config_path)
        if os.path.exists(override_path):
            shutil.copy2(
                override_path,
                os.path.join(backup_dir, f"config_overrides-{stamp}.yaml"))
        try:
            mode = stat.S_IMODE(os.stat(config_path).st_mode)
        except OSError:
            mode = 0o600
        _atomic_write(config_path, content, mode)
        # Die Seite bearbeitet den zuvor zusammengeführten effektiven Stand.
        # Alte Overlays danach neutralisieren, sonst würden sie die neuen Werte
        # beim Neustart erneut überstimmen.
        _atomic_write(override_path, b"{}\n", mode)
        return {
            "saved": True,
            "backup": os.path.relpath(backup_dir),
            "revision": _revision(config_path),
            "validation": validation,
        }


def editor_html() -> bytes:
    return _EDITOR_HTML.encode("utf-8")


_EDITOR_HTML = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#1769c2">
<title>EMS Konfiguration</title>
<script>(function(){const s=localStorage.getItem('ems-theme');document.documentElement.classList.toggle('dark',s==='dark'||(!s&&matchMedia('(prefers-color-scheme:dark)').matches));})();</script>
<style>
:root{color-scheme:light;--bg:#eef3f8;--card:#fff;--text:#20252b;--muted:#697785;--line:#dce4eb;--input:#fff;--blue:#1769c2;--soft:#f5f7f9;--danger:#b52d28;--ok:#258448}
html.dark{color-scheme:dark;--bg:#111820;--card:#1a2631;--text:#e7edf4;--muted:#aebbc8;--line:#3d4c5b;--input:#202e3a;--blue:#338be0;--soft:#202e3a;--danger:#ff8c87;--ok:#75ce91}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--card) 95%,transparent);border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}
.top-in{max-width:1500px;margin:auto;padding:12px 18px;display:flex;align-items:center;gap:12px}.back{color:var(--blue);text-decoration:none;font-weight:700}.title{flex:1}.title h1{font-size:20px;margin:0}.title small{color:var(--muted)}
button,.button{border:1px solid var(--line);border-radius:8px;background:var(--soft);color:var(--text);padding:8px 12px;font:inherit;cursor:pointer}button.primary{background:var(--blue);border-color:var(--blue);color:white;font-weight:700}button.danger{color:var(--danger)}button:disabled{opacity:.55;cursor:wait}
main{max-width:1500px;margin:auto;padding:16px 18px 100px}.notice{padding:11px 13px;border:1px solid #e1b74a;background:#fff7da;color:#735b11;border-radius:10px;margin-bottom:12px}html.dark .notice{background:#3a3219;color:#e5cb74;border-color:#6a5925}
.tools{display:grid;grid-template-columns:minmax(220px,1fr) auto;gap:10px;margin-bottom:12px}.tools input{width:100%}
details.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:9px 0;overflow:hidden}details.panel>summary{list-style:none;padding:14px 16px;cursor:pointer;display:flex;align-items:center;gap:10px}details.panel>summary::-webkit-details-marker{display:none}.panel-title{font-weight:750;font-size:16px}.panel-desc{display:block;color:var(--muted);font-size:12px;margin-top:3px}.panel-count{margin-left:auto;color:var(--muted);font-size:12px}.panel-body{border-top:1px solid var(--line);padding:14px}
.field-row{display:grid;grid-template-columns:minmax(190px,.8fr) minmax(260px,1.4fr);gap:14px;padding:9px 4px;border-bottom:1px solid color-mix(in srgb,var(--line) 65%,transparent)}.field-row:last-child{border-bottom:0}.field-label b{display:block;overflow-wrap:anywhere}.field-label small{display:block;color:var(--muted);margin-top:3px;line-height:1.35}.path{font:10px ui-monospace,monospace;color:var(--muted)}
input,select,textarea{width:100%;border:1px solid var(--line);border-radius:7px;background:var(--input);color:var(--text);padding:8px 9px;font:inherit}input[type=checkbox]{width:22px;height:22px;accent-color:var(--blue)}.bool-wrap{min-height:38px;display:flex;align-items:center}.secret-wrap{display:flex;gap:5px}.secret-wrap button{padding:5px 9px}
fieldset.group{border:1px solid var(--line);border-radius:10px;margin:10px 0;padding:8px 11px 11px}fieldset.group>legend{padding:0 7px;font-weight:700}.group-actions,.item-actions,.load-add{display:flex;gap:7px;justify-content:flex-end;margin-top:8px}.list-item,.load-card{border:1px solid var(--line);background:var(--soft);border-radius:10px;padding:10px;margin:8px 0}.load-head{display:flex;align-items:center;gap:8px}.load-head b{flex:1;font-size:15px}.badge{font-size:11px;color:var(--blue);border:1px solid color-mix(in srgb,var(--blue) 55%,var(--line));border-radius:20px;padding:3px 7px}
.array-scalar{display:grid;grid-template-columns:1fr auto;gap:6px;margin:5px 0}.empty{color:var(--muted);padding:8px}.status{position:fixed;z-index:25;left:50%;bottom:18px;transform:translateX(-50%);width:min(920px,calc(100% - 24px));display:flex;align-items:center;gap:10px;padding:11px 13px;background:var(--card);border:1px solid var(--line);border-radius:11px;box-shadow:0 8px 30px #0003}.status-text{flex:1}.status-text small{display:block;color:var(--muted);margin-top:2px}.status.ok{border-color:#64b87d}.status.err{border-color:#d56b67}.hidden{display:none!important}
@media(max-width:700px){.top-in{padding:10px}.title small{display:none}.title h1{font-size:17px}main{padding:10px 9px 105px}.tools{grid-template-columns:1fr}.field-row{grid-template-columns:1fr;gap:7px}.panel-body{padding:10px}.field-row{padding:10px 2px}.top button{padding:8px}.desktop-label{display:none}.status{bottom:8px;flex-wrap:wrap}.status-text{flex-basis:100%}.status button{flex:1;min-height:42px}}
</style></head><body>
<header class="top"><div class="top-in"><a class="back" href="/">← Dashboard</a><div class="title"><h1>EMS-Konfiguration</h1><small>Alle Einstellungen mit Prüfung, Backup und sicherem Neustart</small></div><button id="theme">◐ <span class="desktop-label">Darstellung</span></button></div></header>
<main><div id="overlay-note" class="notice hidden"></div><div class="tools"><input id="search" type="search" placeholder="Einstellungen durchsuchen …"><button id="expand">Alle Panels öffnen</button></div><div id="editor"><p>Lade Konfiguration …</p></div></main>
<div id="status" class="status"><div class="status-text"><b id="status-main">Konfiguration wird geladen …</b><small id="status-sub">Bitte warten</small></div><button id="validate">Prüfen</button><button id="save" class="primary">Speichern &amp; EMS neu starten</button></div>
<script>
let state=null,revision='',descriptions={},labels={},enums={},templates={},dirty=false,allOpen=false;
const $=s=>document.querySelector(s),esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const norm=p=>p.replace(/\[\d+\]/g,'[]');
function desc(path){return descriptions[path]||descriptions[norm(path)]||''}
function label(key){return String(key).replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase())}
function enumFor(path){return enums[path]||enums[norm(path)]}
function secret(path){return /(^|\.)(password|token|api_key|key|secret)$/i.test(path)}
function changed(){dirty=true;setStatus('Ungespeicherte Änderungen','Vor dem Übernehmen wird die gesamte Konfiguration validiert.','')}
function setStatus(main,sub,kind){$('#status-main').textContent=main;$('#status-sub').textContent=sub||'';$('#status').className='status '+(kind||'')}
function fieldHead(key,path){const d=desc(path);return `<div class="field-label"><b>${esc(label(key))}</b><span class="path">${esc(path)}</span>${d?`<small>${esc(d)}</small>`:''}</div>`}
function scalar(key,value,path,setter){
 const row=document.createElement('div');row.className='field-row';row.dataset.search=(key+' '+path+' '+desc(path)).toLowerCase();row.innerHTML=fieldHead(key,path);const box=document.createElement('div');
 if(typeof value==='boolean'){box.className='bool-wrap';const i=document.createElement('input');i.type='checkbox';i.checked=value;i.onchange=()=>{setter(i.checked);changed()};box.append(i)}
 else {const choices=enumFor(path);let i;if(choices){i=document.createElement('select');choices.forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;o.selected=String(value)===v;i.append(o)})}
 else{i=document.createElement('input');if(typeof value==='number'){i.type='number';i.step=Number.isInteger(value)?'1':'any';i.value=String(value)}else{i.type=secret(path)?'password':'text';i.value=value==null?'':String(value);i.placeholder=value==null?'null':''}}
 const apply=()=>{let v;if(typeof value==='number')v=i.value===''?null:Number(i.value);else if(value===null)v=i.value===''?null:i.value;else v=i.value;setter(v);changed()};i.onchange=apply;i.oninput=()=>{if(typeof value!=='number')apply()};
 if(secret(path)){const wrap=document.createElement('div');wrap.className='secret-wrap';wrap.append(i);const show=document.createElement('button');show.type='button';show.textContent='◉';show.title='Anzeigen/verbergen';show.onclick=()=>i.type=i.type==='password'?'text':'password';wrap.append(show);box.append(wrap)}else box.append(i)}
 row.append(box);return row}
function renderObject(obj,path,container,root=false){
 Object.keys(obj).forEach(key=>{const p=path?path+'.'+key:key;const value=obj[key];const setter=v=>obj[key]=v;
  if(value&&typeof value==='object'){const fs=document.createElement('fieldset');fs.className='group';const lg=document.createElement('legend');lg.textContent=label(key);fs.append(lg);renderValue(value,p,fs,setter);container.append(fs)}
  else container.append(scalar(key,value,p,setter));
 });
 if(!root){const actions=document.createElement('div');actions.className='group-actions';const add=document.createElement('button');add.type='button';add.textContent='＋ Eigenschaft';add.onclick=()=>{const key=prompt('Name der neuen Eigenschaft');if(key&&!(key in obj)){obj[key]='';changed();render()}};actions.append(add);container.append(actions)}
}
function renderList(list,path,container){
 if(path==='controllable_loads'){renderLoads(list,container);return}
 if(!list.length){const e=document.createElement('div');e.className='empty';e.textContent='Leere Liste';container.append(e)}
 list.forEach((value,index)=>{const p=`${path}[${index}]`;if(value&&typeof value==='object'&&!Array.isArray(value)){const card=document.createElement('div');card.className='list-item';renderObject(value,p,card);const a=document.createElement('div');a.className='item-actions';const del=document.createElement('button');del.className='danger';del.textContent='Entfernen';del.onclick=()=>{list.splice(index,1);changed();render()};a.append(del);card.append(a);container.append(card)}
 else{const row=document.createElement('div');row.className='array-scalar';const host=document.createElement('div');host.append(scalar(index,value,p,v=>list[index]=v).querySelector(':scope > div:last-child'));const del=document.createElement('button');del.className='danger';del.textContent='×';del.onclick=()=>{list.splice(index,1);changed();render()};row.append(host,del);container.append(row)}});
 const actions=document.createElement('div');actions.className='group-actions';const add=document.createElement('button');add.textContent='＋ Eintrag';add.onclick=()=>{let v='';if(list.length)v=typeof list[0]==='object'?JSON.parse(JSON.stringify(list[0])):(typeof list[0]==='number'?0:'');list.push(v);changed();render()};actions.append(add);container.append(actions)
}
function renderLoads(list,container){
 list.forEach((load,index)=>{const card=document.createElement('article');card.className='load-card';const head=document.createElement('div');head.className='load-head';head.innerHTML=`<b>${esc(load.name||'Unbenannte Last')}</b><span class="badge">${esc(load.type||'deferrable')}</span>`;card.append(head);renderObject(load,`controllable_loads[${index}]`,card);const a=document.createElement('div');a.className='item-actions';const del=document.createElement('button');del.className='danger';del.textContent='Last entfernen';del.onclick=()=>{if(confirm(`Last „${load.name||''}“ wirklich entfernen?`)){list.splice(index,1);changed();render()}};a.append(del);card.append(a);container.append(card)});
 const a=document.createElement('div');a.className='load-add';['deferrable','thermal'].forEach(type=>{const b=document.createElement('button');b.textContent=type==='thermal'?'＋ Thermische Last':'＋ Verschiebbare Last';b.onclick=()=>{list.push(JSON.parse(JSON.stringify(templates[type])));changed();render()};a.append(b)});container.append(a)
}
function renderValue(value,path,container,setter){if(Array.isArray(value))renderList(value,path,container);else if(value&&typeof value==='object')renderObject(value,path,container);else container.append(scalar(path.split('.').pop(),value,path,setter))}
function render(){const host=$('#editor');host.innerHTML='';Object.keys(state).forEach((key,idx)=>{const d=document.createElement('details');d.className='panel';d.open=allOpen||idx===0||key==='controllable_loads';const summary=document.createElement('summary');const count=state[key]&&typeof state[key]==='object'?(Array.isArray(state[key])?state[key].length:Object.keys(state[key]).length):1;summary.innerHTML=`<div><span class="panel-title">${esc(labels[key]||label(key))}</span><span class="panel-desc">${esc(descriptions[key]||'')}</span></div><span class="panel-count">${count} Einträge</span>`;d.append(summary);const body=document.createElement('div');body.className='panel-body';const value=state[key];if(Array.isArray(value))renderList(value,key,body);else if(value&&typeof value==='object')renderObject(value,key,body,true);else body.append(scalar(key,value,key,v=>state[key]=v));d.append(body);host.append(d)});filter()}
function filter(){const q=$('#search').value.trim().toLowerCase();document.querySelectorAll('.field-row').forEach(r=>r.classList.toggle('hidden',q&&!r.dataset.search.includes(q)));document.querySelectorAll('details.panel').forEach(p=>{const visible=[...p.querySelectorAll('.field-row')].some(r=>!r.classList.contains('hidden'));p.classList.toggle('hidden',q&&!visible);if(q&&visible)p.open=true})}
async function load(){try{const r=await fetch('/api/config.json',{cache:'no-store'});if(!r.ok)throw Error(await r.text());const d=await r.json();state=d.config;revision=d.revision;descriptions=d.descriptions||{};labels=d.labels||{};enums=d.enums||{};templates=d.load_templates||{};if(d.had_overrides){const n=$('#overlay-note');n.classList.remove('hidden');n.textContent='Lokale Dashboard-Overrides wurden in die angezeigte Konfiguration eingearbeitet. Beim Speichern werden sie konsolidiert und die Overlay-Datei geleert.'}render();setStatus('Bereit','Änderungen werden vor dem Speichern vollständig geprüft.','ok')}catch(e){setStatus('Konfiguration nicht verfügbar',e.message,'err')}}
async function submit(kind){const btn=kind==='save'?$('#save'):$('#validate');btn.disabled=true;try{if(kind==='save'&&!confirm('Konfiguration speichern und den EMS-Dienst geordnet neu starten?'))return;setStatus(kind==='save'?'Speichere Konfiguration …':'Prüfe Konfiguration …','Bitte warten','');const r=await fetch('/api/config/'+kind,{method:'POST',headers:{'Content-Type':'application/json','X-EMS-Config':'1'},body:JSON.stringify({config:state,revision})});const text=await r.text();let d;try{d=JSON.parse(text)}catch{throw Error(text.slice(0,300))}if(!r.ok)throw Error(d.error||text);if(kind==='validate'){const w=(d.result.warnings||[]).join(' ');setStatus('Konfiguration gültig',`${d.result.blocks} Bereiche · ${d.result.controllable_loads} steuerbare Lasten${w?' · '+w:''}`,'ok')}else{revision=d.result.revision;dirty=false;setStatus('Gespeichert – EMS startet neu',`Backup: ${d.result.backup}. Die Seite verbindet sich anschließend neu.`,'ok');waitRestart()}}catch(e){setStatus(kind==='save'?'Speichern fehlgeschlagen':'Prüfung fehlgeschlagen',e.message,'err')}finally{btn.disabled=false}}
function waitRestart(){let down=false,tries=0;const timer=setInterval(async()=>{tries++;try{const r=await fetch('/api/status.json?_='+Date.now(),{cache:'no-store'});if(!r.ok)throw 0;if(down){clearInterval(timer);location.href='/'}}catch{down=true}if(tries>40){clearInterval(timer);setStatus('Gespeichert','Neustartstatus konnte nicht automatisch bestätigt werden. Dashboard manuell öffnen.','ok')}},2000)}
$('#search').oninput=filter;$('#expand').onclick=()=>{allOpen=!allOpen;$('#expand').textContent=allOpen?'Alle Panels schließen':'Alle Panels öffnen';document.querySelectorAll('details.panel').forEach(d=>d.open=allOpen)};$('#validate').onclick=()=>submit('validate');$('#save').onclick=()=>submit('save');$('#theme').onclick=()=>{const dark=!document.documentElement.classList.contains('dark');document.documentElement.classList.toggle('dark',dark);localStorage.setItem('ems-theme',dark?'dark':'light')};addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue=''}});load();
</script></body></html>"""
