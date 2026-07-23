# Quell- und Eventausnahmen

Diese Seite ist das Register für eng begrenzte Ausnahmen gemäß `EXCEPT-001`.
Sie ersetzt nicht den Test: Eine bestätigte Ausnahme braucht immer einen
konkreten Regressionstest oder einen Golden-Fall.

## Zulässige Ausnahmearten

| Bereich | Produktionsregister | Zweck |
|---|---|---|
| Attachment-Auswahl | `MANUAL_ATTACHMENT_OVERRIDES`, `MANUAL_ATTACHMENT_SKIP`, `MANUAL_CATEGORY_SKIP` in `ingest/sportsoftware_common.py` | Falsch benannte, doppelte oder nicht als Rennergebnis verwendbare Attachments. |
| Parserformat | `MANUAL_PDF_OVERRIDES`, `MANUAL_HTML_OVERRIDES`, `MANUAL_DOC_DATE_OVERRIDES` in `ingest/sportsoftware_common.py` | Nachweislich falsche Metadaten oder ein eindeutig bestimmtes Sonderlayout. |
| Nationalwertung | `TITLE_FALLBACK_EXCLUDE_EVENTS`, `TITLE_FALLBACK_EXCLUDE_STAGES`, `STAGE_ELITE_OM_OVERRIDE` in `build/build_db.py` | Titel oder Stage-Struktur würden ohne bestätigte Ausnahme eine falsche ÖM/ÖSTM-Wertung erzeugen. |
| Eligibility | `KNOWN_INELIGIBLE_RESULTS` und eventbezogener privater Eligibility-Ledger | Historisch bestätigte, eventbezogene Berechtigung oder Nichtberechtigung. |
| Quellschreibfehler | `KNOWN_NAME_TYPOS`, `KNOWN_RESULT_CLUB_OVERRIDES` in `build/build_db.py` | Fehler steht in der Originalquelle; beobachteter Wert bleibt erhalten, kanonischer Wert wird korrigiert. |
| Quellbeschädigung | `KNOWN_SOURCE_VALUE_CORRUPTIONS` in `build/build_db.py` | Der offizielle Export enthält nur ein unlesbares Fragment; es wird nicht als Parserfehler oder erfundene Zeit behandelt. |
| Quellzählung | `KNOWN_SOURCE_COUNT_ANOMALIES` in `build/build_db.py` | Der Klassenkopf widerspricht der Zahl der darunter sichtbar veröffentlichten Ergebnis-Einheiten. |
| Ergänzende Quellzeilen | `KNOWN_RECOVERED_SOURCE_OMISSIONS` in `build/build_db.py` | Eine zweite offizielle ANNE-Quelle ergänzt namentlich sichtbare Statuszeilen, die in der kompakten Ergebnisliste fehlen. |
| Leerer Quellwert | `KNOWN_SOURCE_MISSING_VALUES` in `build/build_db.py` | Die benannte Quellzeile enthält tatsächlich weder Rang, Zeit noch Status; ein Status wird nicht erfunden. |
| Quellrang | `KNOWN_SOURCE_RANK_ANOMALIES` in `build/build_db.py` | Die veröffentlichte Rangfolge widerspricht der ebenfalls veröffentlichten Zeit. |
| Sportart/Stage | `EVENT_SPORT_TYPE_OVERRIDES` und die Stage-Zuordnung in `build/build_db.py` | Fehlende oder widersprüchliche ANNE-Metadaten werden nur für den konkreten bestätigten Event korrigiert. |
| Eventausschluss | `data/review/excluded_events.json` | Ein Event ohne belastbare Ergebnisquelle bleibt samt Begründung im Quellarchiv, wird aber nicht in die veröffentlichte DB übernommen. |

## Pflichtangaben für neue Ausnahmen

