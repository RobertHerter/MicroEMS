# Der wöchentliche Kalibrierungslauf

Jeden Sonntag um 03:00 misst sich das EMS selbst nach: Wie gut war die Prognose,
wie verhält sich der Pool wirklich, was kostet eine kWh aus dem Akku, wie lange
läuft die Spülmaschine. Die Ergebnisse fließen teils automatisch zurück in die
Planung, teils als Empfehlung an den Betreiber.

Ausgelöst wird das von `ems-kalibrierung.timer` (`OnCalendar=Sun 03:00`,
`Persistent=true` – ein verpasster Termin wird beim nächsten Boot nachgeholt).
Der Lauf dauert auf dem Pi rund 4 Minuten CPU-Zeit und besteht aus vier
`ExecStart`-Zeilen, die nacheinander laufen:

| # | Modul | Misst | Übernimmt |
|---|-------|-------|-----------|
| 1 | `kalibrierung` | PV- und Lastprognose | teils automatisch, teils Empfehlung |
| 2 | `ems.pool_calibration --apply` | Pool-Thermomodell | automatisch (gedämpft) |
| 3 | `ems.battery_calibration --apply` | Kapazität und Entladewirkungsgrad | automatisch (gedämpft) |
| 4 | `ems.load_learning --apply` | Lastprofile der Geräte | automatisch |

Alles, was automatisch übernommen wird, landet im Overlay
`config_overrides.yaml`. Die kommentierte `config.yaml` bleibt unangetastet –
und der laufende Dienst liest das Overlay bei jedem Zyklus neu ein
(`_reload_calibration_overrides`), ein Neustart ist dafür nicht nötig.

---

## Schritt 1 – Prognosegüte

```
python -m kalibrierung --config config.yaml --lookback-days 730 --test-days 365
```

Der umfangreichste Schritt. Er vergleicht über zwei Jahre Historie:

**PV-Vorhersage gegen Ist-Erzeugung.** Ergebnis sind nMAE, Bias, RMSE,
Korrelation und ein Skalierungsfaktor Ist/Vorhersage – global, je Stunde, je
Monat und als Monat×Stunde-Matrix. Beispiel aus dem Lauf vom 02.08.:
nMAE 23,6 %, Bias +605 W, Global-Faktor 0,897.

**Hausverbrauchs-Modell gegen Ist-Verbrauch**, ausdrücklich *out-of-sample*:
Das Modell wird auf dem Trainingszeitraum gebaut und auf dem Testzeitraum
geprüft. Zusätzlich zu Stunden- und Werktag/Wochenende-Faktoren fällt eine
Aufschlüsselung nach Temperaturbereich an.

Wichtig beim Lesen: Gemessen wird hier bewusst das **nackte Grundmodell** –
`correction_factor` auf 1,0, Stundenprofil abgeschaltet, ohne Ensemble und ohne
Temperatur-Residual, in einem einzigen Durchgang über den ganzen Testzeitraum.
Sonst flösse ein früherer Kalibrierlauf in die eigene Messung ein
(Zirkelschluss). Die Zahlen sind damit eine Grundlinie, **nicht** der Fehler des
Live-Systems: der Faktor 1,298 über 25 °C (Klimaanlage) beschreibt, was das
Grundmodell allein nicht sieht – im Betrieb fängt das Temperatur-Residual den
Großteil davon ab (siehe unten).

**Rolling-Origin-Validierung**, 12 Folds à 48 h, roh ohne Korrekturen, `ml`
gegen `similar_days`. Aufgeschlüsselt nach Tageszeit, Wochentag und Jahreszeit,
mit einer Empfehlung für `forecast.method`.

**Champion-/Challenger-Kalibrierung.** Der eigentliche Automatismus: die neu
gerechnete Korrektur (Challenger) tritt gegen die produktiv laufende (Champion)
an. Übernommen wird nur, wenn der WAPE um mindestens
`min_improvement_percent` (1,0 %) besser wird, mindestens `min_samples` (96)
Datenpunkte vorliegen – und ein **Segment-Guard** greift: verschlechtert sich
eines der Segmente Nacht/Morgen/Mittag/Abend zu stark, wird trotz besserer
Gesamtzahl nicht befördert. Der Zustand je Signal steht in
`kalibrierung_profil.yaml` unter `calibration_competition`.

