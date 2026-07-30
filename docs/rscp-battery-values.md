# E3/DC-RSCP: SoC- und Kapazitätswerte

Stand: 30. Juli 2026. Diese Notiz trennt bewusst zwischen öffentlich belegter
Tag-Zuordnung, beobachtetem Verhalten am vorhandenen S10 X und daraus
abgeleiteten Empfehlungen. E3/DC veröffentlicht die Semantik der neueren
`BAT_*`-Tags nicht vollständig in einer frei zugänglichen Protokollspezifikation.
Die offiziellen RSCP-Beispieldateien enthalten zwar Taglisten; die ergänzenden
Beschreibungen stammen bei mehreren hier relevanten Tags jedoch aus
Community-Implementierungen. Die Zuordnung ist deshalb nur dort als
Herstellerdefinition bezeichnet, wo sie sich mit einem offiziellen
E3/DC-Dokument belegen lässt.

## Kurzempfehlung

| Wert | Einheit/Auflösung | Bedeutung und geeignete Verwendung |
|---|---:|---|
| `soc` | `%`, ganzzahlig | `EMS_BAT_SOC`: vom EMS bereitgestellter Batterieladestatus. Gut für Anzeige und Fallback, für SoC-Differenzen und Wirkungsgrad-Fits zu grob. |
| `soc_float` | `%`, Float | Kein eigener RSCP-Tag, sondern der rscp2mqtt-Topicname für `BAT_RSOC`. Am vorhandenen Gerät der beste operative SoC für Optimierung, Historie und SoC-Bilanz. |
| `rsoc` | `%`, Float | Der zugrunde liegende RSCP-Wert `BAT_RSOC`; am vorhandenen Gerät exakt das Verhältnis `usable_remaining_capacity / usable_capacity`. |
| `rsoc_real` | `%`, Float, hier faktisch ganzzahlig | `BAT_RSOC_REAL`: am vorhandenen Gerät der gröbere BMS-SoC aus `RC/FCC`. Als Diagnose für Reserve/Kalibrierung verwenden, nicht unmittelbar als verfügbaren Optimierungs-SoC. |
| `usable_remaining_capacity` | `Ah`, Float | Verbleibende elektrische Ladung, keine Energie in Wh. Für Diagnose und als Zähler von `BAT_RSOC` geeignet; nicht direkt in die Energiebilanz einsetzen. |
| `specified_capacity` | `Wh`, Float | Nominelle spezifizierte Batteriekapazität. Für Inventar und Plausibilisierung geeignet, nicht als automatisch nutzbare oder gealterte Modellkapazität. |

`rsco` ist kein bekannter aktueller RSCP- oder rscp2mqtt-Name. Gemeint ist
wahrscheinlich `rsoc`.

## Belegte Zuordnung

### `soc` / `EMS_BAT_SOC`