1. Event-ID, Datum, Kategorie und konkrete Quelle.
2. Beobachteter Fehler und warum eine allgemeine Parserregel riskant wäre.
3. Erwartetes normalisiertes Ergebnis.
4. Regel-ID, normalerweise `EXCEPT-001` plus die betroffene Fachregel.
5. Regressionstest mit dem kleinstmöglichen Quellausschnitt.
6. Falls zeitlich begrenzt: Gültigkeitszeitraum oder Ablösebedingung.

Ausnahmen dürfen keine erzeugte SQLite-Zeile direkt ändern. Sie müssen vor dem
DB-Build in Auswahl, Parsing, Normalisierung oder fachlicher Ableitung wirken.

## Bestätigte Widersprüche und fehlende Quellwerte

Die folgenden Fälle wurden direkt im veröffentlichten PDF beziehungsweise HTML
gegen die normalisierten Einträge geprüft. Die Freigabe gilt nur für das
angeführte Event, die Kategorie und die konkrete Zählung beziehungsweise Person.
Ein neuer gleichartiger Fall bleibt deshalb ein Publikationsblocker.

### Falsche Anzahl im Klassenkopf

| Event | Kategorie | Klassenkopf | sichtbare Ergebnis-Einheiten |
|---:|---|---:|---:|
| 853 | Premium | 54 | 55 |
| 1167 | Offen 19- | 14 | 15 Teams |
| 1249 | Damen 65- | 2 | 3 |
| 1367 | Herren B | 29 | 30 einschließlich zwei AK |
| 3134 | DB-Kurz | 4 | 5 |
| 3713 | C | 10 | 11 |

### Tatsächlich leere Ergebniszellen

- Event `1672`: `Damen 10`, Veitsberger Miriam; `Herren 10`, Ehrlich Lilly.
- Event `1947`: `NO H45`, Schuller Georg; die Ergebniszelle enthält nur `–`.
- Event `2020`: `Family`, Annika Springer.
- Event `2375`, Stage 2: `Family`, Böhm Niklas.

Diese fünf Einträge bleiben mit `status=unknown` sichtbar. Aus einer leeren
Quellzelle wird insbesondere nicht automatisch DNS, MP oder DSQ abgeleitet.

### Unlesbare oder unmögliche veröffentlichte Werte

- Event `2865`, `Vereinsmeisterschaft`, Slávka Cahlová: `???`.
- Event `4837`, `Familie`, Leonhardt Tano: `-32:10`.
- Event `5287`, `Kerzen`/`Krippe`: Emmanuiele `-11:25:57`, Serge
  `-11:25:53`, Michaela `-11:23:41`.
- Event `5204`: Die PDF-Zellüberlagerungen `er 11` und `ht 95` enthalten
  keine rekonstruierbare exakte Leg-Zeit.

### Widersprüchliche Quellränge

- Event `1734`, Kategorie `B`: Rang 22 ist laut Quelle schneller als ein
  besser gereihter Eintrag.
- Event `1941`, Kategorie `Bahn B`: Rang 27 ist laut Quelle schneller als ein
  besser gereihter Eintrag.
- Event `2839`, Kategorie `HDS`: dieselbe bestätigte Quellrang-Inversion.

### Gemeldet, aber ohne Ergebniszeile

Eine Klassenkopfzahl ist bei SportSoftware häufig die Zahl der Meldungen,
nicht die Zahl der im Ergebnisbereich angeführten Personen. Eine Differenz
wird nur dann als `source_declared_omission` behandelt, wenn jede sichtbare
Ergebniszeile übernommen wurde. Aus der Kopfzahl allein werden keine
unbekannten DNS-Personen erzeugt.

- Events `4364` und `4995`: Die sichtbaren Ergebniszeilen wurden vollständig
  mit der Quelle verglichen; die verbleibenden zwei beziehungsweise sechs
  Meldungen werden in der Quelle nicht namentlich angeführt. Der Benutzer hat
  diese sieben Klassen am 23.07.2026 ausdrücklich bestätigt.
- Event `3366`: Auch das unabhängige LiveResultat-Archiv enthält nur 34 statt
  36 Herren-Elite- und 18 statt 20 Damen-Elite-Einträge. Die vier nur in der
  Kopfzahl enthaltenen Meldungen lassen sich deshalb nicht namentlich
  rekonstruieren.