Der Lauf vom 02.08. zeigt beide Ausgänge nebeneinander:

```
PV-Korrektur     CHAMPION HALTEN      n= 841 · Verbesserung -11.1 % unter Mindestvorsprung
Lastkorrektur    ÜBERNEHMEN           n=1336 · WAPE verbessert sich um 2.0 %
PV-Band          NOCH NICHT BEWERTBAR n= 175 · zu wenig getrennte Residuen
```

**Ein Profil je PV-Quelle.** Solcast und das freie pvlib-Modell bekommen
getrennte Korrekturfaktoren, jedes mit eigener Champion-/Challenger-Prüfung:

```
PV-Korrektur je Quelle (Grundlage des Quellenvergleichs):
  solcast (produktiv) CHAMPION HALTEN      n= 813 · global=0.9051
  pvlib               ÜBERNEHMEN           n= 210 · global=0.9864
```

Sie stehen im Profil unter `pv_sources:`; die flachen Schlüssel `pv_global`,
`pv_hour`, `pv_month`, `pv_month_hour` bleiben als Profil der produktiven
Quelle erhalten, damit ältere Installationen unverändert weiterlaufen.

Das ist die Voraussetzung für die automatische Quellenumschaltung. Vorher wurde
nur die produktive Quelle kalibriert – im Vergleich trat also eine korrigierte
gegen eine rohe Prognose an, und das Ergebnis maß, wer kalibriert ist, nicht wer
besser prognostiziert. Das fremde Profil einfach mitzubenutzen ist keine
Lösung: über 14 Tage kam pvlib mit Solcasts Faktoren auf **WAPE 22,2 %** statt
16,0 % roh. Jede Quelle wird deshalb nur mit *ihrem eigenen* Profil korrigiert –
oder gar nicht.

Zwei Dateien fallen dabei an, und die Unterscheidung ist wichtig:

* **`kalibrierung_profil.yaml`** – das *produktive* Korrekturprofil. Wird live
  von `ems/forecast.py` und `ems/pv_eval.py` gelesen.
* **`kalibrierung.yaml`** – der *Bericht* mit allen Metriken. Wird nur vom
  Dashboard gelesen, greift nicht in die Planung ein.

Nicht automatisch übernommen werden `pv_forecast.scale` und
`forecast.correction_factor` – die stehen als Empfehlung im Log und müssen von
Hand in die `config.yaml`, mit anschließendem Neustart. Grund: Sie wirken
multiplikativ auf dasselbe Signal wie das Korrekturprofil; automatisch gesetzt
gäbe es einen Doppel-Faktor.

## Schritt 2 – Pool-Thermomodell

```
python -m ems.pool_calibration --config config.yaml --apply
```

Fittet per linearer Regression aus der lokalen Historie, was das MILP-Modell in
`ems/loads._add_thermal` als gegeben annimmt:

```
dT/dt = (surface_m2 · solar_absorption · G  −  loss_w_per_k · (T_pool − T_außen)) / C
```

Gerechnet wird über überlappende 2-h-Fenster (8 Slots) – lang genug, dass die
Temperaturänderung über der Sensorauflösung liegt. Nebenbei fallen die *realen*
Heizleistungen der WP-Stufen ab, die erfahrungsgemäß deutlich von den
Datenblattwerten in der `config.yaml` abweichen.

Zuletzt: 1104 Fenster, R² = 0,78, `loss_w_per_k` 175 gemessen gegen 164 im
Modell, Solar-Koeffizient 3,57 gegen 3,64.

## Schritt 3 – Akkumodell: Kapazität und Entladewirkungsgrad

```
python -m ems.battery_calibration --config config.yaml --apply
```

