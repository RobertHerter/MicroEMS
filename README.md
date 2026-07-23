# EMS – Energy Management System

Kostenoptimale Steuerung von Haus-Akku, PV, Fahrzeug und steuerbaren Lasten
(z. B. Pool-Wärmepumpen). Läuft als Python-Dienst auf einem Raspberry Pi
(Raspberry Pi OS **Trixie** / Debian 13), berechnet per **MILP** die optimale
Steuertabelle für 48 h und

- steuert den E3DC-Speicher **direkt per RSCP** (optional, greift real ein) und/oder
- gibt die Sollwerte per **MQTT** an ein beliebiges Smart-Home-System aus
  (z. B. Homey, Home Assistant, ioBroker, Node-RED).

Die Eingangsdaten kommen aus **InfluxDB** (1.x/2.x) **oder** direkt aus den
Originalquellen (E3DC per RSCP, Wetter/Preis/PV aus kostenlosen APIs). **Eine
InfluxDB ist nicht zwingend** – das EMS läuft komplett **standalone** (siehe
„Datenquellen & Standalone"). Wo eine InfluxDB vorhanden ist, bleibt sie
optionaler Fallback je Signal und Ziel für den Writeback der Zukunftswerte.

> Eigennamen wie *Homey*, *Solcast*, *Open-Meteo*, *Energy-Charts* sind nur die
> im Standardfall verwendeten Beispiele. Die MQTT-Ausgabe funktioniert mit jedem
> MQTT-fähigen System; die Datenquellen sind je Signal austauschbar bzw. per
> InfluxDB/REST-Ingest ersetzbar. Fest ist nur der E3DC (RSCP) als Speicher-
> hardware – für andere Geräte müssten Feldnamen/Vorzeichen angepasst werden.

## Funktionsumfang

- **MILP-Optimierung** (PuLP + CBC/HiGHS) über 48 h in 15-min-Slots: Haus-Akku
  (DC/AC-Laden, Entladen), Fahrzeug, steuerbare Lasten – Ziel: minimale
  Netto-Stromkosten inkl. Terminalwert des Akku-Inhalts.
- **Eigene Prognosen** ohne Pflicht-Cloud: Hausverbrauch (Ähnliche-Tage oder ML),
  Strompreis (Spot + Tarifmodell), PV (Solcast **oder** freies pvlib-Modell).
- **PV-Quellen-Autowahl**: pvlib vs. Solcast werden rollierend gegen die realen
  Ertragsdaten bewertet; die produktive Quelle wird automatisch gewählt, das
  Unsicherheitsband aus echten Residuen kalibriert.
- **Direkte E3DC-Steuerung** per RSCP (an echter Hardware verifiziert) mit
  Watchdog, Rücklese-Verifikation und Fail-safe.
- **Steuerbare Lasten**: verschiebbar (Waschmaschine) und thermischer Speicher
  (Pool mit ein/aus-Wärmepumpen), inkl. Temperatur als MILP-Zustand.
- **Robustheit im Echtbetrieb**: Plausibilitäts-Grenzen für externe Eingaben,
  optionale Abend-Reserve, Sofort-Neuberechnung bei großer Live-Abweichung,
  Steuer-Ausfall-Alarm.
- **Beobachtbarkeit**: interaktives HTML-Dashboard, Ersparnis-Tracking gegen eine
  „Ohne-EMS"-Baseline **und** Validierung gegen die echten E3DC-Zähler,
  Invarianten-Prüfung, Drift-Monitor, Erklär-Tooltips.
- **Kalibrierung**: wöchentliche Nachführung von Verbrauchs-, PV- und
  Pool-Thermomodell aus den gesammelten Messdaten.

## Architektur

```
Datenquellen  ──►  EMS (Pi, Python)                        ──►  MQTT  ──►  Smart-Home
 InfluxDB          1. Verbrauchsprognose (72 h, Ähnliche-Tage/ML)      (Sollwerte, z. B.
  ODER direkt:     2. Eingangsdaten lesen (Preis/PV/SoC/…)  ──►  E3DC   Homey/HA/ioBroker)
  E3DC (RSCP)      3. MILP-Optimierung (48 h) → Steuertabelle  (RSCP-Steuerung, optional)
  Wetter-API       4. Ausgabe: MQTT + optional RSCP          ──►  InfluxDB (Writeback, opt.)
  Preis-API        5. optionaler Writeback in InfluxDB
  PV-Forecast      6. Dashboard (HTML) + JSON-API
```

Warum ein Pi-Dienst und keine App im Smart-Home-System: Die MILP-Optimierung
(192 Slots) und die Historien-Prognose brauchen Python-Bibliotheken und
Rechenleistung, die in einer App-Sandbox nicht sinnvoll verfügbar sind. Das
Zielsystem erhält nur die fertigen Sollwerte per MQTT.

## Module

| Datei | Aufgabe |
|-------|---------|
| `ems/config.py` | YAML-Konfiguration laden/validieren (typisierte Dataclasses) + Overlay |
| `ems/main.py` | Orchestrierung + CLI (`--loop`), HTTP-Server, systemd-Watchdog |
| `ems/optimizer.py` | MILP-Optimierer (PuLP, CBC/HiGHS): Steuertabelle 48 h |
| `ems/forecast.py` | Verbrauchsprognose: Ähnliche-Tage-Mittelung oder ML (HistGradientBoosting) |
| `ems/loads.py` | Steuerbare/verschiebbare Lasten im MILP (deferrable + thermischer Speicher) |
| `ems/rscp.py` | Direkte E3DC-Anbindung (RSCP/pye3dc): Live-Werte, 15-min-Historie, Steuerung |
| `ems/homey_mqtt.py` | MQTT-Client: Sollwerte, Status/Last-Will, Alerts, eingehende Kommandos, Last-Rückmeldungen |
| `ems/influx.py` | InfluxDB 1.x (InfluxQL) / 2.x (Flux): Lesen/Schreiben, 15-min-Resampling |
| `ems/local_history.py` | Lokale SQLite (Hauslast, Ist-Werte, Temperatur, Preis, PV-Prognose, Archive) + Quellen-Weichen |
| `ems/weather.py` | Temperatur + Einstrahlung von Open-Meteo (kein Key) |
| `ems/energycharts.py` + `ems/tariff.py` | Spotpreis (Energy-Charts) + Tarifmodell → Endkunden-Bezugspreis |
| `ems/solcast.py` | PV-Vorhersage von Solcast (mehrere Keys/Resourcen) + Dispatcher der aktiven PV-Quelle |
| `ems/pvforecast.py` | Freie PV-Ertragsprognose mit pvlib + Open-Meteo (kein Key) |
| `ems/pv_eval.py` | pvlib vs. Solcast gegen reale Erträge bewerten, Quelle wählen, p10/p90-Band kalibrieren |
| `ems/sanity.py` | Plausibilitäts-Grenzen für externe Eingaben (Preis/PV/Last) |
| `ems/savings.py` + `ems/savings_validate.py` | Ersparnis-Tracking + Gegenprüfung gegen die echten E3DC-Zähler |
| `ems/validate.py` + `ems/drift.py` | Invarianten-Prüfung eines Plans + Predicted-vs-Actual-Drift |
| `ems/explain.py` | Klartext-Begründung der Steuerentscheidungen (Dashboard-Tooltips) |
| `ems/pool_calibration.py` | Pool-Thermomodell (Verlust/Solar/Heizleistung) aus Messdaten fitten |
| `ems/ingest.py` | Externe Einspeisung (REST) von Live-/Historienwerten → Betrieb ohne RSCP/InfluxDB |
| `ems/dashboard.py` | Interaktives HTML-Dashboard + JSON-API |
| `tests/` | pytest-Suite (E2E, Optimierer-Randfälle, Prognose, Ersparnis, Diagnose …) |

## Eingangssignale

Hausverbrauch, Strompreis, Haus-Akku-SoC, PV-Erzeugung, PV-Vorhersage, optional
Fahrzeug-SoC und Einspeisevergütung. Strompreis und PV-Vorhersage werden auch für
die Zukunft gelesen. Alle Leistungen in **W**, Preise in **ct/kWh**, Energien in
**Wh**, Rechenraster **15 min**.

Jedes Signal kommt entweder **aus der InfluxDB** (Zuordnung Signal →
Measurement/Field unter `influxdb.signals`) **oder direkt aus der Originalquelle**
(Flags `e3dc_rscp` / `weather` / `tariff` / `solcast` / `pv_model`) **oder per
REST-Ingest**. Ist eine Direktquelle aktiv, wird das entsprechende InfluxDB-Signal
nicht mehr benötigt.

## Datenquellen & Standalone (ohne InfluxDB)

Alle Direktquellen sind unter `config.yaml` einzeln aktivierbar (Default aus,
ändert sonst nichts). Sind alle aktiv, läuft das EMS **komplett ohne InfluxDB**.

**E3DC per RSCP** (`e3dc_rscp`, Bibliothek `pye3dc`):
- `read_live`: aktueller SoC/PV/Last direkt vom Gerät (frischer als der DB-Umweg),
  Fallback auf InfluxDB.
- `control_enabled`: steuert den Speicher direkt (zusätzlich zur MQTT-Ausgabe, die
  parallel weiterläuft). **Greift real ein.** Die `EMS_REQ_SET_POWER`-Modi sind an
  echter Hardware verifiziert (pye3dc 0.10): **0**=auto, **1**=idle, **2**=Entladen,
  **3**=Laden (aus PV), **4**=Netzladen; der Wert ist die Gesamtleistung. `apply_control`
  setzt je Slot Netzladen (Mode 4), Netz-Entladen (Mode 2, nur bei
  `allow_grid_discharge`) oder reine Lade-/Entlade-Grenzen (persistente Limits, kein
  Mode-Eingriff). Mit `verify_control: true` werden die Limits nach dem Schreiben
  zurückgelesen; Abweichungen außerhalb `control_verify_tolerance_w` melden einen
  Steuer-Alarm (`ems/alert`, Dashboard). Aktive Modi werden alle 5 s per Watchdog
  erneuert (der E3DC fällt sonst nach ~10 s auf auto zurück); beim Beenden schaltet
  der Dienst aktiv auf auto zurück (Fail-safe).
- `history_source`: die 15-min-Hauslast kommt aus der lokalen SQLite statt aus der
  InfluxDB. Einmaliger Backfill: `python rscp_import.py --config config.yaml --days
  730` (1 RSCP-Aufruf je Fenster, mehrere Stunden); danach `history_source: true`.
  Zyklisch werden neue Fenster mit Reifeverzug (`history_settle_minutes`) und
  Überlappung (`history_overlap_hours`) nachgeführt.
- **Ist-Werte lokal** (Tabelle `actuals`): jeder Zyklus protokolliert den
  E3DC-Live-Snapshot; Intraday-Korrektur, Ersparnis, Drift und Dashboard-Ist-Kurven
  lesen dann daraus (zentrale Weiche `read_actual_signal`).

**Temperatur & Einstrahlung** (`weather`, Open-Meteo, kein Key): stündlicher Abruf
(`past_days`/`forecast_days`) in Tabelle `temperature`/`radiation`, aufs Slot-Raster
interpoliert. Tiefe Historie einmalig via `weather_backfill.py`. Zukunftswerte werden
unveränderlich archiviert (`weather_forecast_archive`) für ehrliche Backtests.

**Bezugspreis** (`tariff`, Energy-Charts, kein Key): Day-Ahead-Spot je Zyklus in
Tabelle `spot_price`; das Tarifmodell rechnet daraus den Endkunden-Bezugspreis:

```
netto  = spot·(1+markup_percent/100) + markup_ct_kwh + levies_ct_kwh + netzentgelt
brutto = netto·(1+vat_percent/100)          # MwSt auf alles
```

Netzentgelt `static`/`included`/`14a` (§14a EnWG zeitvariabel). Tiefe Historie via
`energycharts_backfill.py`. Fehlende Folgetag-Preise ergänzt die Ähnliche-Tage-Schätzung.

**PV-Vorhersage** – zwei austauschbare Quellen (nicht gleichzeitig aktiv):
- `solcast`: rooftop-site-Forecast inkl. P10/P90, mehrere Keys/Resourcen,
  `combine: sum|mean`, Abruf-Budget `calls_per_key_per_day` über das Tageslicht
  verteilt. Tiefe Historie via `solcast_import.py`.
- `pv_model`: **freies pvlib-Modell** (kein Key/Kontingent) aus Paneldaten
  (kWp/Neigung/Azimut je Ausrichtung) + Open-Meteo-Einstrahlung. `shadow: true`
  rechnet es nur zum Vergleich mit, ohne den Optimierer zu beeinflussen.
- **Autowahl** (`pv_source_selection`): sobald beide Quellen genügend gemeinsame
  Archiv-Erfahrung haben, wählt `pv_eval.select_source` die im WAPE bessere Quelle
  (nur aus echten Rolling-Origin-Archiven, nie aus dem optimistischen Cache); die
  Wahl wird mit Begründung persistiert. `python pv_source_report.py` zeigt den
  Vergleich manuell und empfiehlt das kalibrierte p10/p90-Band.

Beide PV- und alle übrigen Signale sind auch per **REST-Ingest** einspielbar
(`ingest`): so kann ein Fremdsystem die Daten liefern, RSCP/InfluxDB entfallen.
Payloads siehe `config.example.yaml`.

## Installation

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip coinor-cbc mosquitto mosquitto-clients

sudo mkdir -p /opt/ems && sudo chown "$USER" /opt/ems
# Projektdateien nach /opt/ems kopieren (ems/, requirements.txt, config.example.yaml, *.service ...)
cd /opt/ems
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # bzw. requirements.lock (exakt getestete Versionen)
# nur für Entwicklung und Tests:
# pip install -r requirements-dev.txt

cp config.example.yaml config.yaml     # anpassen – siehe „Konfiguration"

# Einmaliger Testlauf:
python -m ems.main --config config.yaml --log-level INFO
```

> PuLP bringt einen CBC-Solver mit; das System-Paket `coinor-cbc` ist optional als
> robuste Alternative. Für `optimization.solver: highs` zusätzlich `highspy`.

### Als Dienst (systemd)

```bash
sudo useradd -r -s /usr/sbin/nologin ems 2>/dev/null || true
sudo chown -R ems:ems /opt/ems
sudo cp ems.service \
        ems-kalibrierung.service ems-kalibrierung.timer \
        ems-savings.service ems-savings.timer \
        ems-backup.service ems-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ems.service \
        ems-kalibrierung.timer ems-savings.timer ems-backup.timer
journalctl -u ems -f
```

Der Dienst rechnet im Intervall `general.run_interval_minutes` (Standard 15 min)
neu, läuft gehärtet als Benutzer `ems` (Port 80 über `CAP_NET_BIND_SERVICE`,
Schreibzugriff nur auf `/opt/ems`) und mit **systemd-Watchdog** (Neustart, wenn das
Lebenszeichen ausbleibt). Die Timer:

- **`ems-kalibrierung.timer`** (So 03:00): Verbrauchs-/PV-Kalibrierung
  (`kalibrierung.py`) inkl. pvlib-p10/p90-Bandkalibrierung + Pool-Thermomodell
  (`ems.pool_calibration --apply`). Korrekturprofil, PV-Band und Poolparameter
  werden spätestens im nächsten EMS-Zyklus ohne Dienstneustart übernommen.
- **`ems-savings.timer`** (täglich 02:45): validiert die Vortags-Ersparnis gegen
  die echten E3DC-Zähler (`savings_check.py --persist`).
- **`ems-backup.timer`** (wöchentlich): sichert die unversionierten Dateien
  (`config.yaml` mit Zugangsdaten, Kalibrierprofile, Ersparnis-Status) via
  [backup.sh](backup.sh). **Für echte Sicherheit ein externes Ziel setzen**
  (`Environment=EMS_BACKUP_DIR=/mnt/nas/ems-backup` in `ems-backup.service`).

## Konfiguration

Zentrale Datei `config.yaml` (aus `config.example.yaml` kopieren – dort ist jeder
Block kommentiert). `config.yaml` enthält **Zugangsdaten** (InfluxDB, MQTT, E3DC,
API-Keys) und ist deshalb **nicht** im Git; sie wird vom Backup-Timer gesichert.

Interaktive Änderungen (Dashboard-Steuerpanel) und die Kalibrierung schreiben in
eine **Overlay-Datei `config_overrides.yaml`**, die beim Laden über `config.yaml`
gelegt wird – so überdauern sie einen Neustart, ohne die kommentierte Basisdatei
anzutasten. `config_overrides.yaml`, `kalibrierung.yaml` und
`kalibrierung_profil.yaml` sind anlagenspezifische Laufzeitdateien: Sie bleiben
lokal, werden nicht versioniert und vom Backup-Timer gesichert.

Wichtige Blöcke:

| Block | Inhalt |
|-------|--------|
| `general` | Zeitzone, Horizont (48 h), Slot-Länge, Rechenintervall |
| `house_battery` / `inverter` | Kapazität, Lade-/Entladeleistungen, Wirkungsgrade, SoC-Grenzen, WR-/Netzanschlussgrenzen |
| `vehicle` | Auto-Akku, Lade-Min/-Max, Ziel-SoC, Abfahrtzeit(en), Ladekurve |
| `optimization` | Solver, MIP-Gap, Strafterme, Ladestrategie (`auto/peak/asap/late`), **Abend-Reserve** |
| `feed_in` / `tariff` | Einspeisevergütung; bei `zero_at_negative_price` wird in Negativpreis-Slots Laden/Abregeln statt Einspeisen priorisiert; Bezugspreis-Tarifmodell |
| `weather` / `solcast` / `pv_model` / `pv_source_selection` | Wetter- und PV-Quellen + Autowahl |
| `e3dc_rscp` | RSCP-Zugang, `read_live`/`control_enabled`/`history_source`, Verifikation |
| `controllable_loads` | verschiebbare + thermische Lasten (Pool-WP) |
| `sanity` | Plausibilitäts-Grenzen (Preis-Spike, PV-Cap, negative Werte) |
| `recalc` | Sofort-Neuberechnung bei großer Live-Abweichung vom Plan |
| `monitoring` | Drift-, Solver- und Ausführungs-Audit, Alarm-Schwellen |
| `mqtt` / `dashboard` / `influxdb` | Ausgabe/Anbindung: Broker, Web/API, DB |
| `savings` / `calibration` / `report` | Ersparnis-Status, Kalibrierprofile, Debug-Report |

### Anlagenwerte & Steuergrößen

Konfigurierbar sind u. a. Kapazitäten, Leistungsgrenzen, Wirkungsgrade und SoC-
Grenzen von Haus- und Auto-Akku sowie Einspeisevergütung (fest oder aus DB).
Der Optimierer bestimmt je 15-min-Slot: Haus-Akku **DC-Laden** (nur PV), **AC-Laden**
(Netz), **Entladen** und **Auto-Ladeleistung** (0 oder Min…Max). Nebenbedingungen:
SoC-/Leistungsgrenzen, WR-Durchsatz, kein gleichzeitiges Laden/Entladen, Auto-Ziel-SoC
zur Abfahrt. Ziel: minimale Netto-Stromkosten inkl. Terminalwert des Akku-Inhalts.

### Abend-Reserve (optional)

`optimization.evening_reserve_*` hält den Akku (weiche Nebenbedingung, nie
infeasible) über einem Mindest-SoC, damit er nicht vor der teuren Abendspitze
leerläuft. Fest (`evening_reserve_soc_percent` + Fenster) oder **adaptiv**
(`evening_reserve_auto`): Höhe = Energie für die Restlast während der abendlichen
Preisspitze, Fenster von `hold_from_hour` bis zum Peak-Beginn.

### Steuerbare / verschiebbare Lasten (`controllable_loads`)

Optionale Liste zusätzlicher Lasten, die der Optimierer mitplant und in die
günstigsten/PV-reichsten Slots legt. Zwei Typen (`ems/loads.py`):

- **`deferrable`** – muss `runtime_minutes` im Fenster laufen; Leistung konstant
  (`power_w`) oder als 15-min-Kurve (`power_profile_w`).
- **`thermal`** – thermischer Speicher (Pool): Temperatur ist ein **MILP-Zustand**
  im Band `[min_c, max_c]`, geheizt über `stages` (ein/aus-Wärmepumpen, per
  `requires` koppelbar); Verlust `~ loss_w_per_k·(T−T_außen)`, optionaler solarer
  Eintrag, Ist-Temperatur aus `temp_signal`. Je Stufe geht der Schaltbefehl an
  `control_topic`; optionale Rückmeldung über `feedback_topic` (an/aus) bzw.
  `power_topic` (gemessene Leistung). Bei eigenem WP-Thermostat (`thermostat: true`)
  ist das Signal eine Heiz-**Freigabe**. Das Pool-Thermomodell wird wöchentlich aus
  den Messdaten kalibriert (`pool_calibration.py`).

Leere Liste (Default) = keine zusätzlichen Variablen.

## Anbindung an ein Smart-Home-System (MQTT)

Das EMS publiziert bei jedem Zyklus die Sollwerte des laufenden Slots. Ein
beliebiger MQTT-Client (Homey, Home Assistant, ioBroker, Node-RED …) abonniert die
Topics und schreibt die Werte auf die Geräte-Capabilities.

```
ems/setpoint/batt_charge_limit_w      Ladelimit (Hardware-Max = frei laufen)
ems/setpoint/batt_discharge_limit_w   Entladelimit (Hardware-Max = frei laufen)
ems/setpoint/batt_grid_charge_w       Netzladen erzwingen (Akku <- Netz)
ems/setpoint/batt_grid_discharge_w    Netz-Entladen (Akku -> Netz)
ems/setpoint/charge_limited           true/false
ems/setpoint/discharge_limited        true/false
ems/setpoint/car_charge_w             z. B. 4000
ems/setpoint/mode                     "auto" | "grid_charge" | "hold" | ...
ems/setpoint/updated                  ISO-Zeitstempel des Slots
ems/loads/<name>                      Sollzustand steuerbarer Lasten (0/1, retained)
ems/schedule                          komplette 48-h-Tabelle als JSON (retained)
ems/status                            "online" | "offline" (retained, Last Will)
ems/alert                             Störungen als JSON {level, message, time}
```

Eingehende Kommandos (an das EMS):

```
ems/cmd/recalc              sofortige Neuberechnung (Payload egal)
ems/cmd/car_boost           "1"/"0": Auto sofort mit Max-Leistung laden bis Ziel-SoC
ems/cmd/car_departure_time  "HH:MM" | "default" | "off"/"urlaub" (kein Ziel-SoC erzwingen)
ems/cmd/car_target_soc      Ziel-SoC in % (1..100) | "default"
ems/cmd/min_soc             Haus-Akku Minimum-SoC in % | "default"
ems/cmd/max_soc             Haus-Akku Maximum-SoC in % | "default"
ems/cmd/load/<name>         steuerbare Last aktivieren/deaktivieren | "default"
```

Parameter-Kommandos **mit Retain** publizieren, dann überstehen sie einen
EMS-Neustart. Die wirksamen Werte meldet das EMS unter `ems/vehicle/*` und
`ems/battery/*` zurück.

**Fail-safe:** Sollwerte werden **ohne** Retain publiziert – fällt das EMS aus, hält
der Broker keine veralteten Steuerbefehle vor. Zusätzlich hält der Loop eine
stehende Verbindung mit **Last Will**: stirbt der Prozess, setzt der Broker selbst
`ems/status = offline`. Empfohlener Watchdog: wenn `ems/status` auf `offline`
wechselt (oder `ems/setpoint/updated` ~35 min ausbleibt), alle Limits auf
Hardware-Maximum setzen (Eigenverbrauchs-Automatik des E3DC).

## Dashboard

![EMS Dashboard (Beispielausgabe mit synthetischen Daten)](dashboard_beispiel.png)

Nach jedem Lauf entsteht `dashboard.html` (im Loop-Betrieb per HTTP auf Port 80,
Auto-Reload nach jeder Neuberechnung):

Die dafür benötigte `plotly.min.js` wird beim ersten Dashboard-Lauf automatisch
aus dem installierten Plotly-Paket erzeugt. Das große, generierte Browser-Bundle
wird deshalb nicht im Git-Repository gespeichert.

- **KPI-Kacheln**: Netto-Kosten Horizont, Ersparnis gesamt (inkl. an den Zählern
  bestätigter Ersparnis), Modus jetzt (mit Limits), Eingriffe im Plan; die
  Planprüfung erscheint als eigenes Banner darüber.
- **E3/DC-Livekacheln** (Default 5 s): Solarerzeugung, Hauslast, Netzfluss,
  Batterieleistung, Akku-SoC, Wallbox – dazu, wenn vorhanden, die Pool-Ist-
  Temperatur und die Außentemperatur. Ein zweiter Bereich zeigt PV-Tagesprognose,
  aktuellen Bezugspreis sowie die E3/DC-Zählerstände des Tages für PV, Haus,
  Netzbezug/-einspeisung und Speicherladung/-entladung. PV-Soll bis zur aktuellen
  Uhrzeit, Ist-Abweichung, Zähleralter und Energiebilanz machen unvollständige oder
  veraltete Tagesdaten sichtbar. Momentanwerte werden 5 s, die summierten
  Tageszähler ressourcenschonend einmal pro Minute aktualisiert; auf dem Handy ist
  der Tagesbereich standardmäßig eingeklappt.
- **Leistung** (PV mit p10–p90-Band, Verbrauch, Netz, Einspeise-Linie; Ist
  durchgezogen, Prognose gestrichelt), **Ladezustand**, **Strompreis** +
  Einspeisevergütung, **Steuerung**, **Modus-Zeitleiste** (Farbstreifen + Hover-
  Klartext), **Steuerbare Lasten** und **Temperaturen** (erwartet vs. echt).
- Farbcodierte, einklappbare Diagnose-Panels (Prognosequalität, Betriebsdiagnose,
  Pool-Rückkopplung) – Status auch eingeklappt sichtbar.
- **Interaktives Steuerpanel** (`dashboard.controls_enabled: true`): Lasten an/aus +
  Kernparameter, Optimierungsmodus, manuelles Akku-Laden/-Entladen – ohne MQTT.
  Die automatische direkte E3/DC-Steuerung lässt sich dort ebenfalls sicher
  schalten; beim Ausschalten werden aktive RSCP-Modi und SmartPower-Limits sofort
  freigegeben und am Gerät zurückgelesen. Direkte RSCP-Steuerung und die davon
  unabhängigen MQTT-Sollwerte sind im Panel klar getrennt, während das reine
  Live-Monitoring weiterläuft.
- **Grafischer Planvergleich**: Akku-, Netz- und SoC-Verlauf des aktiven Plans
  gegen eine Vorschau mit anderem Optimierungsmodus, bevor dieser übernommen wird.
- **Manuelle Akku-Zeitplanung**: Netzladen/Entladen auf einem 48-h-Zeitstrahl planen
  (persistent, auch ohne offenen Browser ausgeführt, abbrechbar; SoC-/Leistungs-
  grenzen geprüft).

Die Beispielausgabe (**synthetische Daten**) liegt als
[dashboard_beispiel.html](dashboard_beispiel.html) bei – regenerierbar mit
`python beispiel_dashboard.py`. Screenshot erneuern:

```bash
chromium --headless --no-sandbox --hide-scrollbars --window-size=1500,1110 \
  --screenshot=dashboard_beispiel.png dashboard_beispiel.html
```

### Webserver & API (Basic Auth)

Der HTTP-Server liefert das Dashboard und JSON-Endpunkte: `/api/data.json`
(vollständiger Optimierungs-Zustand, ideal für Grafana/Node-RED), `/api/live.json`
(aktueller E3/DC-Snapshot, Intervall `dashboard.live_refresh_seconds`, `0` = aus).
Die gesamte Weboberfläche lässt sich mit **Basic Auth** absichern
(`dashboard.username`/`password`; leer = ungeschützt).

## Ersparnis-Tracking & Validierung

Für jeden abgeschlossenen Slot vergleicht das EMS die **tatsächlichen** Netzkosten
(gemessener Netzbezug/-einspeisung × Preis) mit einer Simulation, was der E3DC
**ohne EMS** getan hätte (`savings.py`, Zustand in `savings_state.json`,
Measurement `ems_savings`). Zusätzlich prüft `savings_check.py` (täglich per Timer)
die Vortags-Ersparnis **unabhängig gegen die echten E3DC-Energiezähler**: es liest
die gemessenen 15-min-Energieaggregate, rechnet Ist vs. „Ohne-EMS"-Baseline und
deckt über die Energiebilanz Vorzeichen-/Integrationsfehler auf. Die kumulierte,
zähler-bestätigte Ersparnis erscheint im Dashboard.

```bash
python savings_check.py --config config.yaml --days 7    # Fenster-Report
python savings_check.py --config config.yaml --summary   # kumuliert (nur DB)
```

## Diagnose & Modell-Prüfung

- **`ems/validate.py`** – prüft jeden Plan gegen Invarianten (SoC-/Leistungsgrenzen,
  Energiebilanz, kein gleichzeitiges Laden/Entladen, DC-Laden nur aus PV,
  Einspeisebegrenzung, Ausführbarkeit) plus ökonomische Plausibilität (nie teurer
  als die Baseline). Läuft in Tests, im Backtest und live (Banner + `ems/alert`).
- **`ems/drift.py`** – Predicted-vs-Actual-SoC-Drift (MAE, Measurement `ems_drift`),
  Warnung über der Schwelle. Deckt Modellfehler auf (Wirkungsgrade, Standby, Alterung).
- **Sanity-Grenzen** (`sanity`) – begrenzen Preis-Spikes, negative/überhöhte PV und
  negative Last vor dem Solve; ein einzelner API-Ausreißer verzerrt keinen Zyklus.
- **Ausführungs-Audit + Auto-Recalc** – vergleicht Soll/Ist des laufenden Slots
  (Akku/SoC; Netz nur informativ, da Bilanz-Residuum) und rechnet bei großer
  Live-Abweichung (`recalc`) sofort neu.
- **Debug-Report-Button** (`report.enabled: true`) – lädt den Schnappschuss des
  letzten Laufs (ohne Zugangsdaten) für die Offline-Analyse.
- **`backtest.py`** – spielt vergangene Tage durch den Optimierer und prüft jeden
  Plan; findet Modellfehler über Monate echter Daten in Minuten:

  ```bash
  python backtest.py --config config.yaml --days 120
  python backtest.py --config config.yaml --start 2026-01-01 --end 2026-03-01
  python backtest.py --config config.yaml --days 30 --historical-forecasts
  ```

  Mit `--historical-forecasts` wird ausschließlich der jeweils produktiv
  archivierte Prognosestand verwendet (kein nachträglich bekannter Ist-Verlauf).
  Schreibt nichts in die DB; als Regressions-Sweep nach jeder Modelländerung
  laufen lassen (erwartet: 0 Fehler, 0 negative Ersparnis-Tage).

## Test

```bash
pytest                            # komplette Suite (parallel via pytest-xdist)
pytest -m "not slow"              # schneller Smoke-Lauf ohne die schweren MILP-/ML-Tests
python -m tests.test_synthetic    # nur der End-to-End-Lauf
```

Ohne InfluxDB/MQTT lauffähig. Abgedeckt: End-to-End (Prognose, Optimierung,
Fallback, Dashboard), Optimierer-Randfälle (Strategien, negative Preise, Arbitrage,
DST-Tage), Prognose, Invarianten + Fuzz/metamorph, PV-Auswertung, Sanity, Ersparnis-
Gegenprüfung, Ausführungs-Audit, Auto-Recalc.

## Modellannahmen

- PV ist am DC-Bus verfügbar; DC-Laden reduziert die an den WR geführte PV-Leistung
  (`pv_to_ac = pv − dc_charge ≥ 0`). AC-Laden (Netz) hat einen eigenen, schlechteren
  Wirkungsgrad (`ac_charge_efficiency`).
- Intraday-Korrektur: Last und PV mit getrennten Fenstern, Faktorgrenzen und
  Abklingzeiten; Totzone und maximale Änderung je Lauf verhindern hektische Sprünge.
- Datenlücken werden nicht unbegrenzt interpoliert: fehlende PV/Solarstrahlung = 0,
  fehlende Preise = Historienmedian/Fixpreis, komplett fehlende Last-Historie =
  `forecast.fallback_load_w`. Bei Solcast müssen für Zukunftsslots alle Teilanlagen
  vorhanden sein; unvollständige P10-Reihen werden nicht zur Peak-Planung genutzt.
- Optional `feed_in.zero_at_negative_price` (Solarspitzengesetz): Einspeisung in
  Slots mit negativem **Börsenpreis** mit 0 ct bewertet. Der Rohpreis bleibt dabei
  getrennt vom Endkundenpreis. Abregelung wird nur geplant, wenn eine physische
  WR-Einspeisegrenze oder `e3dc_rscp.curtailment_control_enabled` vorhanden ist;
  der RSCP-Sollwert wird zurückgelesen und beim Beenden wieder aufgehoben.
- Geschätzte Folgetag-Preise werden zur Mitte gestaucht (`forecast.price_damping`).
- Terminalwert (`"auto"`): fallende Grenzwert-Kurve in 3 Segmenten – die letzte
  gespeicherte kWh ist weniger wert als die erste.
- Slot 0 wird mit Live-Messwerten verankert.
- Nach jedem Lauf vergleicht ein rein diagnostischer Schattenlauf `asap`,
  `peak` und `late` nach Kosten, Netzbezug, Einspeisung/-spitze,
  Abregelung sowie Max-/End-SoC. Alle Modi erscheinen gleichzeitig als kompakte
  Vergleichskarten und gemeinsame Akku-/SoC-Kurven aus exakt denselben
  Solver-Ergebnissen. Es gibt keine zweite Vergleichsrechnung; die Empfehlung
  bleibt unverbindlich. `auto` erscheint nicht als vierte Strategie; stattdessen
  wird die für den aktuellen Tag automatisch gewählte Strategie `asap` oder
  `peak` markiert. Jede Kachel zeigt außerdem die Anzahl der Eingriffs-Slots.
- Für `late` wird die Ziel-Konfidenz ohne Netzladen separat mit Erwartungs-PV und
  PV-P10 geprüft: `100 % sehr wahrscheinlich`, `nur mit Erwartungsprognose
  erreichbar` oder `unter P10 nicht erreichbar`.
- Jede besondere Planentscheidung weist ihren realen Ausführungspfad aus:
  direktes RSCP, MQTT-Sollwert, physische Wechselrichterfunktion oder nur Modell.
- Abgeschlossene Slots werden gegen die E3DC-Zählerenergie geprüft. Das Audit
  berücksichtigt dabei die typische 75-minütige Bereitstellungsverzögerung,
  während parallel alle fünf Sekunden eine vorläufige Prüfung aus geglätteten
  Livewerten läuft. Kurze Datenlücken werden interpoliert, längere Lücken nicht;
  erst wiederholte Abweichungen lösen einen Live-Alarm aus. Die spätere
  Zählerprüfung bestätigt oder verwirft diesen Verdacht mit exakter Energie,
  zeigt Energieabweichung und Einspeisegrenze und unterscheidet Geräte-,
  Prognose- und Modellursachen. Ein SoC wird nur mit einem zeitlich passenden
  historischen Ist-Wert verglichen, nie mit dem aktuellen Live-SoC.
- Auto: Schalt-Malus je Einschaltvorgang, optionale Ladekurve; der Ziel-SoC ist eine
  **weiche** Nebenbedingung (`car_target_penalty_ct_kwh`) und meldet Fehlmengen per
  `ems/alert`. Ohne Fahrzeug-SoC wird das Auto nicht mitoptimiert; Abfahrtzeiten je
  Wochentag über `vehicle.departure_times`.
- WR-Durchsatz begrenzt (`pv_to_ac + Entladung + AC-Laden ≤ WR_max`); optionale
  Einspeisebegrenzung `inverter.max_export_w` (gilt auch für die Baseline).
- Lade-/Entladewirkungsgrade wirken auf die SoC-Bilanz.

## Lizenz

**PolyForm Noncommercial License 1.0.0** – siehe [LICENSE](LICENSE).

Die nichtkommerzielle Nutzung (privat, Forschung, Bildung, gemeinnützige
Organisationen usw.) ist ausdrücklich erlaubt; **kommerzielle Nutzung ist nicht
lizenziert**. Es handelt sich damit um *source-available*-Software, nicht um eine
OSI-„Open-Source"-Lizenz (eine solche dürfte kommerzielle Nutzung nicht
ausschließen). Für eine kommerzielle Lizenz bitte den Autor kontaktieren.