- Event `1909`: Die kompakte Ergebnis-PDF lässt sieben DNS-Zeilen aus. Der
  ebenfalls auf ANNE verlinkte Zwischenzeiten-Anhang nennt sie vollständig:
  Diesenreiter Ben; Haselberger Kevin; Fürnkranz Martin; Wendler Michael;
  Pirchegger Günter; Schanes Josef; Fruhwirth Friedrich. Sie werden aus diesem
  zweiten offiziellen Anhang als DNS ergänzt. Rochford Jan bleibt gemäß der
  kompakteren Endergebnisliste DNF und wird nicht durch den älteren
  Zwischenstand überschrieben.
- Event `1947`, `NO H45`: Schuller Georg steht als eigene Quellzeile mit dem
  Ergebniswert `–` in der HTML-Datei. Die Zeile bleibt nun mit
  `status=unknown` sichtbar, statt wegen des nicht standardisierten Werts
  verworfen zu werden.
- Event `1400`: Die vermeintlichen Auslassungen waren dagegen echte
  Parserlücken. `Posten 8 fehlt` und `Posten 10 falsch` werden nun beide als
  MP-Zeilen übernommen.
- Event `4254`: Wiederholte Starts derselben Person innerhalb einer Klasse
  bleiben getrennte Quellzeilen. Insbesondere wird `Jara Leonhardt (2) – DNS`
  neben ihrem gewerteten D-10-Ergebnis gespeichert.

Die erneute Vollprüfung am 23.07.2026 verglich außerdem die Rohzeilen der
Events `633`, `853`, `856`, `1114`, `1677`, `1967`, `4254`, `4364` und `4995`
direkt mit den normalisierten Resultaten. Abgesehen von den oben dokumentierten
Reparaturen enthält keine dieser Quellen weitere namentliche Ergebniszeilen,
die der Parser auslässt. Eine bloße Differenz zur Klassenkopfzahl bleibt daher
ein Quellenhinweis und erzeugt insbesondere keine erfundene DNS-Person.

## Nicht rekonstruierbare ANNE-Altimporte

- Die Events `530` und `2610` liefern über den ANNE-Altimport lediglich auf
  volle Minuten gerundete Zeiten und keine Quellränge.
- Bei Event `530` verweist die ANNE-Seite nur auf die nicht mehr auflösbare
  Veranstalter-Domain `tvff.at`; eine vollständige Ergebnisdatei wurde nicht
  gefunden.
- Für Event `2610` ist das PicoEvents-Archiv zwar noch erreichbar. Die
  Staffel-Liveansicht vom 12.10.2019 wurde jedoch mitten im Bewerb eingefroren
  (nur ein kleiner Teil der 629 gemeldeten Legs ist im Ziel) und ist deshalb
  kein vollständiger Ersatz. Die dort ebenfalls vorhandene Einzelwertung vom
  13.10.2019 ist eine andere Etappe und darf die Staffelzeilen nicht
  überschreiben. Diese beiden ANNE-Datensätze bleiben eingeschränkt;
  Sekunden und Ränge werden nicht erfunden. Beide Events sind deshalb in
  `data/review/excluded_events.json` als nicht veröffentlichbar markiert und
  werden vollständig vor dem DB-Import ausgeschlossen. Roh- und
  Normalisierungsdaten bleiben erhalten, falls später eine belastbare
  Ergebnisquelle auftaucht.

## Rekonstruierte ANNE-Altimporte

- Event `197`: Die auf der ANNE-Seite verlinkte OLC-Wienerwald-Eventseite
  führt zur vollständigen Unterseite `?p=2008/sprinterg`. Der feste
  SportSoftware-Text liefert 233 Zeilen in 19 Klassen mit Sekundenzeiten,
  Rängen und Status.
- Event `1079`: Die noch erreichbare ANNE-CDN-Datei
  `event_1079_Ergebnisse_Sprint.html` liefert 496 Zeilen in 42 Klassen.
