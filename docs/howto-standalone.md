# MicroEMS von 0 aufsetzen – nur mit einem E3DC, ohne InfluxDB

Alle Fremdquellen sind kostenlos und ohne API-Key: Wetter von Open-Meteo, Spotpreis von Energy-Charts, PV-Prognose mit pvlib. Gebraucht wird nur der E3DC im LAN.

## 1. Voraussetzungen

Raspberry Pi OS Trixie / Debian 13 (oder jedes Linux mit Python ≥ 3.11):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip coinor-cbc git
```

`coinor-cbc` ist der Rückfall-Solver. Der bevorzugte kommt als Python-Paket (Schritt 2).

## 2. Installieren

```bash
sudo mkdir -p /opt/ems && sudo chown $USER /opt/ems
git clone https://github.com/RobertHerter/MicroEMS.git /opt/ems
cd /opt/ems
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Darin steckt `highspy` – der Solver des Produktivbetriebs: schneller als das
gebündelte CBC und mit festem Seed **deterministisch** (gleicher Input → gleicher
Plan, worauf Plan-Stabilität und Warmstart-Prüfung aufbauen). Fehlt er, fällt der
Optimierer sichtbar auf CBC zurück.

Reproduzierbar statt „neueste passende Version":

```bash
.venv/bin/pip install -r requirements.lock
```

## 3. Konfiguration

Nur diese Abschnitte müssen angefasst werden – alles andere kann auf Standard bleiben.

```yaml
general:
  timezone: "Europe/Berlin"
  latitude: 48.4              # Standort für Wetter und PV-Prognose
  longitude: 11.7
  holiday_subdivision: "BY"   # Bundesland, für die Lastprognose

# --- kein InfluxDB ---
influxdb:
  enabled: false              # No-op; alle Daten liegen lokal in SQLite

# --- E3DC als einzige Hardwarequelle ---
e3dc_rscp:
  enabled: true
  host: "192.168.1.50"        # IP des E3DC im LAN
  username: "portal@example"  # E3DC-Portal-Login
  password: "..."
  key: "..."                  # RSCP-Passwort (am Gerät gesetzt)
  read_live: true             # SoC/PV/Last direkt vom Gerät
  autoread_limits: true       # Kapazität und Leistungsgrenzen selbst auslesen
  history_source: false       # ERST nach dem Backfill auf true (Schritt 4)
  history_db_path: "/opt/ems/e3dc_history.sqlite"
  control_enabled: false      # ERST nach dem Probelauf auf true (Schritt 6)

# --- Preis: Day-Ahead-Spot, kein Key ---
tariff:
  enabled: true
  type: "dynamic"
  markup_percent: 0.0         # Aufschläge des eigenen Tarifs eintragen
  markup_ct_kwh: 0.0
  levies_ct_kwh: 0.0
  vat_percent: 19.0

# --- Wetter: Open-Meteo, kein Key ---
weather:
  enabled: true

# --- PV-Prognose: pvlib statt Solcast ---
solcast:
  enabled: false
pv_model:
  enabled: true
  arrays:
    # string_index (optional): der DC-Strang, der genau dieses Feld misst.
    # Damit wird die Prognosegüte je Feld messbar statt nur für die Summe.
    - {name: "Ost",  kwp: 5.0, tilt: 30, azimuth: 90,  string_index: 0}
    - {name: "West", kwp: 5.0, tilt: 30, azimuth: 270, string_index: 1}
  system_loss: 0.14

# --- kein Smart Home nötig ---
mqtt:
  enabled: false

dashboard:
  enabled: true
  serve: true                 # HTTP-Server im --loop-Betrieb (Standard an)
  host: "0.0.0.0"             # Standard 127.0.0.1 = nur lokal; 0.0.0.0 = im Netz
  port: 8080
  username: "ems"             # Basic Auth; leer = ohne Passwort erreichbar
  password: "bitte-aendern"   # bei host 0.0.0.0 unbedingt setzen
```

