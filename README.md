# EMS – Energy Management System

Kostenoptimale Steuerung von Haus-Akku, PV und Fahrzeug. Läuft als
Python-Dienst auf einem Raspberry Pi (Raspberry Pi OS **Trixie** / Debian 13),
liest Eingangsdaten aus **InfluxDB** (1.x oder 2.x), berechnet per **MILP** die
optimale Steuertabelle für 48 h und gibt die Steuerbefehle per **MQTT** an
Homey aus. Zusätzlich werden alle Zukunftswerte (Steuerbefehle, prognostizierte
SoCs, Zustände) zurück in InfluxDB geschrieben.

## Architektur

```
InfluxDB  ──►  EMS (Pi, Python)                              ──►  MQTT  ──►  Homey
(1.x/2.x)      1. Verbrauchsprognose (72h, Ähnliche-Tage)         (Sollwerte)
               2. Eingangsdaten lesen (Preis/PV-Vorhersage,      ──►  InfluxDB
                  aktueller Haus-/Auto-SoC)                            (Steuertabelle +
               3. MILP-Optimierung (48h) → Steuertabelle              Prognose-SoCs)
               4. MQTT-Ausgabe an Homey
               5. Writeback in InfluxDB
               6. Dashboard (HTML)
```

Warum Pi-Dienst und nicht Homey-App: Die MILP-Optimierung (192 Slots) und die
Historien-Prognose brauchen Python-Bibliotheken und Rechenleistung, die in der
Homey-App-Sandbox nicht sinnvoll verfügbar sind. Homey erhält nur die fertigen
Steuerbefehle per MQTT.

## Module

| Datei | Aufgabe |
|-------|---------|
| `ems/config.py` | YAML-Konfiguration laden/validieren (typisierte Dataclasses) |
| `ems/influx.py` | InfluxDB-Abstraktion 1.x (InfluxQL) / 2.x (Flux), Lesen/Schreiben, 15-min-Resampling |
| `ems/forecast.py` | Hausverbrauchs-Prognose per Ähnliche-Tage-Mittelung (Wochentag/Feiertag/Monat/Jahreszeit/Temperatur, Rezenz-Gewichtung) |
| `ems/optimizer.py` | MILP-Optimierer (PuLP/CBC): Steuertabelle 48 h |
| `ems/homey_mqtt.py` | MQTT: Steuerbefehle an Homey, Status/Last-Will, Alerts, Kommandos |
| `ems/savings.py` | Ersparnis-Tracking: Ist-Kosten vs. simulierte "Ohne-EMS"-Baseline |
| `ems/dashboard.py` | Interaktives HTML-Dashboard (heute + Vorhersage + Steuerbefehle) |
| `ems/main.py` | Orchestrierung + CLI (`--loop` für Dauerbetrieb), systemd-Watchdog |
| `tests/` | pytest-Suite (E2E synthetisch, Optimierer-Randfälle, Prognose, Ersparnis) |

## Eingangssignale (aus InfluxDB)

Hausverbrauch, Strompreis, Haus-Akku-SoC, PV-Erzeugung, PV-Vorhersage,
optional Fahrzeug-SoC, optional Einspeisevergütung. Strompreis und PV-Vorhersage
werden auch für die Zukunft gelesen. Alle Leistungen in **W**, Preise in
**ct/kWh**, Energien in **Wh**. Berechnung auf **15-min-Slots**.

Die Zuordnung Signal → Measurement/Field wird in `config.yaml` unter
`influxdb.signals` festgelegt und an die eigene InfluxDB-Struktur angepasst.

## Konfigurierbare Anlagenwerte

Haus-Akku-Kapazität, Auto-Akku-Kapazität, Haus-Akku max. DC-Ladeleistung,
Haus-Akku max. AC-Ladeleistung, Haus-Akku max. Entladeleistung, Wechselrichter
max. AC-Leistung, Auto max./min. Ladeleistung, min./max. Haus-SoC, min. Auto-SoC,
Ziel-Auto-SoC + Abfahrtzeit, Einspeisevergütung (fest oder aus DB). Siehe
`config.example.yaml`.

## Steuergrößen (Optimierung)

Pro 15-min-Slot über 48 h:
- Haus-Akku **DC-Ladeleistung** (nur aus PV)
- Haus-Akku **AC-Ladeleistung** (aus dem Netz)
- Haus-Akku **DC-Entladeleistung**
- **Auto-Ladeleistung** (semikontinuierlich: 0 oder zwischen Min und Max)