- Event `3438`: Die auf der ANNE-Seite verlinkte SPORTident-Center-Wertung
  liefert 48 Zeilen in 16 belegten Klassen. Weil die Seite clientseitig
  gerendert wird und die Backend-API keine anonyme Reproduktion zulässt,
  liegt die sichtbare Tabelle als überprüfbarer Roh-Snapshot vor und wird
  durch `parse_sportident_center.py` deterministisch normalisiert.

## Bestätigte Nicht-Rennquellen

- Events `3226`, `4231`, `4694`, `4695`, `5151`, `5153` und `5154`:
  Die jeweils einzige PDF ist eine periodenübergreifende Street-/Lockdown-Cup-
  Gesamtwertung mit mehreren Laufspalten. Sie ist keine eigenständige
  physische Rennleistung und wird deshalb nicht als Eventergebnis importiert.

## Bestätigte Parallel- und Hilfsquellen

- Split-/Bahnlisten der Events `874`, `1634`, `1893`, `2158`, `2236`, `2240`,
  `2303`, `2447`, `3132`, `3526`, `3652`, `3715`, `4038` und `4087`
  duplizieren das jeweilige vollständige Kategorienresultat.
- Die schmaleren Teilwertungen der Events `1253`, `1409`, `1410` und `2364`
  sind bereits vollständig in der ausgewählten Quelle desselben Rennens
  enthalten.
- Die nach Vereinen statt Klassen gruppierten Listen der Events `2475` und
  `2835` enthalten dieselben Personenleistungen wie die vollständige
  Klassenliste und werden nicht ein zweites Mal importiert.
- Event `1221` enthält zusätzlich eine Vereinswertung und einen PDF-Ausdruck
  derselben vollständigen Web-Ergebnisliste.
- Die Gesamtwertungen der Events `2084`, `2119`, `2274`, `2395`, `2622`,
  `2690`, `2806`, `3184`, `3540`, `3687`, `3699`, `3947`, `3949`, `4408`,
  `4626`, `5278`, `5301` und `5457` kombinieren bereits einzeln gespeicherte
  Etappen beziehungsweise Punkteläufe oder eine periodenübergreifende Serie.
- Die reinen Team-Punktetabellen der Events `2091` und `2430` enthalten keine
  Personen- oder Laufzeiten und sind keine zusätzliche physische Leistung.
- Event `3955` enthält eine Jury-Erklärung ohne Ergebniszeilen; Event `1893`
  zusätzlich eine reine Kinder-Teilnahmeliste ohne Rang oder Zeit.
- Event `4879` enthält neben der W/NÖ Sprint-MS zwei Obstsalat-Side-Event-
  Exporte. Sie gehören nicht zur Meisterschaftsleistung.
- Event `4999` ist ein ausdrücklich so benannter Bewertungstest; sein
  Textanhang enthält nur vier Namen ohne Kategorie, Rang, Zeit oder Status.
- Event `4995`, Attachment `2`, ist die Kontrollzeit-Detailansicht derselben
  Mini-Kids-Läufe aus Attachment `1`.
- Event `952`, Attachment `2`, enthält nur die drei Zwischenrunden der bereits
  als Einzelgesamtzeit und Mannschaftswertung gespeicherten Surprise-
  Mannschaft. Die Runden sind keine zusätzlichen Starts.

## Bestätigte Mannschaftswertungen aus Einzelläufen

- Event `1669`, Attachment `1`: Die NÖ Schul-MS 2016 summiert drei
  Einzelzeiten je Schule. Der spätere Qualifikationsblock ist eine Teilmenge
  derselben Tabelle und wird nicht ein zweites Mal importiert.
- Event `1682`, Attachments `0` und `2`: Die Tiroler Schul-MS 2016 verwendet
  Klassenbezeichnungen nach Schulstufen, die nicht dem D/H-Standardschema
  entsprechen. Die Einzelquelle wird in sechs Klassen getrennt; die
  Mannschaftsquelle veröffentlicht vier Personen je Schule und eine
  Mannschaftssumme. `ohne Wertung` bleibt als OOC ohne regulären Rang erhalten.