`serve` schaltet den HTTP-Server im `--loop`-Betrieb; ohne ihn wird das Dashboard
nur als Datei geschrieben (`output_path`).

Azimut: 0 = Nord, 90 = Ost, 180 = Süd, 270 = West.

Konfiguration prüfen, bevor irgendetwas läuft:

```bash
cd /opt/ems
.venv/bin/python -m ems.main --config config.yaml --check
```

`--check` validiert die Config und rechnet **einen** Solve, ohne zu steuern oder zu schreiben. Erwartete Ausgabe am Ende:

```
OK – Konfiguration gültig und Optimierer lösbar (Trockenlauf).
```

## 4. Historie einmalig holen

Ohne Historie gibt es keine Lastprognose. Die Reihenfolge ist wichtig:

```bash
cd /opt/ems
# Hauslast aus dem E3DC (ein RSCP-Aufruf je 15-min-Fenster, läuft Stunden)
.venv/bin/python rscp_import.py --config config.yaml --days 730

# Temperatur, Einstrahlung und die deutschlandweiten Indizes fürs Preismodell
.venv/bin/python weather_backfill.py --config config.yaml --days 540 --grid

# Spotpreis-Historie
.venv/bin/python energycharts_backfill.py --config config.yaml --days 540
```

Danach in `config.yaml` umstellen:

```yaml
e3dc_rscp:
  history_source: true
```

## 5. Als Dienst einrichten

```bash
sudo tee /etc/systemd/system/ems.service >/dev/null <<'UNIT'
[Unit]
Description=MicroEMS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ems
WorkingDirectory=/opt/ems
ExecStart=/opt/ems/.venv/bin/python -m ems.main --config /opt/ems/config.yaml --loop
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/ems
PrivateTmp=true
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT

sudo useradd -r -s /usr/sbin/nologin ems 2>/dev/null || true
sudo chown -R ems:ems /opt/ems
sudo systemctl daemon-reload
sudo systemctl enable --now ems.service
```

Prüfen:

```bash
systemctl status ems.service
journalctl -u ems.service -f | grep -E "Optimierung|WARNING|ERROR"
```

Erwartete Zeile je Zyklus: `Optimierung: Optimal, erwartete Netto-Kosten -9.89 € (Horizont)`.

Dashboard: `http://<pi>:8080` (Basic Auth mit den Zugangsdaten aus `dashboard`).

## 6. Steuerung scharf schalten

**Erst nachdem ein paar Zyklen sauber gelaufen sind** und der Plan im Dashboard plausibel aussieht:

```yaml
e3dc_rscp:
  control_enabled: true    # greift real in den Speicher ein
  verify_control: true     # gesetzte Limits zurücklesen
```

```bash
sudo systemctl restart ems.service
```

Der Dienst setzt aktive Modi alle 5 s per Watchdog neu (der E3DC fällt sonst nach ~10 s auf auto zurück) und schaltet beim Beenden aktiv auf auto – Fail-safe.

## 7. Wöchentliche Kalibrierung (empfohlen)

```bash
sudo tee /etc/systemd/system/ems-kalibrierung.service >/dev/null <<'UNIT'
[Unit]
Description=MicroEMS Kalibrierung
[Service]
Type=oneshot
User=ems
WorkingDirectory=/opt/ems
ExecStart=/opt/ems/.venv/bin/python -m kalibrierung --config /opt/ems/config.yaml --lookback-days 730 --test-days 365
ExecStart=/opt/ems/.venv/bin/python -m ems.pool_calibration --config /opt/ems/config.yaml --apply
ExecStart=/opt/ems/.venv/bin/python -m ems.battery_calibration --config /opt/ems/config.yaml --apply
ExecStart=/opt/ems/.venv/bin/python -m ems.load_learning --config /opt/ems/config.yaml --apply
ExecStart=/opt/ems/.venv/bin/python -m ems.archive_thinning --config /opt/ems/config.yaml --apply
UNIT

sudo tee /etc/systemd/system/ems-kalibrierung.timer >/dev/null <<'UNIT'
[Unit]
Description=MicroEMS Kalibrierung wöchentlich
[Timer]
OnCalendar=Sun 03:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload && sudo systemctl enable --now ems-kalibrierung.timer
```