`e3dc/battery/soc` wird von rscp2mqtt direkt aus `TAG_EMS_BAT_SOC`
veröffentlicht. Die Zuordnung verwendet Prozent und das ganzzahlige
RSCP-Format `UChar8`
([rscp2mqtt-Mapping](https://github.com/pvtom/rscp2mqtt/blob/59d0ee905e7350861fc2cec0d63df72b78f01926/RscpMqttMapping.h#L242-L246),
[Tag-Metadaten](https://github.com/git-kick/ioBroker.e3dc-rscp/blob/97c9cd2d8e0ce95df10af5ef20678c2c77dfaae8/lib/RscpTags.json#L3018-L3031)).
Auch die offizielle E3/DC-KNX-Dokumentation beschreibt den ausgegebenen
„Batterie SOC“ lediglich als aktuellen Batterieladestatus in Prozent
([E3/DC KNX Connect, Seite 19](https://www.e3dc.com/wp-content/uploads/2021/07/KNX-Integration.pdf)).

Der Wert ist die aggregierte EMS-Sicht und für eine Benutzeranzeige passend.
Ein Prozentpunkt entspricht beim aktuell konfigurierten Modell aber bereits
181 Wh. Für die Bestimmung kleiner SoC-Änderungen ist die Quantisierung daher
zu grob.

### `rsoc` und `soc_float` / `BAT_RSOC`

`BAT_RSOC` ist ein `Float32`-Prozentwert und wird in den Tag-Metadaten als
„errechneter SOC Wert“ bezeichnet
([Tag-Metadaten](https://github.com/git-kick/ioBroker.e3dc-rscp/blob/97c9cd2d8e0ce95df10af5ef20678c2c77dfaae8/lib/RscpTags.json#L7763-L7778)).
rscp2mqtt veröffentlicht genau diesen RSCP-Wert unter
`e3dc/battery/soc_float`; `soc_float` ist also nur ein MQTT-Name und keine
weitere SoC-Berechnung
([Mapping](https://github.com/pvtom/rscp2mqtt/blob/59d0ee905e7350861fc2cec0d63df72b78f01926/RscpMqttMapping.h#L438-L440),
[direkte BAT-Abfrage](https://github.com/pvtom/rscp2mqtt/blob/59d0ee905e7350861fc2cec0d63df72b78f01926/RscpMqttMain.cpp#L1979-L1995)).

E3DC-Control liest zunächst den ganzzahligen EMS-SoC und ersetzt ihn durch den
feiner aufgelösten `BAT_RSOC`, sofern beide höchstens einen Prozentpunkt
voneinander abweichen. Das stützt seine Verwendung als präzisere Darstellung
desselben operativen Ladestatus
([E3DC-Control](https://github.com/Eba-M/E3DC-Control/blob/869fe4afb5ed0542fadb0097cd22dc3e4b8afbdb/RscpExampleMain.cpp#L6527-L6529),
[Plausibilisierung](https://github.com/Eba-M/E3DC-Control/blob/869fe4afb5ed0542fadb0097cd22dc3e4b8afbdb/RscpExampleMain.cpp#L6671-L6675)).

### `rsoc_real` / `BAT_RSOC_REAL`

rscp2mqtt weist `BAT_RSOC_REAL` als `Float32` in Prozent aus. Eine frei
zugängliche, belastbare Herstellerdefinition des Zusatzes „REAL“ wurde nicht
gefunden. Die häufig zitierte Beschreibung „Tatsächlicher SOC“ ist in den
Tag-Metadaten ausdrücklich als Import aus RSCPGui gekennzeichnet und damit
keine offizielle E3/DC-Semantik
([Metadaten](https://github.com/git-kick/ioBroker.e3dc-rscp/blob/97c9cd2d8e0ce95df10af5ef20678c2c77dfaae8/lib/RscpTags.json#L7932-L7946)).

Am vorhandenen S10 X entspricht der Wert dem gerundeten Verhältnis aus
`BAT_RC` (Remaining Capacity) und `BAT_FCC` (Full Charge Capacity). Das spricht
für einen BMS-internen, physischen SoC vor der Abbildung auf das operative
nutzbare Fenster. Diese Interpretation ist eine Messinferenz, keine
garantierte Protokolleigenschaft für alle Batterie-Firmwares.

### `usable_capacity` und `usable_remaining_capacity`

rscp2mqtt ordnet beide Werte übereinstimmend als `Float32` in Ah zu:

- `BAT_USABLE_CAPACITY` → `e3dc/battery/usable_capacity`
- `BAT_USABLE_REMAINING_CAPACITY` →
  `e3dc/battery/usable_remaining_capacity`

([rscp2mqtt-Mapping](https://github.com/pvtom/rscp2mqtt/blob/59d0ee905e7350861fc2cec0d63df72b78f01926/RscpMqttMapping.h#L457-L458),
[Topicliste](https://github.com/pvtom/rscp2mqtt/blob/59d0ee905e7350861fc2cec0d63df72b78f01926/TOPICS.md#L47-L65)).
Die ioBroker-Metadaten sind für `usable_remaining_capacity` intern
widersprüchlich: Das Einheitenfeld sagt `Ah`, die Beschreibung `Wh`
([Metadaten](https://github.com/git-kick/ioBroker.e3dc-rscp/blob/97c9cd2d8e0ce95df10af5ef20678c2c77dfaae8/lib/RscpTags.json#L8244-L8258)).
Die RSCP-Rohantwort des vorhandenen Geräts und das exakte Zahlenverhältnis zu
`BAT_RSOC` bestätigen hier Ah als sinnvolle Interpretation.

Ah ist eine Ladungsmenge. Eine Multiplikation mit der momentanen
Batteriespannung liefert nur eine punktuelle Näherung in Wh: Die Spannung
ändert sich mit SoC, Temperatur und Last. Für eine belastbare Energiekapazität
müsste `U · I` über einen vollständigen Referenzzyklus integriert werden.

### `specified_capacity` / `BAT_SPECIFIED_CAPACITY`

Der Wert liegt im `BAT_SPECIFICATION`-Container und wird von rscp2mqtt als Wh
ausgegeben
([Mapping](https://github.com/pvtom/rscp2mqtt/blob/59d0ee905e7350861fc2cec0d63df72b78f01926/RscpMqttMapping.h#L451-L458),
[Tag-Metadaten](https://github.com/git-kick/ioBroker.e3dc-rscp/blob/97c9cd2d8e0ce95df10af5ef20678c2c77dfaae8/lib/RscpTags.json#L8868-L8883)).
Am vorhandenen Gerät beträgt er 22.344 Wh. Das ist die spezifizierte
Herstellerkapazität, nicht automatisch die im Betrieb entladbare Energie.

E3/DC nennt für den S10 X 21 offiziell 20,6 kWh *nutzbare* Kapazität. Der
Hersteller definiert sie als die im Referenzzyklus für den Verbrauch
entladbare Energiemenge; Systemreserve ist bereits berücksichtigt und der
reale Wert darf abweichen
([E3/DC-Datenblatt, Seite 3 und Fußnote 4](https://www.e3dc.com/en/wp-content/uploads/sites/3/2022/02/E3DC_TD_E-and-X-series.pdf)).
Die Differenz zwischen 22,344 kWh RSCP-Spezifikation und 20,6 kWh
Hersteller-Nutzkapazität zeigt bereits, warum `specified_capacity` nicht
ungeprüft als Modellkapazität übernommen werden darf.

## Abgleich am vorhandenen S10 X

Ein unmittelbar aufeinanderfolgender, nur lesender RSCP-Abruf ergab am
30. Juli 2026:

| RSCP-Wert | Messwert |
|---|---:|
| `EMS_BAT_SOC` | 68 % |
| `BAT_RSOC` | 68,2359 % |
| `BAT_RSOC_REAL` | 70,0 % |
| `BAT_USABLE_CAPACITY` | 24,5607 Ah |
| `BAT_USABLE_REMAINING_CAPACITY` | 16,7592 Ah |
| `BAT_FCC` | 25,9 Ah |
| `BAT_RC` | 18,1 Ah |
| `BAT_SPECIFIED_CAPACITY` | 22.344 Wh |

Die beiden beobachteten Identitäten sind:

```text
16,7592 Ah / 24,5607 Ah × 100 = 68,2359 % = BAT_RSOC
18,1 Ah    / 25,9 Ah    × 100 = 69,8842 % ≈ 70 % = BAT_RSOC_REAL
```

Damit ist für dieses Gerät empirisch klar:

1. `soc` ist die ganzzahlige EMS-Darstellung des operativen `BAT_RSOC`.
2. `soc_float`/`BAT_RSOC` beschreibt den Anteil der verbleibenden nutzbaren
   Ladungsmenge.
3. `BAT_RSOC_REAL` folgt dem gröberen BMS-Verhältnis `RC/FCC` und bildet ein
   anderes Bezugsfenster ab.

### Bestätigung aus 14 Tagen lokaler Historie

Eine zusätzliche Auswertung der lokal archivierten RSCP-Werte über 14 Tage
bestätigt, dass die oben gezeigte Momentaufnahme kein Zufall ist:

| Geprüfte Beziehung | Ergebnis |
|---|---:|
| `soc_float` gegen `usable_remaining_capacity / usable_capacity × 100` | MAE 0,0205 Prozentpunkte |
| `rsoc_real` gegen DCB-`remaining_capacity / full_charge_capacity × 100` | MAE 0,389 Prozentpunkte |
| `rsoc_real`, wenn `soc_float = 0 %` | Median 4 % |
| `usable_capacity / FCC` | Median 0,9496 |

Das ist ein starker gerätespezifischer Messbeleg für zwei getrennte
Bezugsfenster:

- `soc_float` bildet nahezu exakt das **operativ nutzbare** Fenster ab.
- `rsoc_real` bildet näherungsweise den **BMS-/DCB-internen** Ladestatus ab.
  Dass bei operativ 0 % noch median 4 % gemeldet werden, ist mit einer
  geschützten unteren Reserve vereinbar.

Die Aussage „Reserve“ bleibt dennoch eine Inferenz: Die Zahlenbeziehung ist
gemessen, aber E3/DC hat die genaue interne Berechnung und den Zweck von
`BAT_RSOC_REAL` nicht öffentlich spezifiziert. Ebenso zeigt das mediane
Verhältnis 0,9496 zwischen `usable_capacity` und `FCC`, dass beide Größen nicht
identisch sind; es beweist für sich allein aber nicht, welche weiteren
Temperatur-, Alterungs- oder Sicherheitskorrekturen das BMS anwendet.

## Konsequenz für das EMS-Modell

1. **Operativen Start-SoC aus `soc_float`/`BAT_RSOC` beziehen.** Das ist der
   SoC, dessen 0…100-%-Fenster zu `usable_capacity` gehört und den das E3/DC
   für den normalen Betrieb abbildet.
2. **`EMS_BAT_SOC` nur als Fallback und Plausibilisierung verwenden.** Ein
   Wechsel auf den ganzzahligen Wert verschlechtert SoC-Deltas und
   Wirkungsgradkalibrierung ohne zusätzlichen Informationsgewinn.
3. **`rsoc_real`, `RC`, `FCC`, `usable_capacity`, Spannung und `ASOC`
   diagnostisch archivieren.** So lassen sich Reserveverschiebungen,
   Kalibrierung und Alterung erkennen, ohne den Optimierer auf ein
   BMS-internes Bezugsfenster umzustellen.
4. **`usable_remaining_capacity` nicht als Wh behandeln.** Das Verhältnis zu
   `usable_capacity` ist belastbar; die direkte energetische Umrechnung ist
   wegen der variablen Spannung nicht belastbar genug.
5. **`specified_capacity` niemals automatisch in `capacity_wh` schreiben.**
   Sie ist eine nominelle Obergrenze, nicht die aktuelle, operative
   SoC-zu-Energie-Abbildung.

Im aktuellen Repository ist die Historienquelle auf
`e3dc/battery/soc_float` eingestellt. Die direkte RSCP-Live-Abfrage liest
zusätzlich zu `pye3dc.poll()` leichtgewichtig `BAT_RSOC` und die zugehörigen
Kapazitätswerte. `BAT_RSOC` wird nur übernommen, wenn es im Bereich 0…100 %
liegt und entweder zum Verhältnis
`usable_remaining_capacity / usable_capacity` oder zum ganzzahligen
`EMS_BAT_SOC` passt. `EMS_BAT_SOC` bleibt bei fehlenden beziehungsweise
unplausiblen Batterie-Tags der Fallback.

Die parallel gelesenen Rohwerte werden je Optimierungslauf in der lokalen
SQLite-Tabelle `battery_diagnostics` archiviert. Damit lassen sich Reserve,
FCC/RC, nutzbare Ladungsmenge sowie Spannung und Strom später auswerten, ohne
das operative Standardsignal in `actuals.soc` zu verändern.

## Kapazität und Wirkungsgrade nicht vermischen

Die drei Modellparameter sind nur zusammen interpretierbar. Mit den aktuell
gesetzten Werten ergibt ein vollständiger SoC-Hub:

```text
interner SoC-Energiehub                 = 18,100 kWh
benötigte DC/PV-Ladeenergie bei η=0,99  = 18,100 / 0,99 = 18,283 kWh
lieferbare Entladeenergie bei η=0,85    = 18,100 × 0,85 = 15,385 kWh
benötigte AC-Netzladeenergie bei η=0,90 = 18,100 / 0,90 = 20,111 kWh
```

Aus SoC-Verlauf und einer einzigen Leistungsmessstelle lassen sich Kapazität
und Wirkungsgrad nicht unabhängig bestimmen: Beim Laden beobachtet man nur
`capacity / charge_efficiency`, beim Entladen nur
`capacity × discharge_efficiency`. Die aktuelle Kombination kann deshalb
empirisch zu den gemessenen Steigungen passen, ohne dass 18,1 kWh eine direkt
von RSCP gelieferte Kapazität wäre.

Für eine eindeutige Trennung wären ein vollständiger Referenzzyklus und klar
definierte Messpunkte erforderlich:

- DC-Batterieenergie aus Spannung und Strom für die interne Kapazität,
- AC-seitige Energie am Wechselrichter für Lade-/Entladewirkungsgrade,
- durchgehend `BAT_RSOC` als gemeinsames SoC-Bezugsfenster,
- getrennte Auswertung nach Leistung, Temperatur und SoC-Bereich.

Bis zu einer solchen Messung ist die aus langen realen Phasen kalibrierte
Parameterkombination geeigneter als das Ersetzen von `capacity_wh` durch
22.344 Wh oder 20.600 Wh.

## Quellenlage

Der ioBroker-RSCP-Adapter dokumentiert, dass seine Ausgangsbasis das offizielle
E3/DC-Beispielpaket samt Tagliste ist, zusätzliche Tags und Beschreibungen aber
auch aus RSCPGui und weiteren Community-Projekten stammen
([Quellenübersicht](https://github.com/git-kick/ioBroker.e3dc-rscp/blob/97c9cd2d8e0ce95df10af5ef20678c2c77dfaae8/README-dev.md#L149-L166)).
Deshalb sind die Tag-IDs, RSCP-Datentypen und die am Gerät beobachteten
Zahlenbeziehungen belastbar; die weitergehende physikalische Interpretation
von `RSOC_REAL` bleibt ausdrücklich eine Implementierungs- und Messinferenz.