- Event `3728`, Attachment `1`: Die NÖ Schul-MS 2022 markiert nur vollständige
  Dreierteams mit Teamrang und Mannschaftssumme. Daneben gedruckte
  Einzelstarter werden nicht zu künstlichen Mannschaften gruppiert.
- Event `952`, Attachment `1`: Die Surprise-Mannschaft 2013 addiert die
  vollständigen Drei-Runden-Zeiten dreier Personen zu genau einem Teamrang.
- Event `3004`, Attachments `0` und `1`: Die vier Schulklassen der Einzelquelle
  werden getrennt importiert; die Mannschaftsquelle speichert vier
  Einzelresultate, Teamrang und Summe der besten drei. Event `4271`,
  Attachment `1`, verwendet dieselbe Mannschaftslogik. MP/DNS eines nicht
  gewerteten vierten Mitglieds macht ein Team mit veröffentlichter
  Mannschaftssumme nicht automatisch ungültig.

## Bestätigte Staffel-Sonderformate

- Event `926`, Attachment `0`: Die Bundesländerstaffel 2013 ist ein
  OS2003-Festbreitenbericht mit drei Klassen und drei beziehungsweise fünf
  Legs. Teamrang, Teamstatus und Leg-Status bleiben getrennt.
- Event `3474`, Attachment `1`: Die Vienna Sprint Relay 2022 hat vier Legs;
  dieselbe Person darf Legs 1/3 beziehungsweise 2/4 laufen. Teams mit DNS und
  ohne veröffentlichte Namen bleiben als memberlose Teamleistung erhalten.

## Mehrstufenquellen

Das Normalformat `stageDocuments` trennt mehrere Etappen aus einem physischen
Anhang. Jede Teilmenge trägt `stageNumber`, `stageDate`, `stageTitle` und
eigene Kategorien; sämtliche Teilmengen verweisen in SQLite weiterhin auf
dasselbe `source_document`. Gesamtzeit und Etappenzeit dürfen dabei nicht
stillschweigend vertauscht werden.

- Event `2091`, Attachment `1`: `E2` und `E3` werden den Stages 3 und 4
  zugeordnet; für eine ausdrücklich `Annulliert E2` markierte Klasse wird
  keine künstliche E2-Teilnahme erzeugt. Das separat vorhandene `E1` bleibt
  die Quelle für Stage 2. Eine namentlich sichtbare Zeile mit leerer
  Etappenspalte wird in dieser Etappe als DNS erhalten.
- Event `2430`, Attachment `3`: `E2` und `E3` liefern die Einzelbewerbe der
  Stages 3 und 4; annullierte E2-Klassen werden ausgelassen. Die Mixed-Relay-
  Quelle desselben Tages bleibt als zusätzlicher eigener Bewerb erhalten.
  Leere Etappenspalten namentlich vorhandener Personen werden als DNS
  gespeichert.
- Event `3681`, Attachment `1`: Die Verfolgung übernimmt den veröffentlichten
  Gesamt-Rang und die Gesamtzeit; die isolierte `E2`-Laufzeit bleibt
  zusätzlich als Quellenhinweis am Ergebnis erhalten. Teilnehmer mit
  vorhandenem E1-Eintrag und leerem E2-Feld bleiben für die Verfolgung als
  DNS sichtbar; dadurch entspricht die Klassenanzahl der Quellkopfzahl.
- Event `4835`, Attachment `1`: Die PDF-Spalten `Time 1/Pos 1` und
  `Time 2/Pos 2` werden als zwei Stages gespeichert. Die Excel-Darstellung
  `MM:SS:00` wird als Minuten/Sekunden interpretiert, nicht als Stunden.

`build/audit_event_coverage.py` zählt auch Zeilen innerhalb von
`stageDocuments`; diese vier Anhänge sind deshalb keine Parserlücken mehr.
