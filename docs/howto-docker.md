# MicroEMS im Docker-Betrieb

Alternative zum systemd-Betrieb (siehe [howto-standalone.md](howto-standalone.md)).
Fachlich identisch – dieselbe Konfiguration, dieselben Kalibrierungsjobs, nur in
Containern. Auch hier gilt: nur der E3DC wird gebraucht, keine InfluxDB.

## 1. Voraussetzungen

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER      # danach neu anmelden
```

Der Solver liegt im Image, `coinor-cbc` muss auf dem Host **nicht** installiert
werden.

## 2. Verzeichnisse und Konfiguration

Das Image erwartet zwei Volumes: `config` (Konfiguration) und `data` (SQLite,
Dashboard-HTML, Debug-Schnappschüsse).

```bash
mkdir -p ~/microems/{config,data} && cd ~/microems
curl -fsSL https://raw.githubusercontent.com/RobertHerter/MicroEMS/v3.15.0/config.example.yaml \
     -o config/config.yaml
curl -fsSL https://raw.githubusercontent.com/RobertHerter/MicroEMS/v3.15.0/docker-compose.yml \
     -o docker-compose.yml
```

In `docker-compose.yml` das lokale `build: .` durch das fertige Image ersetzen –
sonst wird ein Checkout gebraucht:

```yaml
services:
  ems:
    image: ghcr.io/robertherter/microems:3.15.0
    # build: .        <- entfernen
  scheduler:
    image: ghcr.io/robertherter/microems:3.15.0
    # build: .        <- entfernen
```

In `config/config.yaml` sind gegenüber dem systemd-Howto **zwei Pfade** anders –
sie müssen auf die Volumes zeigen:

```yaml
e3dc_rscp:
  history_db_path: "/app/data/e3dc_history.sqlite"

dashboard:
  output_path: "/app/data/dashboard.html"

calibration:
  pv_profile: "/app/data/kalibrierung_profil.yaml"
```

Alles Übrige (E3DC-Zugang, `influxdb.enabled: false`, `tariff`, `weather`,
`pv_model`, `dashboard`) ist wortgleich zum systemd-Howto, Schritt 3.

Konfiguration prüfen, bevor etwas dauerhaft läuft:

```bash
docker run --rm -v ./config:/app/config -v ./data:/app/data \
  ghcr.io/robertherter/microems:3.15.0 --config /app/config/config.yaml --check
```

Der `ENTRYPOINT` startet `python -m ems.main`, deshalb werden nur die Argumente
angehängt. Der Prozess läuft dabei nicht als root – siehe
[Ohne root](#ohne-root).

## 3. Historie einmalig holen

Dieselben Skripte wie im systemd-Betrieb, nur im Container. Reihenfolge beachten:

```bash
cd ~/microems
DOCK="docker run --rm -v ./config:/app/config -v ./data:/app/data \
      ghcr.io/robertherter/microems:3.15.0 python"

# Hauslast aus dem E3DC (ein RSCP-Aufruf je 15-min-Fenster, läuft Stunden)
$DOCK rscp_import.py --config /app/config/config.yaml --days 730

# Temperatur, Einstrahlung und die deutschlandweiten Indizes fürs Preismodell
$DOCK weather_backfill.py --config /app/config/config.yaml --days 540 --grid

# Spotpreis-Historie
$DOCK energycharts_backfill.py --config /app/config/config.yaml --days 540
```

Danach in `config/config.yaml` umstellen:

```yaml
e3dc_rscp:
  history_source: true
```

## 4. Starten

```bash
cd ~/microems
docker compose up -d
docker compose ps
docker compose logs -f ems | grep -E "Optimierung|WARNING|ERROR"
```

Erwartete Zeile je Zyklus:
`Optimierung: Optimal, erwartete Netto-Kosten -9.89 € (Horizont)`.

Dashboard: `http://<host>:8080` (Basic Auth aus `dashboard.username` /
`password`).

Der Compose-Stack startet **zwei** Container:

| Container | Aufgabe |
| --- | --- |
| `microems` | der EMS-Dienst (`--loop`), bedient das Dashboard auf 8080; läuft als Benutzer `ems` |
| `microems-scheduler` | `cron -f` für die periodischen Jobs; cron selbst ist root, die Jobs laufen als `ems` |

Der Scheduler ersetzt die systemd-Timer und fährt aus `docker/crontab`:

