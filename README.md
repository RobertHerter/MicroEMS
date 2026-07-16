# EMS – Energy Management System

Kostenoptimale Steuerung von Haus-Akku, PV und Fahrzeug. Läuft als
Python-Dienst auf einem Raspberry Pi (Raspberry Pi OS **Trixie** / Debian 13),
berechnet per **MILP** die optimale Steuertabelle für 48 h und gibt die
Steuerbefehle per **MQTT** an Homey aus – und/oder steuert den E3DC **direkt per
RSCP**.

Die Eingangsdaten können aus **InfluxDB** (1.x/2.x) kommen **oder** direkt aus
den Originalquellen (E3DC per RSCP, Open-Meteo, Energy-Charts, Solcast). **Eine
InfluxDB ist damit nicht mehr zwingend erforderlich** – das EMS läuft komplett
standalone (siehe „Standalone ohne InfluxDB"). Wo eine InfluxDB vorhanden ist,
bleibt sie optionaler Fallback je Signal, und alle Zukunftswerte (Steuerbefehle,
prognostizierte SoCs, Zustände) werden auf Wunsch dorthin zurückgeschrieben.

## Architektur

```
Datenquellen  ──►  EMS (Pi, Python)                       ──►  MQTT  ──►  Homey (Sollwerte)
 InfluxDB          1. Verbrauchsprognose (72h, Ähnliche-Tage)
  ODER direkt:     2. Eingangsdaten lesen (Preis/PV/SoC/…)  ──►  E3DC (RSCP-Steuerung, optional)
  E3DC (RSCP)      3. MILP-Optimierung (48h) → Steuertabelle
  Open-Meteo       4. Ausgabe: MQTT an Homey + optional RSCP ──►  InfluxDB (Writeback, optional)
  Energy-Charts    5. optionaler Writeback in InfluxDB
  Solcast          6. Dashboard (HTML)
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
| `ems/forecast.py` | Hausverbrauchs-Prognose: Ähnliche-Tage-Mittelung (Standard) oder Machine Learning (Lag/PV/Temp, via scikit-learn HistGradientBoosting) |
| `ems/optimizer.py` | MILP-Optimierer (PuLP/CBC): Steuertabelle 48 h |
| `ems/homey_mqtt.py` | MQTT: Steuerbefehle an Homey, Status/Last-Will, Alerts, Kommandos |
| `ems/savings.py` | Ersparnis-Tracking: Ist-Kosten vs. simulierte "Ohne-EMS"-Baseline |
| `ems/dashboard.py` | Interaktives HTML-Dashboard (heute + Vorhersage + Steuerbefehle) |
| `ems/rscp.py` | Optionale direkte E3DC-Anbindung (RSCP/pye3dc): Live-Werte, 15-min-Historie, Steuerung (Modi 0–4, Watchdog) |
| `ems/local_history.py` | Lokaler SQLite-Speicher (Hauslast, Ist-Werte, Temperatur, Spotpreis, PV-Prognose) + Quellen-Weichen |
| `ems/weather.py` | Temperatur direkt von Open-Meteo (kein Key) |
| `ems/energycharts.py` + `ems/tariff.py` | Spotpreis von Energy-Charts + Tarifmodell → Endkunden-Bezugspreis (§14a EnWG) |
| `ems/solcast.py` | PV-Vorhersage von Solcast (mehrere Keys/Resourcen, Abruf-Budget/-Verteilung) |
| `ems/loads.py` | Steuerbare/verschiebbare Lasten im MILP (deferrable + thermischer Speicher, z.B. Pool) |
| `ems/ingest.py` | Externe Einspeisung (REST-Ingest) von Live-/Historienwerten -> Betrieb ohne RSCP/InfluxDB |
| `ems/main.py` | Orchestrierung + CLI (`--loop` für Dauerbetrieb), systemd-Watchdog |
| `tests/` | pytest-Suite (E2E synthetisch, Optimierer-Randfälle, Prognose, Ersparnis) |

## Eingangssignale

Hausverbrauch, Strompreis, Haus-Akku-SoC, PV-Erzeugung, PV-Vorhersage,
optional Fahrzeug-SoC, optional Einspeisevergütung. Strompreis und PV-Vorhersage
werden auch für die Zukunft gelesen. Alle Leistungen in **W**, Preise in
**ct/kWh**, Energien in **Wh**. Berechnung auf **15-min-Slots**.

Jedes Signal kommt entweder **aus der InfluxDB** (Zuordnung Signal →
Measurement/Field in `config.yaml` unter `influxdb.signals`) **oder direkt aus
der Originalquelle** – gesteuert über die jeweiligen Flags (`e3dc_rscp`,
`weather`, `tariff`, `solcast`). Ist eine Direktquelle aktiv, wird das
entsprechende InfluxDB-Signal nicht mehr benötigt; siehe „Standalone ohne
InfluxDB".

## Direkte Quellen / Standalone ohne InfluxDB

Statt (oder zusätzlich zu) InfluxDB/MQTT kann das EMS den E3DC direkt per RSCP
ansprechen (Bibliothek `pye3dc`, `pip install pye3dc`) und alle übrigen
Eingangsdaten direkt aus den Originalquellen ziehen. Sind alle Direktquellen
aktiv, läuft das EMS **komplett ohne InfluxDB**. Aktivierung unter `config.yaml`
→ `e3dc_rscp` / `weather` / `tariff` / `solcast` (Default aus, ändert sonst
nichts):

- **`read_live`**: aktueller SoC/PV/Last direkt vom Gerät (frischer als der
  DB-Umweg) für Anfangs-SoC und Slot-0-Anker – mit Fallback auf InfluxDB.
- **`control_enabled`**: steuert den Speicher direkt per RSCP (zusätzlich zur
  MQTT-Ausgabe an Homey, die parallel **immer** weiterläuft). **Greift real ein.**
  Die `EMS_REQ_SET_POWER`-Modi sind **an echter Hardware verifiziert** (pye3dc
  0.10): **0**=auto, **1**=idle, **2**=Entladen, **3**=Laden (aus PV),
  **4**=Netzladen. Der Wert ist die Gesamtleistung (PV zuerst, Netz für den Rest).
  `apply_control` setzt daraus je Slot:
  - *Netzladen* (`batt_grid_charge_w>0`) → **Mode 4**, Wert = geplante
    Gesamt-Ladeleistung (DC+Netz).
  - *Netz-Entladen* (`batt_grid_discharge_w>0`, nur bei `allow_grid_discharge`)
    → **Mode 2**, Wert = geplante Entladeleistung. (Mode 2 = Entladen live
    verifiziert, −3042 W; Mode 4 = Netzladen mit 8 kW verifiziert.)
  - *reine Lade-/Entlade-Begrenzung* (Peak-Shaving, Sperren) → persistente
    Limits (`set_power_limits`), **kein** Mode-Eingriff/Watchdog.
  - Für aktive Modi (2/3/4) sendet ein Watchdog-Thread den Befehl alle **5 s**
    neu, da der E3DC sonst nach ~10 s selbst auf auto zurückfällt.
  - Beim Beenden schaltet der Dienst aktiv auf auto (Mode 0) zurück; stirbt der
    Prozess, tut es der 10-s-Watchdog des E3DC selbst (Fail-safe).
- **`history_source`**: die Verbrauchsprognose liest die 15-min-Hauslast aus
  einer lokalen SQLite (`ems/local_history.py`) statt aus der InfluxDB. Die
  Hauslast je Fenster wird aus der E3DC-Energiebilanz berechnet
  (`PV + Akku-Entladung + Netzbezug − Akku-Ladung − Einspeisung`; gegen
  InfluxDB verifiziert: Bias ~−44 W, Fenster-Rauschen mittelt sich in der
  Ähnliche-Tage-Prognose heraus). Ablauf:
  1. **Einmaliger Backfill** (Hintergrund): `python rscp_import.py --config
     config.yaml --days 730` – 1 RSCP-Aufruf je 15-min-Fenster (2 Jahre
     ≈ 70 000, mehrere Stunden). Danach `history_source: true` setzen.
  2. **Zyklisch**: der Dienst führt vor jeder Prognose (alle 15 min) die neuen
     Fenster nach – mit Reifeverzug (`history_settle_minutes`), weil die
     jüngsten E3DC-Aggregate noch unvollständig sein können, und überlappend
     (`history_overlap_hours`), damit Zwischenstände automatisch ersetzt werden.
     Nichtpositive Zwischenbilanzen werden verworfen. Der Abruf ist idempotent
     und auf 3 Tage gekappt, sodass ein Lauf nie den ganzen Backfill zieht. Nach einem
     längeren Ausfall (> 3 Tage) den Backfill einmal manuell erneut laufen
     lassen.

  Damit kann die Verbrauchs-Historie ohne InfluxDB/openHAB laufen.
- **Ist-Werte lokal** (`ems/local_history.py`, Tabelle `actuals`): bei aktivem
  `history_source` protokolliert der Dienst jeden Zyklus den E3DC-Live-Snapshot
  (SoC/PV/Last/Netz/Akku). Alle Funktionen, die bisher die jüngsten Ist-Werte
  aus der InfluxDB lasen – Intraday-Korrektur, Ersparnis-Tracking, Drift-Monitor
  und die Ist-Kurven im Dashboard – lesen dann aus dieser lokalen Tabelle
  (zentrale Weiche `read_actual_signal`).
- **Temperatur direkt von Open-Meteo** (`ems/weather.py`, `weather.enabled`,
  kostenlos, kein API-Key): je Zyklus werden die letzten `past_days` (max 92) +
  `forecast_days` stündlich abgerufen und in die lokale SQLite (Tabelle
  `temperature`) gecacht; `_read_temp` liefert daraus (auf Slot-Raster
  interpoliert) statt aus InfluxDB. Tiefe Historie einmalig via
  `weather_backfill.py` (ERA5-Archiv, ein Call/Jahr). Fällt der Abruf aus, wird
  der Cache genutzt. Jeder erfolgreiche Abruf archiviert zusätzlich nur seine
  Zukunftswerte unveränderlich mit Erstellungs- und Zielzeitpunkt
  (`weather_forecast_archive`). Damit kann die Rolling-Origin-Validierung ab
  diesem Zeitpunkt exakt den damals bekannten Wetterstand verwenden.
- **Bezugspreis direkt von Energy-Charts + Tarifmodell** (`ems/energycharts.py`,
  `ems/tariff.py`, `tariff.enabled`, kostenlos, kein API-Key): je Zyklus wird der
  Day-Ahead-Spot der Gebotszone (EUR/MWh → ct/kWh) geholt und in die lokale SQLite
  (Tabelle `spot_price`) gecacht. Beim Auslesen rechnet das Tarifmodell daraus den
  **Endkunden-Bezugspreis** (ct/kWh brutto):

  ```
  netto  = spot·(1+markup_percent/100) + markup_ct_kwh + levies_ct_kwh + netzentgelt
  brutto = netto·(1+vat_percent/100)          # MwSt auf alles
  ```

  Tarifart `dynamic`/`fixed`; Netzentgelt `static` (konstant), `included` (=0) oder
  `14a` (§14a EnWG zeitvariabel: Fensterliste nach Uhrzeit/Monat/Datum/Wochentag,
  erstes passendes Fenster gewinnt, sonst Default). Zentrale Weiche
  `tariff.read_price_signal` (nutzen `_price_series` und das Ersparnis-Tracking).
  Tiefe Preishistorie einmalig via `energycharts_backfill.py` (90-Tage-Blöcke).
  Fällt der Abruf aus, wird der Cache genutzt; fehlende Folgetag-Preise ergänzt
  weiterhin die Ähnliche-Tage-Schätzung.
- **PV-Vorhersage direkt von Solcast** (`ems/solcast.py`, `solcast.enabled`):
  rooftop-site-Forecast (inkl. P10/P90) je Zyklus geholt und in die lokale SQLite
  (Tabelle `pv_forecast`, je Quelle) gecacht; `read_pv_signal`/`available` ersetzen
  die `pv_forecast`-Reads in `_pv_series`, Intraday-PV-Korrektur und Dashboard.
  **Mehrere Keys und Resourcen**: `combine: "sum"` addiert verschiedene Arrays
  (Ost/West), `"mean"` mittelt redundante Quellen (dieselbe Anlage über mehrere
  Keys). **Abruf-Budget** `calls_per_key_per_day` (Free-Tier 10/Key) wird je Quelle
  gleichmäßig verteilt (key_budget / Quellen-je-Key Abrufe/Tag). `distribution`
  steuert wie: `"daytime"` konzentriert aufs lokale Fenster `[window_start_hour,
  window_end_hour)` (PV-Nowcasting im Fokus), `"24h"` verteilt rund um die Uhr
  (hält auch den Folgetag-Forecast frisch); zwischen Abrufen wird der letzte
  Forecast gehalten. Fehler (z.B. 429) → Cooldown + Cache. Solcast 30-min-Perioden werden
  beim Auslesen aufs Slot-Raster gehalten. Tiefe PV-Historie einmalig via
  `solcast_import.py` (aus der InfluxDB, Quelle `influx_hist`, nur für Zeitstempel
  VOR dem Live-Beginn → keine Überlappung mit Ost/West beim `sum`). Erfolgreiche
  Live-Abrufe werden außerdem als unveränderliche Snapshots mit `issue_time` und
  `target_time` archiviert (`pv_forecast_archive`). Alte Snapshots werden nicht
  überschrieben; rückwirkend lassen sie sich nicht aus dem aktuellen Cache
  rekonstruieren.
- **Standalone erreicht:** Verbrauch, Temperatur, Bezugspreis und PV-Vorhersage
  laufen ohne InfluxDB. Auch die Kalibrierung (`kalibrierung.py`) folgt derselben
  Weiche und rechnet dann gegen die lokalen Daten. Die InfluxDB bleibt optional
  (Fallback je Signal + Writeback).

Hinweis: Live-Werte, 15-min-Historie und die Steuerung (Modi 2/3/4) sind gegen
echte Hardware (pye3dc 0.10) verifiziert. Feldnamen-Mapping (`_map_live`) und
Vorzeichen (`grid_sign`/`batt_sign`) sind gerätespezifisch – bei abweichender
Hardware prüfen; die Logik ist zusätzlich mit gemocktem Client getestet
(`tests/test_rscp.py`).

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

## Steuerbare / verschiebbare Lasten (`controllable_loads`)

Optionale Liste zusätzlicher Lasten, die der Optimierer mitplant und in die
günstigsten/PV-reichsten Slots legt (Sollwert on/off je Slot optional per MQTT).
Zwei Typen (`ems/loads.py`):
- **`deferrable`** – muss `runtime_minutes` im Zeitfenster laufen; Leistung
  konstant (`power_w`) oder als 15-min-Kurve (`power_profile_w`, Startzyklus).
- **`thermal`** – thermischer Speicher (z.B. Pool): die Temperatur ist ein
  **MILP-Zustand**, gehalten im Band `[min_c, max_c]`, geheizt über `stages`
  (on/off-Wärmepumpen, per `requires` koppelbar – z.B. „große WP nur mit
  kleiner"); Wärmeverlust `~ loss_w_per_k·(T−T_außen)` (Außentemp aus dem
  Wetter-Feed), Ist-Temperatur aus `temp_signal`. So wird vorausschauend in
  PV-Überschuss/günstige Slots vorgeheizt statt grob „PV>X → an".

Leere Liste (Default) = keine zusätzlichen Variablen, Optimierer unverändert.

## Betrieb ohne InfluxDB / externe Einspeisung (Ingest-API)

Das EMS läuft auch **ganz ohne InfluxDB und ohne RSCP** — die sonst von dort
kommenden Werte werden per REST eingespielt (`ems/ingest.py`):
- **`influxdb.enabled: false`** → No-op-Repository (kein Lesen/Writeback aus/in
  InfluxDB); alle Eingangsdaten kommen lokal/extern.
- **`dashboard.ingest_enabled: true`** → POST-Endpunkte am Dashboard-HTTP-Server
  (Basic-Auth wie das Dashboard, `dashboard.username`/`password`):
  - `POST /api/ingest/live` – aktueller Snapshot (SoC/PV/Last/Netz/Akku), im
    Speicher gecacht; `run_once` nutzt ihn als Ersatz für `e3dc.read_live`.
  - `POST /api/ingest/house_load` – 15-min-Hauslast-Historie (für die Prognose).
  - `POST /api/ingest/actuals` – Ist-Werte (Dashboard/Ersparnis/Drift).
  - `POST /api/ingest/temperature` / `/spot` / `/pv_forecast` – für den voll
    externen Betrieb auch Temperatur, Spotpreis und PV-Prognose. Bei
    `/pv_forecast` kann `issue_time` mitgegeben werden (sonst Eingangszeit);
    Zukunftswerte werden zugleich im Prognosearchiv abgelegt.
  Zeitstempel werden auf UTC-ISO normalisiert; Historien landen direkt in der
  lokalen SQLite, Live-Werte im Cache. Payload-Formate: siehe `config.example.yaml`.

So kann ein beliebiges Fremdsystem (Homey, node-red, eigenes Skript) die Daten
liefern; RSCP/InfluxDB sind dann nur noch optionale Quellen.

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
ems/cmd/recalc          sofortige Neuberechnung anstoßen (Payload egal)
ems/cmd/car_boost       "1"/"0": Auto sofort mit Max-Leistung laden, bis der
                        Ziel-SoC erreicht ist (überschreibt car_charge_w)
ems/cmd/car_departure_time  "HH:MM": Abfahrtzeit setzen; ""/"default" =
                        Konfigwert; "off"/"urlaub" = Urlaubsmodus: keine
                        Abfahrten, der Ziel-SoC wird nicht mehr erzwungen
ems/cmd/car_target_soc  Ziel-SoC in % (1..100); ""/"default" = Konfigwert
ems/cmd/min_soc         Haus-Akku Minimum-SoC in % (z.B. Reserve vor Sturm/
                        Stromausfall hochsetzen); ""/"default" = Konfigwert
ems/cmd/max_soc         Haus-Akku Maximum-SoC in % (Akku schonen);
                        ""/"default" = Konfigwert
```

Die Parameter-Kommandos in Homey **mit Retain** publizieren, dann überstehen
sie einen EMS-Neustart (der Broker liefert sie beim Reconnect erneut aus).
Inkonsistente Grenzen (min ≥ max) werden verworfen. Die aktuell wirksamen
Werte meldet das EMS unter `ems/vehicle/departure_time`,
`ems/vehicle/target_soc_percent`, `ems/battery/min_soc_percent` und
`ems/battery/max_soc_percent` zurück.

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

![EMS Dashboard (Beispielausgabe mit synthetischen Daten)](dashboard_beispiel.png)

Nach jedem Lauf entsteht `dashboard.html` (Pfad konfigurierbar, im Loop-Betrieb
per HTTP auf Port 80 erreichbar, Auto-Reload nach jeder Neuberechnung):

- **KPI-Kacheln**: Netto-Kosten Horizont, Ersparnis gesamt, Akku-SoC,
  Modus jetzt (mit Limits), Eingriffe im Plan
- **Leistung** (PV mit Solcast-p10–p90-Band, Verbrauch, Netz, Einspeise-Linie;
  Ist durchgezogen, Prognose gestrichelt), **Ladezustand**, **Strompreis** +
  Einspeisevergütung, **Steuerung** (Ladebefehle, Abregelung, Ist-Akkuleistung)
- **Modus-Zeitleiste**: Eingriffe als Farbstreifen (auto/Peak-Laden/gedrosselt/
  gesperrt/Netzladen/Netz-Entladen) mit Legende und Hover-Klartext
- **Steuerbare Lasten** (nur bei konfigurierten `controllable_loads`): An/Aus je
  Slot als Farbstreifen, deaktivierte Lasten grau
- **Temperaturen** (nur bei thermischen Lasten): erwartete vs. echte
  Pooltemperatur je Last (Komfortband hinterlegt) + Außentemperatur
- Vergangenheit grau hinterlegt, Tagesgrenzen mit Wochentag, Jetzt-Linie
- **Interaktives Steuerpanel** (`dashboard.controls_enabled: true`): Lasten
  an/aus + Kernparameter, Optimierungsmodus (auto/asap/peak), manuelles
  Akku-Laden/-Entladen – direkt aus dem Dashboard, ohne MQTT

Die interaktive Beispielausgabe (Bild oben, **synthetische Daten**) liegt als
[dashboard_beispiel.html](dashboard_beispiel.html) bei – regenerierbar mit
`python beispiel_dashboard.py` (nutzt Plotly vom CDN, das produktive Dashboard
läuft dagegen offline mit lokalem `plotly.min.js`). Screenshot erneuern:

```bash
chromium --headless --no-sandbox --hide-scrollbars --window-size=1500,1110 \
  --screenshot=dashboard_beispiel.png dashboard_beispiel.html
```

### Webserver & API (Basic Auth)

Der eingebaute HTTP-Server liefert das Dashboard aus und bietet zusätzlich einen
**JSON-API-Endpunkt**. Unter `http://<host>:<port>/api/data.json` kann der
vollständige Optimierungs-Zustand (Ist-Werte und Zukunftspläne für alle Slots) als
maschinenlesbares JSON abgerufen werden – ideal zur Anbindung an Drittsysteme
(Grafana, Node-RED, etc.).

**Sicherheit:** Die gesamte Weboberfläche (Dashboard, Report-Download und API) kann 
mit Basic Authentication abgesichert werden. Dazu in der `config.yaml` unter `dashboard`
die Felder `username` und `password` setzen. (Bleiben sie leer, ist der Zugriff ungeschützt).

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
- `tests/test_validate.py` – Invarianten-Validator (`ems/validate.py`).
- `tests/test_fuzz.py` – Fuzz (zufällige Szenarien × Invarianten) + metamorphe
  Relationen (Preis-Offset, höhere Vergütung, mehr PV).

## Modell-Prüfung: Invarianten & Backtest

Modellfehler zeigen sich oft nur als „das sieht komisch aus" im Dashboard.
Zwei Werkzeuge machen die Suche systematisch:

- **`ems/validate.py`** – prüft einen fertigen Plan gegen Invarianten, die
  immer gelten müssen (SoC-/Leistungsgrenzen, Energiebilanz, kein
  gleichzeitiges Laden/Entladen, DC-Laden nur aus PV-Überschuss, kein Entladen
  bei PV-Überschuss, Einspeisebegrenzung, **Ausführbarkeit**: die an Homey
  gesendeten Befehle passen zu den geplanten Flüssen) plus ökonomische
  Plausibilität (Plan nie teurer als die Ohne-EMS-Baseline). Läuft in Tests,
  im Backtest UND live: nach jeder Optimierung, mit Anzeige im Dashboard
  (Banner + Kachel) und Alarm auf `ems/alert`.
- **`ems/drift.py`** – Predicted-vs-Actual: vergleicht je Lauf den
  prognostizierten mit dem gemessenen Haus-SoC (MAE in Prozentpunkten ->
  Measurement `ems_drift`), Warnung über der Schwelle. Deckt Modellfehler auf,
  die kein einzelner Plan zeigt (Wirkungsgrade, Standby, Alterung).
- **Debug-Report-Button** (Dashboard, nur bei `report.enabled: true`): lädt bei
  einer Implausibilität den Schnappschuss des letzten Laufs (Eingaben + Plan,
  ohne Zugangsdaten) herunter und öffnet das Mailprogramm vorausgefüllt an
  `report.mail_to` – die JSON hängt man manuell an. Damit lässt sich der Fehler
  offline im Backtest/Optimizer exakt nachstellen. Kein SMTP-Server nötig.
- **`backtest.py`** – spielt vergangene Tage aus der InfluxDB durch den
  Optimierer (perfekte Voraussicht) und prüft jeden Plan. Findet Modellfehler
  über Monate echter Daten in Minuten, statt monatelang zuzuschauen:

  ```bash
  python backtest.py --config config.yaml --days 120
  python backtest.py --config config.yaml --start 2026-01-01 --end 2026-03-01
  ```

  Schreibt nichts in die DB. Nach jeder Modelländerung als Regressions-Sweep
  laufen lassen: erwartet werden 0 Fehler und 0 (terminalwert-bereinigt)
  negative Ersparnis-Tage.

## Modellannahmen

- PV ist am DC-Bus verfügbar; DC-Laden reduziert die an den Wechselrichter
  geführte PV-Leistung (`pv_to_ac = pv − dc_charge ≥ 0`).
- AC-Laden (Netz) hat einen eigenen, schlechteren Wirkungsgrad als DC-Laden
  aus PV (`ac_charge_efficiency`, Standard = `charge_efficiency`).
- Intraday-Korrektur: Last und PV besitzen getrennte Fenster, Faktorgrenzen und
  Abklingzeiten. Die Last nutzt den Median der Slot-Verhältnisse gegen einzelne
  Verbrauchsspitzen; PV berücksichtigt nur Slots oberhalb
  `intraday_pv_min_power_w`. Totzone und maximale Änderung je Lauf verhindern
  hektische Sprünge. Ist-/Basisprognose-Slots sowie Roh- und angewandte Faktoren
  werden lokal in `intraday_window`/`intraday_correction` archiviert.
- Optional `feed_in.zero_at_negative_price` (Solarspitzengesetz): Einspeisung
  wird in Negativpreis-Stunden mit 0 ct bewertet.
- Geschätzte Folgetag-Preise werden zur Mitte gestaucht
  (`forecast.price_damping`) – keine Spekulation auf Phantom-Preistäler.
- Terminalwert (`"auto"`): fallende Grenzwert-Kurve in 3 Segmenten (oberes
  Preisquartil / Mittel / max(unteres Quartil, Einspeisung)) statt
  Einheitspreis – die letzte gespeicherte kWh ist weniger wert als die erste.
- Slot 0 wird mit Live-Messwerten (Last, PV der letzten Slot-Länge) verankert.
- Wallbox: Schalt-Malus je Einschaltvorgang (`car_switch_penalty_ct`) und
  optionale Ladekurve (`vehicle.taper_start_soc_percent`: Leistung sinkt
  linear bis `min_charge_w` bei 100 %).
- Der Auto-Ziel-SoC ist eine **weiche** Nebenbedingung
  (`car_target_penalty_ct_kwh`): Ist er nicht erreichbar, lädt der Plan so
  viel wie möglich und meldet die Fehlmenge per `ems/alert`, statt komplett
  auf "auto" zurückzufallen.
- Optional `inverter.max_export_w`: Einspeisebegrenzung am Netzanschluss –
  der Plan rechnet nicht mit Erlösen, die real abgeregelt würden (gilt auch
  für die Ohne-EMS-Baseline des Ersparnis-Trackings).
- Wechselrichter-Durchsatz begrenzt: `pv_to_ac + Entladung + AC-Laden ≤ WR_max`.
- Auto lädt AC-seitig und zählt nicht in den Batterieport-Durchsatz.
- Lade-/Entladewirkungsgrade wirken auf die SoC-Bilanz.
- Liegt kein Fahrzeug-SoC vor, wird das Auto nicht mitoptimiert.
- Ohne Rückkehrzeit-Info wird angenommen, dass das Auto im Horizont angesteckt
  bleibt; der Ziel-SoC wird zu jeder Abfahrtzeit angestrebt (weich, s.o.).
- Abfahrtzeiten je Wochentag über `vehicle.departure_times` (mo..so, `null` =
  keine Abfahrt, z.B. am Wochenende). Liegt keine Abfahrt im Horizont, wird
  der Ziel-SoC zum Horizontende angestrebt – außer es gibt gar keine
  Abfahrtstage. Ein MQTT-Override (`ems/cmd/car_departure_time`) gilt für alle
  Wochentage.