Nebenbedingungen: SoC-Grenzen Haus/Auto, Leistungsgrenzen, Wechselrichter-
Durchsatz, **kein gleichzeitiges Laden/Entladen** (per Binärvariablen erzwungen),
**Auto-Ziel-SoC zur Abfahrtzeit**. Ziel: Minimierung der Netto-Stromkosten
(Import·Preis − Export·Einspeisevergütung) inkl. Terminalwert des Akku-Inhalts.

## Installation auf dem Pi (Trixie)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip coinor-cbc mosquitto mosquitto-clients

sudo mkdir -p /opt/ems && sudo chown $USER /opt/ems
# Projektdateien nach /opt/ems kopieren (ems/, requirements.txt, config.example.yaml ...)
cd /opt/ems
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # oder: requirements.lock (exakt getestete Versionen)

cp config.example.yaml config.yaml
# config.yaml anpassen: InfluxDB-Version/Zugang, Signal-Mapping, Anlagenwerte, MQTT

# Einmaliger Testlauf:
python -m ems.main --config config.yaml --log-level INFO
```

> Hinweis: PuLP bringt einen CBC-Solver mit; das System-Paket `coinor-cbc` ist
> optional als robuste Alternative.

### Als Dienst (systemd)

```bash
sudo useradd -r -s /usr/sbin/nologin ems 2>/dev/null || true
sudo chown -R ems:ems /opt/ems
sudo cp ems.service ems-kalibrierung.service ems-kalibrierung.timer \
        ems-backup.service ems-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ems.service ems-kalibrierung.timer ems-backup.timer
journalctl -u ems -f
```

Der Dienst rechnet im Intervall `general.run_interval_minutes` (Standard 15 min)
neu. Er läuft gehärtet als Benutzer `ems` (Port 80 über
`CAP_NET_BIND_SERVICE`, Schreibzugriff nur auf `/opt/ems`) und mit
**systemd-Watchdog**: bleibt das Lebenszeichen 35 min aus (Prozess hängt),
startet systemd den Dienst neu.

### Backup

`ems-backup.timer` sichert wöchentlich die unversionierten Dateien
(`config.yaml` mit Zugangsdaten, Kalibrierprofile, Ersparnis-Status) per
[backup.sh](backup.sh) nach `/opt/ems/backup` (letzte 8 Stände). **Wichtig:**
Das lokale Ziel schützt nicht vor einem Ausfall des Datenträgers – für echte
Sicherheit in `ems-backup.service` ein externes Ziel setzen
(`Environment=EMS_BACKUP_DIR=/mnt/nas/ems-backup`).

## Homey-Anbindung (MQTT)

Das EMS publiziert bei jedem Zyklus die Sollwerte des laufenden Slots:

```
ems/setpoint/batt_charge_limit_w      Ladelimit (Hardware-Max = frei laufen)
ems/setpoint/batt_discharge_limit_w   Entladelimit (Hardware-Max = frei laufen)
ems/setpoint/batt_grid_charge_w       Netzladen erzwingen (Akku <- Netz)
ems/setpoint/batt_grid_discharge_w    Netz-Entladen (Akku -> Netz)
ems/setpoint/charge_limited           true/false
ems/setpoint/discharge_limited        true/false
ems/setpoint/car_charge_w             z.B. 4000
ems/setpoint/mode                     "auto" | "grid_charge" | "hold" | ...
ems/setpoint/updated                  ISO-Zeitstempel des Slots
ems/schedule                          komplette 48h-Tabelle als JSON (retained)
ems/status                            "online" | "offline" (retained, Last Will)
ems/alert                             Störungen als JSON {level, message, time}
```

Eingehende Kommandos (von Homey an das EMS):

```
ems/cmd/recalc      sofortige Neuberechnung anstoßen (Payload egal)
ems/cmd/car_boost   "1"/"0": Auto sofort mit Max-Leistung laden, bis der
                    Ziel-SoC erreicht ist (überschreibt car_charge_w)