Misst über 30 Tage beide Werte des Akkumodells. Sie hängen zusammen – im
Optimierer zählt ihr Produkt –, deshalb laufen sie in einem Durchgang: erst die
Kapazität, dann der Entladewirkungsgrad mit dem neuen Wert.

**Nutzbare Kapazität** aus den Ladephasen. Konfiguriert steht meist der Nennwert
des Datenblatts; die gealterte Kapazität liegt darunter, und `capacity_wh` war
bis dahin der einzige Modellwert ohne Kalibrierpfad. Kapazität und Wirkungsgrad
sind aus SoC-Daten nicht trennbar – gemessen wird nur ihr Produkt. Der Anker ist
deshalb `charge_efficiency`: ein DC-Pfad ohne Wechselrichter, der kaum altert.
Die gemessene Kapazität skaliert exakt proportional mit ihm.

Gewertet wird nur der Teil einer Ladung im Band 20–90 %: nahe 100 % regelt der
Wechselrichter ab und das BMS balanciert, dort bildet der SoC die Energie nicht
mehr linear ab. Eine Ladung von 12 % auf 100 % wird beschnitten, nicht verworfen.
Streuen die Einzelphasen zu stark (MAD/Median über 0,07), wird der ganze Lauf
verworfen – ein Filter auf einzelne Ausreißer schnitte nur eine Seite der
Fehlerverteilung ab.

**Entladewirkungsgrad**: wie viel SoC eine ans Haus gelieferte kWh wirklich kostet.
Entscheidend ist die Methodik: gemessen wird über **zusammenhängende
Entladephasen**, nicht je Slot. Der SoC kommt nur in ganzen Prozent (bei 22 kWh
sind das ~223 Wh) – eine slotweise Zuordnung greift genau die Slots heraus, in
denen der Zähler umspringt, und liefert Werte bis über 1,0.

Zuletzt: 22 Phasen, 249,5 h, 271,4 kWh geliefert gegen 309,5 kWh SoC-Entnahme →
gemessen 0,877 bei einem Modellwert von 0,85.

Das ist der Wert, den ich auf jeder frisch aufgesetzten Anlage nach ein bis zwei
Wochen zuerst prüfen würde. Steht er zu hoch, sagt der Plan einen zu flachen
SoC-Verlauf voraus – über eine Nacht summiert sich das auf zweistellige
Prozentpunkte, ohne dass der Drift-Monitor anschlägt.

## Schritt 4 – Lastprofile

```
python -m ems.load_learning --config config.yaml --apply
```

Lernt aus der Tabelle `load_feedback`, wie ein Gerät seine Leistung über den
Zyklus verteilt, und schreibt das als `power_profile_w` (Leistung je Slot) ins
Overlay. Eine Spülmaschine zieht nicht konstant – Heizphase, Waschen, Abpumpen
sind völlig verschiedene Stufen, und der Optimierer kann das abbilden, sobald er
das Profil kennt.

Voraussetzung ist ein konfiguriertes `power_topic` an der Last. Läufe werden als
zusammenhängende Slots über einer Einschaltschwelle erkannt; kurze Einbrüche bis
`MAX_GAP_SLOTS` (1 h) verschmelzen nicht zu zwei Läufen.

Zuletzt: Spülmaschine übernommen (4 Läufe, 6 Slots, 90 min, Spitze 2077 W),
Trockner abgelehnt (nur 2 Läufe), beide Waschmaschinen ohne Rückmeldung.

---

## Schritt 5 – Prognosearchiv ausdünnen

```
python -m ems.archive_thinning --config config.yaml --apply
```

Löscht keine Messwerte, nur Redundanz in `pv_forecast_archive`. Diese Tabelle
hält je Ausgabezeitpunkt die komplette Prognose – für leckagefreie
Rolling-Origin-Auswertung genau richtig, aber sie wächst mit jedem Zyklus:
gemessen am 14.08.2026 rund **49 600 Zeilen und 14,2 MB pro Tag** (Tabelle plus
zwei Indizes). Auf 730 Tage wären das ~10,4 GB.

