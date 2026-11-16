# EOS-Vergleich mit MicroEMS

Dieser Ordner ist die versionierte Quelle des Vergleichsaufbaus. Betrieben wird
er aus einem Klon von [Akkudoktor-EOS/EOS](https://github.com/Akkudoktor-EOS/EOS)
unter `/opt/eos`; `./install.sh` spielt die Dateien dorthin ein (ohne Daten,
ohne Neustart). Die Units aus `systemd/` gehören nach `/etc/systemd/system`.
Alle Pfade (`/opt/ems`, `/opt/eos`) und die Anlagenwerte im Text beziehen sich
auf die Anlage, für die der Vergleich gebaut wurde.

Dieser Stack ist bewusst **read-only gegenüber dem E3DC**. EOS erhält nur
historische/aktuelle Messwerte aus `/opt/ems/e3dc_history.sqlite`; weder der
Exporter noch der Container enthalten RSCP-Zugangsdaten oder Steuerlogik.

Start/Status:

```bash
cd /opt/eos
docker compose -f docker-compose.yaml -f compose.local.yaml up -d
docker compose -f docker-compose.yaml -f compose.local.yaml ps
```

Die EOS-API und das alte EOSdash sind ausschließlich lokal auf homePi unter
`127.0.0.1:8513/8514` erreichbar. Der gemeinsame, read-only Vergleichsleitstand
ist ohne Reverse Proxy unter `http://<homePi-IP>:8530` im lokalen Netz erreichbar.

`scripts/e3dc_to_eos.py` überträgt alle verfügbaren 15-Minuten-Slots aus der
lokalen MicroEMS-Historie nach EOS. Hauslast, PV sowie Netzbezug/-einspeisung
werden dabei in die von EOS erwarteten kumulativen Energie-Zähler (kWh)
integriert; Batterie-Leistung und Batterie-SoC bleiben direkte Messkanäle.
MicroEMS zählt Batterieladen positiv, EOS Entladen positiv, daher wird die
Batterieleistung beim Export negiert.

Zusätzlich wird bei jedem Lauf der jüngste unveränderliche MicroEMS-
Optimierer-Snapshot übernommen: Hauslast, PV, Endkunden-Bezugspreis,
Einspeisevergütung, Außentemperatur und Globalstrahlung. Die Übergabe enthält
echte Zeitstempel und `interval: 15 minutes`. EOS nutzt dafür seine fünf
Import-Provider; Zugangsdaten externer Dienste werden nicht in EOS dupliziert.

`scripts/configure_from_microems.py` übernimmt die nicht geheimen Anlagenwerte
aus `/opt/ems/config.yaml`: Standort, 2 × 8,2 kWp Ost/West bei 20°, Systemverlust,
E3DC-Akku und -Wechselrichter, Prognose-/Optimierungshorizonte sowie Tarif- und
Einspeiseparameter. Für beide PV-Flächen ist in EOS jeweils die gemeinsame
Wechselrichterleistung von 12 kW hinterlegt.

EOS kann diese Messwerte mit `interval=15 minutes` abfragen. Der aktuelle
GENETIC-Optimizer von EOS arbeitet jedoch laut eigener Dokumentation weiterhin
in festen 1-Stunden-Schritten. Deshalb ist der Vergleich derzeit: gleiche
E3DC-Quelle und 15-Minuten-Rohdaten, EOS-Plan stündlich gegenüber MicroEMS-Plan
mit 15 Minuten. EOS-Automatik bleibt deaktiviert, damit keine Steuerbefehle
entstehen.

## Drei Vergleichssysteme

1. MicroEMS: eigene Prognosen und 15-Minuten-MILP.
2. `microems-eos` (lokal Ports 8513/8514): EOS-Optimizer mit exakt importierten
   MicroEMS-Prognosen.
3. `native-eos` (lokal Ports 8523/8524): EOS mit `LoadAkkudoktorAdjusted`,
   `PVForecastForecastSolar`, `ElecPriceEnergyCharts` und `OpenMeteo`.

Die native Instanz rechnet reproduzierbar mit Seed 42 und 1200 GENETIC-
Generationen. Gegenüber dem vorherigen 400-Generationen-Lauf reduziert dies im
ersten Vergleich den 48-Stunden-Netzbezug von 2,148 auf 0,937 kWh.

Die native Instanz bekommt nur dieselben E3DC-Messwerte. Ihr Lastmodell wird
mit dem gemessenen Jahresverbrauch skaliert und anhand der letzten E3DC-Woche
nachgeführt. Beim Strompreis nutzt EOS den konstanten Grundanteil aus Aufschlag,
Abgaben und Standard-Netzentgelt plus MwSt.; die §14a-Zeitfenster kann sein
nativer Provider nicht abbilden. Beide EOS-Instanzen bleiben ohne Steuerwirkung.

## Vergleichsleitstand

`comparison/server.py` normalisiert den jüngsten MicroEMS-Lauf und die beiden
EOS-Optimierungslösungen auf einen 48-Stunden-Vergleich. Gezeigt werden Kosten,
Netzbezug/-einspeisung, PV, Last, Netzfahrplan, SOC, Batterieabsicht und die
Zeitpunkte mit der größten Abweichung. EOS-Stundenwerte werden für die synchrone
Darstellung auf dem 15-Minuten-Raster gehalten und entsprechend gekennzeichnet;
es findet keine künstliche Interpolation statt.

Die Netzkurve zeigt die physikalisch relevante Nettoleistung als Bruttobezug
minus Bruttoeinspeisung. Da das EOS-Modell beide Bruttoflüsse in derselben
Stunde ausgeben kann, unterscheidet das Steuerband zwischen `Netzladen`,
`AC-Laden + Einspeisen` und `AC-Ladebefehl · ohne Bezug`.

Der Dienst `ems-comparison-dashboard.service` schreibt nur lokale EOS-Snapshots
nach `/opt/eos/comparison/snapshots`. Die Browseroberfläche erhält weder
Konfigurationsgeheimnisse noch Schreib- oder Steuerendpunkte.

Der Abschnitt **Mitternachtsfahrplan** bewertet die für den Optimierervergleich
entscheidenden Ausgangsgrößen gegen die E3DC-Istwerte: Brutto-Netzbezug und
Batterie-SOC. Für jeden abgeschlossenen lokalen Tag wird ausschließlich der
erste Fahrplan zwischen 00:00 und 00:30 Uhr verwendet. Alle drei Systeme werden
auf dasselbe Stundenraster gebracht. Angezeigt werden der Bezugs-MAE in kW und
der SOC-MAE in Prozentpunkten.

Für die Rangfolge wird der Bezugs-MAE auf die gemeinsame 12-kW-Leistungsreferenz
normiert und gleichgewichtet mit dem SOC-MAE gemittelt. Ein Sieger wird nur aus
Tagen bestimmt, für die alle drei Mitternachtsfahrpläne und vollständige
Istwerte vorliegen. Damit kann ein System weder durch fehlende Historie noch
durch einen einfacheren Tag einen künstlich besseren Wert erhalten.

MicroEMS besitzt bereits historische Optimierer-Fahrpläne. EOS-Fahrpläne sind
nicht seriös aus den Eingangsprognosen rekonstruierbar. Die beiden Exportdienste
archivieren deshalb künftig nach jedem Lauf Bruttobezug, Einspeisung und SOC-Plan kompakt in
`comparison/forecast_history.sqlite` (120 Tage Aufbewahrung). Für beide EOS-
Instanzen erscheint erst nach dem ersten vollständig abgeschlossenen Tag mit
archiviertem Mitternachtsstand ein belastbarer Vergleich; fehlende Historie
wird nicht als Nullfehler gewertet.

Die beiden 15-Minuten-Exportdienste erzeugen nach dem Datenimport jeweils eine
frische Optimierungslösung. Während des Laufs wird EOS vorübergehend auf
`OPTIMIZATION` mit 60 Sekunden Prüfintervall gesetzt und anschließend auch bei
Fehler oder Timeout wieder auf `DISABLED`/900 Sekunden zurückgestellt. Ein
gemeinsames Lock serialisiert Import-EOS und natives EOS, damit die genetischen
Optimierungen nicht parallel um CPU konkurrieren.

Der zuerst konfigurierte Provider `PVForecastAkkudoktor` wurde am 03.08.2026
nicht eingesetzt, weil dessen öffentlicher Forecast-Endpunkt für die gültige
Ostflächen-Anfrage reproduzierbar HTTP 500 lieferte. Forecast.Solar ist der in
EOS vorhandene schlüssellose Fallback und nutzt dieselben Flächenparameter.