```

`ems/alert` meldet z.B. eine nicht-optimale Optimierung (Fallback aktiv) oder
einen fehlgeschlagenen Zyklus – ideal für einen Homey-Push-Benachrichtigungs-Flow.

In Homey die **MQTT Client**-App auf diese Topics abonnieren und die Werte per
Flow auf die Geräte-Capabilities (Ladeleistung etc.) schreiben.

**Fail-safe:** Die Sollwerte werden ohne Retain-Flag publiziert – fällt das EMS
aus, hält der Broker keine veralteten Steuerbefehle vor. Zusätzlich hält das
EMS im Loop-Betrieb eine stehende MQTT-Verbindung mit **Last Will**: stirbt der
Prozess (Absturz, Stromausfall, Netzverlust), setzt der Broker selbst
`ems/status = offline`. Empfohlener Watchdog-Flow in Homey: Wenn `ems/status`
auf `offline` wechselt (oder `ems/setpoint/updated` länger als ~35 min kein
Update bekommt), alle Limits auf Hardware-Maximum setzen (Eigenverbrauchs-
Automatik des E3DC).

## Writeback in InfluxDB

Zusätzlich werden geschrieben (Measurements konfigurierbar unter
`influxdb.outputs`):
- `ems_load_forecast` – prognostizierter Hausverbrauch (72 h)
- `ems_control` – Steuerbefehle je Slot (48 h)
- `ems_prediction` – prognostizierte Haus-/Auto-SoCs, Netz, Slot-Kosten (48 h)
- `ems_savings` – Ersparnis-Tracking je abgeschlossenem Slot (s.u.)

## Ersparnis-Tracking

Für jeden abgeschlossenen Slot vergleicht das EMS die **tatsächlichen**
Netzkosten (gemessener Netzbezug/-einspeisung × Preis) mit einer Simulation,
was der E3DC **ohne EMS** im reinen Eigenverbrauchsmodus getan hätte (eigener
hypothetischer Akku-SoC wird fortgeführt, Zustand in `savings_state.json`).
Die kumulierte Differenz erscheint im Dashboard-Titel („Ersparnis gesamt")
und je Slot in `ems_savings` – wird sie dauerhaft negativ, stimmt etwas am
Modell. Benötigt die Signale `pv_generation`, `house_consumption`,
`grid_power` (positiv = Bezug) und `electricity_price`.

## Dashboard

Nach jedem Lauf entsteht `dashboard.html` (Pfad konfigurierbar): heutiger Tag
(Ist-Werte) plus 48-h-Vorhersage mit Leistungen, Steuerbefehlen, SoC-Verläufen
und Preis. Eine Beispielausgabe liegt als `dashboard_beispiel.html` bei.

## Test

```bash
pytest                            # komplette Suite
python -m tests.test_synthetic    # nur der End-to-End-Lauf
```

Ohne InfluxDB/MQTT lauffähig. Abgedeckt:
- `tests/test_synthetic.py` – End-to-End: Prognose, Optimierung (Lösbarkeit +
  Nebenbedingungen), Fallback bei ungültigen Eingaben, Dashboard-Erzeugung.
- `tests/test_optimizer.py` – Randfälle: peak/asap-Strategie, negative Preise,
  Netz-Entlade-Arbitrage, unerreichbarer Auto-Ziel-SoC (Fallback),
  DST-Umstellungstage (92/100 Slots).
- `tests/test_forecast.py` – Rezenz-Gewichtung, Datenlücken, leere Historie.

## Modellannahmen

- PV ist am DC-Bus verfügbar; DC-Laden reduziert die an den Wechselrichter
  geführte PV-Leistung (`pv_to_ac = pv − dc_charge ≥ 0`).
- AC-Laden (Netz) hat einen eigenen, schlechteren Wirkungsgrad als DC-Laden
  aus PV (`ac_charge_efficiency`, Standard = `charge_efficiency`).
- Intraday-Korrektur: Das Ist/Prognose-Verhältnis der letzten Stunden wird
  abklingend (Halbwertszeit `intraday_decay_hours`) auf die Last- und
  PV-Prognose angewandt.
- Optional `feed_in.zero_at_negative_price` (Solarspitzengesetz): Einspeisung
  wird in Negativpreis-Stunden mit 0 ct bewertet.
- Wechselrichter-Durchsatz begrenzt: `pv_to_ac + Entladung + AC-Laden ≤ WR_max`.
- Auto lädt AC-seitig und zählt nicht in den Batterieport-Durchsatz.
- Lade-/Entladewirkungsgrade wirken auf die SoC-Bilanz.
- Liegt kein Fahrzeug-SoC vor, wird das Auto nicht mitoptimiert.
- Ohne Rückkehrzeit-Info wird angenommen, dass das Auto im Horizont angesteckt
  bleibt; der Ziel-SoC wird zu jeder Abfahrtzeit erzwungen.