* täglich 02:45 – Ersparnis gegen die echten E3DC-Zähler validieren
* sonntags 03:00 – Kalibrierung: Verbrauch/PV, Pool-Thermomodell,
  Entladewirkungsgrad und Lastprofile

Geschriebene Werte landen im Overlay `config/config_overrides.yaml`,
`config.yaml` bleibt unangetastet.


> **Wichtig im Container:** `dashboard.host` steht standardmäßig auf
> `127.0.0.1` und ist dann von außerhalb des Containers **nicht** erreichbar –
> der veröffentlichte Port `8080:8080` läuft ins Leere. Im Container gehört
> deshalb in die Config:
>
> ```yaml
> dashboard:
>   host: "0.0.0.0"
>   username: "ems"          # ohne Zugangsdaten ist das Dashboard im Netz offen
>   password: "bitte-aendern"
> ```

## 5. Steuerung scharf schalten

**Erst nachdem ein paar Zyklen sauber gelaufen sind:**

```yaml
e3dc_rscp:
  control_enabled: true    # greift real in den Speicher ein
  verify_control: true
```

```bash
docker compose restart ems
```

Beim Stoppen des Containers schaltet der Dienst den E3DC aktiv auf auto zurück
(Fail-safe). Wichtig dafür: **kein `docker kill`** verwenden – `docker compose
stop` oder `down` senden SIGTERM und lassen dem Dienst Zeit zum Aufräumen.

## 6. Aktualisieren

```bash
cd ~/microems
# Tag in docker-compose.yml auf die neue Version setzen, dann:
docker compose pull
docker compose up -d
```

Ein Rollback ist das Zurücksetzen des Tags plus `up -d`. Die Daten liegen im
Volume und überleben den Wechsel; das Schema wird beim Start automatisch
migriert.

## Ohne root

Der Container startet als root, der EMS-Prozess und die geplanten Jobs laufen
aber als unprivilegierter Benutzer. Beim Start passiert Folgendes
(`docker/entrypoint.sh`, für die Jobs `docker/scheduler-run.sh`):

1. Gehören Dateien in `config/` oder `data/` noch einem anderen Benutzer – bei
   Installationen vor dieser Version typischerweise root –, werden genau diese
   Einträge einmalig übergeben. Stimmt schon alles, wird nichts geschrieben.
   Im Log steht dann einmal `[ems-rechte] Uebergebe /app/data an 10001:10001`.
2. Der Prozess gibt die root-Rechte ab (`setpriv`) und startet erst danach.

**Bestehende Installationen müssen nichts umstellen.** Unter welchem Benutzer
der Dienst läuft:

| Lage | Benutzer im Container |
| --- | --- |
| `config/` gehört root (mit `sudo` oder als root angelegt) | `ems` (10001:10001); `config/` und `data/` gehören danach 10001 |
| `config/` gehört einem normalen Benutzer (z. B. 1000) | genau dieser Benutzer – `config.yaml` bleibt auf dem Host ohne `sudo` bearbeitbar |
| `EMS_UID` / `EMS_GID` gesetzt | die angegebene Kennung |
| `EMS_RUN_AS_ROOT: "1"` | root, wie früher – es wird nichts übergeben |
| `user:` im Compose-File | der dort genannte Benutzer; der Entrypoint ändert dann nichts |

Die Variablen gehören unter `environment:` **beider** Dienste (`ems` und
`scheduler`):

```yaml
    environment:
      TZ: Europe/Berlin
      EMS_RUN_AS_ROOT: "1"     # altes Verhalten
```

Lässt sich der Wechsel nicht durchführen (`cap_drop: ALL`, ein Mount, das kein
`chown` erlaubt), läuft der Dienst mit einer `WARNUNG` im Log als root weiter,
statt am Update zu scheitern.

`docker compose exec` läuft weiterhin als root und würde root-eigene Dateien in
den Mounts hinterlassen. Für Kommandos, die schreiben, deshalb `ems-run`
voranstellen – es wechselt auf denselben Benutzer wie der Dienst:

```bash
docker compose exec ems ems-run python -m ems.battery_calibration \
  --config /app/config/config.yaml --days 30
```

Ist es doch passiert, repariert der nächste Start des Dienstes (und jeder
Scheduler-Job) die Eigentümer wieder.

Das Dashboard lauscht im Container auf 8080 – kein privilegierter Port, er
braucht keine Sonderrechte.

## Betriebsdetails

* **Zeitzone:** über `TZ` im Compose-File (Standard `Europe/Berlin`). Sie muss zu
  `general.timezone` in der Config passen, sonst laufen Slot-Raster und Cron
  auseinander.