Der Aufwand steckt nicht in der Historie, sondern in der Wiederholung:

| | |
|---|---|
| pvlib-Ensemble-Mitglieder | **65 %** aller Zeilen, gelesen nur bis `ensemble_lookback_days` (45 T) |
| `pvmodel:Ost` | 7 280 Zeilen/Tag – derselbe Zielslot in jedem Zyklus neu |

Deshalb:

1. **Rohfenster** `general.forecast_archive_raw_days` (60 Tage) bleibt
   vollständig – deckt den Ensemble-Lookback (45 T) mit Reserve, ebenso
   Quellenauswahl (30 T) und Feldgüte (21 T).
2. **Älter:** je Quelle und Zielslot bleibt eine Prognose je Lead-Bucket
   (0 h plus `ensemble_horizon_hours`, hier 6/24/48 h). Behalten wird die
   *kleinste* Vorlaufzeit ab dem Bucket – genau die, die `read_group_asof` für
   diesen Lead auswählt. Damit sind Kalibrier-Fit und Validierung nach dem
   Ausdünnen bitgleich; ein Test prüft das.
3. **Ensemble-Mitglieder** jenseits des Rohfensters fallen ganz weg.

Größenordnung: 730 Tage kosten so ~300 MB statt ~10,4 GB. Verloren geht die
feine Entwicklung einer Prognose für alte Slots („wie hat sich die Vorhersage
für den 3. Januar über 72 Stunden verändert") – für Kalibrierung,
Quellenvergleich und Validierung wird sie nicht gebraucht.

Ohne `--apply` wird nur gezählt. `--vacuum` gibt den Platz an das Dateisystem
zurück; ohne das bleibt er in der Datei und wird von späteren Einträgen
wiederverwendet.

Solange das Archiv jünger als das Rohfenster ist, meldet der Schritt
„nichts jenseits des Rohfensters" und tut nichts. Es begann am 16.07.2026, die
erste echte Ausdünnung fällt damit Mitte September 2026 an.

---

## Was die Temperatur im laufenden Betrieb tut

Nicht Teil des wöchentlichen Laufs, aber nötig, um dessen Zahlen einzuordnen:
Die Außentemperatur geht an drei Stellen in die Lastprognose ein, und nur die
erste steckt im oben gemessenen Grundmodell.

1. **Tagesauswahl (`similar_days`).** `weight_same_temp` (2,0) bevorzugt
   historische Tage mit ähnlicher Temperatur, `temp_sigma` (4,0) legt das
   Fenster fest. Das ist eine Gewichtung, kein Klimalast-Term – ein Hitzetag
   ohne Gegenstück in der Historie bleibt unterschätzt.
2. **ML-Merkmale.** Der `ml`-Pfad kennt `cdh` = max(0, T − 24 °C) als Kühlgrad,
   `hdh` = max(0, 12 °C − T) als Heizgrad und `temp_24h` als rollierendes
   24-h-Mittel für den Wärmestau – der dritte Hitzetag kühlt anders als der
   erste. Welcher Pfad zieht, entscheidet nicht `forecast.method`, sondern das
   je Vorlauf-Bucket gelernte Ensemble; `method` greift nur als Rückfall, wenn
   zu wenige Archiv-Folds für das Lernen da sind.
3. **Temperatur-Residual.** Obendrauf ein aus den archivierten Folds gelerntes
   Restmodell mit den Stützstellen 10/20 °C (Momentanwert) und 12/18 °C
   (24-h-Mittel), begrenzt auf `temperature_residual_max_adjustment_percent`
   (35 % der Prognose). Es wird nur produktiv, wenn es den archivierten Fehler
   messbar senkt.

Ob und wie stark das greift, steht in jedem Zyklus im Log:

```
Temperatur-Residual Last: mittlere Korrektur +213 W, Maximum +715 W.
Lastmodelle: ... Lastmodell 3/3 Horizonte gelernt, Empfehlung ml · Temperatur-Residual aktiv.
```

Lohnt sich zu prüfen, wenn die Korrektur auffällig groß wird: Liegt das Maximum
bei ~35 % der Prognose, greift die Kappe und das Residual trägt mehr, als ihm
erlaubt ist – dann fehlt dem Grundmodell etwas, das es selbst lernen sollte.

**Was jede Schicht bringt**, steht im Lauf-Archiv unter *Fehler-Heatmap*: Über
der Lastkarte schaltet eine Leiste zwischen dem Endergebnis, dem Ensemble ohne
Residual und den rohen Einzelmodellen um, jeweils mit Gesamt-WAPE und Abstand
zum Endergebnis. Alle Varianten werden dabei **auf denselben Zielslots**
gewertet – ein Zielslot, an dem einer Variante der Wert fehlt, fällt bei allen
heraus. Deshalb zeigt die Leiste weniger Paare als die Karte darüber; nur so
vergleichen die Zahlen dasselbe.

Beim Lesen zwei Ebenen auseinanderhalten: Der Gesamt-WAPE auf den Knöpfen ist
über alle Paare gepoolt und gewichtet damit verbrauchsstarke Slots stärker. Die
Zellen der Heatmap zeigen den Fehler je Zielstunde und Vorlauf. Beide können in
verschiedene Richtungen zeigen – das ist kein Widerspruch, sondern der Hinweis,
dass ein Modell seinen Vorsprung nicht gleichmäßig über den Tag holt.

## Wann etwas übernommen wird

Jeder der drei `--apply`-Schritte hat eigene Gates. Reißt einer davon, bleibt
der alte Wert stehen und das Log sagt warum.

| Schritt | Mindest-Stichprobe | Güte | Grenzen |
|---------|-------------------|------|---------|
| Pool | 96 Fenster | R² ≥ 0,5 | Verlust 30–3000 W/K · Absorption 0,05–1,0 · Stufe 300–30000 W |
| Akku · Kapazität | 4 Ladephasen | Streuung MAD/Median ≤ 0,07 | Schritt ≤ 25 % · verträglich mit der Entladeseite |
| Akku · Entladen | 6 Phasen, 40 h | – | Wirkungsgrad 0,55–0,98 |
| Lastprofil | 3 Läufe | – | 15–720 min · 0,05–30 kWh |

Zwei Sicherungen ziehen sich durch alle drei:

**Dämpfung.** Übernommen wird nie der Messwert, sondern die Mitte zwischen altem
und neuem Wert (`APPLY_BLEND = 0.5`). Der Fit läuft wöchentlich über ein
30-Tage-Fenster; eine untypische Woche – Dauerregen, Party mit offener
Poolabdeckung – soll die Planung nicht voll umwerfen. Der Preis ist Trägheit:
Bei einer echten Änderung sind nach einer Woche 50 % der Lücke geschlossen, nach
drei Wochen 88 %. Am Pool ist das gerade zu sehen, die Stufenleistungen wandern
noch von den Datenblattwerten zu den gemessenen.

**Plausibilitätsgrenzen.** Zentral in `ems/quality.py` (`BOUNDS`) statt verstreut
in den Modulen – ein Fit außerhalb wird verworfen, nicht gekappt.

## Nachsehen und selbst laufen lassen

Letzter Lauf komplett:

```bash
journalctl -u ems-kalibrierung.service --since "-8d" -o cat
```

Nächster Termin und ob der letzte durchlief:

```bash
systemctl list-timers ems-kalibrierung.timer
```

Jeder Schritt läuft ohne `--apply` als reiner Trockenlauf – er misst und
berichtet, schreibt aber nichts:

```bash
.venv/bin/python -m ems.battery_calibration --config config.yaml --days 30
```

Was aktuell wirklich wirkt, steht im Overlay:

```bash
cat /opt/ems/config_overrides.yaml
```

Einen einzelnen Wert wieder loszuwerden heißt, ihn dort zu löschen – dann gilt
wieder die `config.yaml`, bis die nächste Kalibrierung ihn neu setzt.