Geschriebene Werte landen im Overlay `config_overrides.yaml`, `config.yaml` bleibt unangetastet.
Der letzte Schritt löscht keine Messwerte, sondern dünnt nur das Prognosearchiv
jenseits von `general.forecast_archive_raw_days` (60) aus – ohne ihn wächst es um
rund 14 MB pro Tag.
Die Schritte 2–4 sind nur nötig, wenn eine thermische Last, ein Akku bzw. Geräte
mit `power_topic` konfiguriert sind – ohne passende Daten melden sie das und
schreiben nichts. Was jeder Schritt misst und wann er etwas übernimmt, steht in
[kalibrierung.md](kalibrierung.md).

## Zuerst nachmessen

Der eine Wert, den ich jedem empfehle nach ein bis zwei Wochen zu prüfen:

```bash
.venv/bin/python -m ems.battery_calibration --config config.yaml --days 30
```

Er prüft beide Werte des Akkumodells gegen die Messung:

* **Entladewirkungsgrad** – wie viel SoC eine ans Haus gelieferte kWh kostet. Auf meiner Anlage standen 0,93 in der Config, gemessen waren es 0,786; über eine Nacht sind das zweistellige Prozentpunkte Fehler im geplanten SoC-Verlauf.
* **Nutzbare Kapazität** – aus den Ladephasen. Konfiguriert steht meist der Nennwert des Datenblatts, nicht die gealterte Kapazität. Bei mir 20,6 kWh gegen 18,6 kWh gemessen: der Plan lässt den SoC beim Laden dann zu langsam steigen.

Beide hängen zusammen (im Modell zählt ihr Produkt), deshalb misst der Lauf sie in einem Durchgang und in der richtigen Reihenfolge.

## Was ohne InfluxDB fehlt

Nichts Funktionales. Alle Daten liegen in `e3dc_history.sqlite` (Hauslast, Ist-Werte, Preise, Wetter, Prognose-Archiv, Debug-Schnappschüsse). Der InfluxDB-Pfad bleibt optionaler Fallback je Signal und Ziel für einen Writeback der Zukunftswerte – für den Betrieb ist er nicht nötig.

## Nicht gebraucht, aber mitinstalliert

`requirements.txt` zieht `influxdb`, `influxdb-client` und `paho-mqtt` mit. Für den
reinen E3DC-Betrieb sind die drei überflüssig – sie stören nicht, kosten aber
Plattenplatz. Wer sie weglassen will, installiert von Hand:

```bash
.venv/bin/pip install PyYAML pandas numpy python-dateutil PuLP holidays \
                      plotly scikit-learn pye3dc pvlib highspy
```

## Optional

* **MQTT ans Smart Home:** `mqtt.enabled: true` mit Broker-Daten. Läuft parallel zur RSCP-Steuerung.
* **Docker statt systemd:** `docker compose up -d` mit dem Multi-Arch-Image `ghcr.io/robertherter/microems:3.7.1`; die Kalibrierungs-Timer sind im Container enthalten.
* **Konfiguration im Browser:** `dashboard.controls_enabled: true` schaltet den Editor unter `/config` frei (mit Prüfung, Backup und Neustart).


---

*Verifiziert am 29.07.2026: `requirements.txt` installiert in einem frischen venv
fehlerfrei (Debian 13, Python 3.13), alle im Code benutzten Fremdpakete sind
importierbar, und `--check` läuft mit der Beispielconfig durch. Die systemd-Units
sind aus einem laufenden Setup abgeleitet, aber nicht auf einem frischen System
durchgespielt.*