* **Healthcheck:** der EMS-Container prüft `GET /version`. Der Scheduler hat ihn
  bewusst abgeschaltet – er bedient keinen HTTP-Port.
* **Backup:** es genügt, `config/` und `data/` zu sichern. Darin steckt alles:
  Konfiguration, Overlay, SQLite-Historie, Prognose-Archiv. Die kleinen
  Dateien sichert der Scheduler wöchentlich selbst – siehe
  [Sicherung](#sicherung).
* **Logs:** `docker compose logs` zeigt die Ausgabe beider Container; der
  Scheduler schreibt seine Jobs nach `/proc/1/fd/1`, erscheint also im
  Container-Log statt in einer Datei.

## Sicherung

Der Scheduler sichert **montags 03:30** die kleinen, nicht wiederherstellbaren
Dateien (Pendant zu `ems-backup.timer` im systemd-Betrieb):

| Datei | Quelle im Container |
| --- | --- |
| `config.yaml` (mit Zugangsdaten), `config_overrides.yaml` | `/app/config` |
| `kalibrierung.yaml`, `kalibrierung_profil.yaml`, `savings_state.json` | `/app/data` |

Ergebnis ist `data/backup/ems-config-<Datum>-<Zeit>.tar.gz`; die letzten 8
bleiben, ältere werden gelöscht. Das Archiv enthält die Zugangsdaten aus
`config.yaml` und ist deshalb nur für den Dienst-Benutzer lesbar (Modus 600) –
auf dem Host also mit `sudo` oder als dieser Benutzer öffnen. Zurückspielen:
Archiv entpacken, die beiden Config-Dateien nach `config/`, den Rest nach
`data/`. Ein Lauf außer der Reihe:

```bash
docker compose exec scheduler /app/docker/scheduler-run.sh backup
```

`savings_state.json` ist nur dabei, wenn `savings.state_path` auf
`/app/data/savings_state.json` zeigt (wie im Kopf von `docker-compose.yml`
empfohlen).

**Die Historie ist nicht enthalten.** `e3dc_history.sqlite` ist rund 2 GB groß
und liegt ohnehin in `data/`. Wer sie mitsichern will, setzt am Dienst
`scheduler` `EMS_BACKUP_DB: "1"`: dann entsteht zusätzlich eine konsistente
Kopie `ems-history-<Datum>-<Zeit>.sqlite` (`VACUUM INTO`, ohne den Dienst
anzuhalten), von der die letzten 2 bleiben – das Ziel braucht dafür etwa 6 GB
frei. Erwartet wird die Historie unter `/app/data/e3dc_history.sqlite`
(Schritt 2).

**Ein Ziel außerhalb des Hosts ist sinnvoll.** `data/backup` liegt auf
derselben Platte wie das Original und schützt nur vor versehentlichem Löschen
oder Verkonfigurieren, nicht vor einem Platten- oder SD-Karten-Ausfall. Für ein
NAS oder einen USB-Stick das Ziel einhängen und benennen:

```yaml
  scheduler:
    environment:
      TZ: Europe/Berlin
      EMS_BACKUP_DIR: /backup
      # EMS_BACKUP_DB: "1"      # zusätzlich die Historie
    volumes:
      - ./config:/app/config
      - ./data:/app/data
      - /mnt/nas/ems-backup:/backup
```

Das Ziel muss für den Dienst-Benutzer schreibbar sein (siehe
[Ohne root](#ohne-root); Vorgabe 10001:10001) – ein eigenes Mount übergibt der
Scheduler nicht von selbst.

## Nachmessen

Der eine Wert, den ich nach ein bis zwei Wochen zu prüfen empfehle:

```bash
docker compose exec ems ems-run python -m ems.battery_calibration \
  --config /app/config/config.yaml --days 30
```

Er vergleicht den konfigurierten Entladewirkungsgrad mit dem gemessenen. Auf
meiner Anlage standen 0,93 in der Config, gemessen waren es 0,786 – über eine
Nacht sind das zweistellige Prozentpunkte Fehler im geplanten SoC-Verlauf.

Ebenso lohnend, sobald zwei abgeschlossene Tage vorliegen:

```bash
docker compose exec ems ems-run python plan_value.py \
  --config /app/config/config.yaml --days 7
```

Zeigt in Euro pro Tag, was die Prognose gegenüber perfekter Voraussicht kostet –
zerlegt in Prognose, Nachplanen und Ausführung.
