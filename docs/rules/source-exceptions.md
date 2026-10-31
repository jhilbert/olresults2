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
| Landesmeisterschaftsklassen | `data/review/regional_source_classes.json` | Je Event, Etappe und Bundesland durch Ausschreibung oder offizielle Landesliste belegte Quellklassen einschließlich ausdrücklich gemeinsamer Wertungen. |

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
Ein neuer gleichartiger Fall bleibt deshalb je nach Prüftyp ein
Publikationsblocker oder eine offene Warnung.
Die unten angeführten und reproduzierbar geprüften Fälle erscheinen im
Qualitätsreport nur noch als Information. Dadurch bleiben sie im Audit-Trail,
ohne erneut als offene Nacharbeit vorgelegt zu werden.

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

- Events `633` und `856`: Die ANNE-Meldeliste nennt Luise Schöller
  beziehungsweise Markus Ditz in genau der betroffenen Klasse. Da beide in
  der vollständigen Ergebnisliste fehlen, werden sie mit Provenienz zur
  offiziellen Meldeliste als DNS ergänzt.
- Event `4995`: Die ANNE-Meldeliste schließt alle sechs Differenzen namentlich:
  Marina Skern, Christina Satke, Michael Grill, Nikolai Dytlewski, Thomas
  Glatz und Mario Novosel werden als DNS ergänzt.
- Event `1677`: Der OLD-ANNE-Zwischenzeiten-Anhang führt Lerchegger Bernhard
  mit 27:34 noch unter H21-E, die endgültige Ergebnisliste dieselbe Leistung
  bereits unter `Herren18-20`. Es fehlt daher kein Ergebnis; eine Ergänzung in
  H21-E würde denselben Lauf doppelt zählen.
- Events `853`, `1114`, `1677`, `1967`, `3366`, `4254` und `4364`: Ergebnislisten,
  verfügbare ANNE-Meldungen und alternative offizielle Anhänge wurden
  verglichen. Die verbleibenden 18 Klassendifferenzen enthalten nirgends
  zusätzliche veröffentlichte Namen. Sie werden als bestätigte
  Quellenbegrenzung protokolliert und nicht mehr als offene Parserarbeit
  vorgelegt.
- Event `3366`: Auch das unabhängige LiveResultat-Archiv enthält nur 34 statt
  36 Herren-Elite- und 18 statt 20 Damen-Elite-Einträge. Die vier nur in der
  Kopfzahl enthaltenen Meldungen lassen sich deshalb nicht namentlich
  rekonstruieren. Die offizielle Startliste enthält ebenfalls nur dieselben
  34 beziehungsweise 18 sichtbaren Personen; ihre abweichenden Kopfzahlen
  liefern keine zusätzlichen Identitäten.
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

Die erneute Vollprüfung am 23.07.2026 verglich die Rohzeilen aller zehn
betroffenen Events direkt mit den normalisierten Resultaten und zusätzlich
mit ANNE-Meldelisten, Split-/Teamanhängen, Startlisten und erreichbaren
LiveResultat-Daten. Acht belegte DNS-Zeilen werden aus den ergänzenden
Meldelisten rekonstruiert; für die übrigen Differenzen existieren keine
zusätzlichen Namen. Eine bloße
Klassenkopfzahl erzeugt daher weiterhin keine erfundene DNS-Person.

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

## Zusätzliche Landesmeisterschaftsquellen

- Die offiziellen WOLV-Jahreslisten 2024–2026 sind auf
  `http://www.wolv.at/download.php` verlinkt. Ein quellentreuer maschineller
  Auszug ihrer Tabellenzellen steht in
  `data/review/wolv_annual_medal_tables_2024_2026.json`, einschließlich
  Quell-URL, SHA-256, Seiten- und Zellposition sowie Nichtwertungen. Er kann
  mit `build/extract_wolv_annual.py` reproduziert werden. Da Kategorien in
  verbundenen Zellen stehen und Staffeln mehrere Personen enthalten, ist der
  Auszug ausdrücklich nur Prüfmaterial: Er ist noch nicht auf Events oder
  Personen gemappt und bestätigt keine Titel automatisch.
- Event `4404`, Wiener Nachtmeisterschaft 2024: Die offizielle WOLV-Liste
  `http://www.wolv.at/events/2024/Wr.%20Meisterschaften%202024_Nacht.pdf`
  (SHA-256 `92fc35f6467f5cf652c80aa67233c0f5b4874f5cf89f0bd24ecde848a7f688d2`)
  weist H45−/H50− und H65−/H70− getrennt aus. Diese expliziten
  Personenlisten werden übernommen, ohne nach Altersregeln selbst zu teilen.
  H35− wird in H19− gewertet. In H45− druckt WOLV die Ränge 1, 3 und 4;
  wir erhalten diese als Quellränge und markieren die fehlende 2 zur Prüfung.
  Die vier eindeutig veröffentlichten Wiener Jugendpaare sind jeweils zwei
  Personen mit einem gemeinsamen Ergebnis; die ungeteilte Namenszelle bleibt
  als Quellenbeobachtung erhalten.
- Event `4847`, Mittel 2025: Die WOLV-Jahresliste `http://www.wolv.at/docs/med25.pdf`
  bestätigt auch die Wiener Klassen D/H13–14. Die Klassen dürfen nicht mit
  gleichnamigen niederösterreichischen Klassen zusammengeführt werden; für
  Klassen ohne ausreichende Starterzahl wird keine Medaille erfunden.
- Event `4848`, Nachwuchs/Senioren 2025: Die offizielle Liste
  `http://www.wolv.at/events/2025/Wr.NW+Sen.-MS.pdf` (SHA-256
  `9b4f4be9794e464ad3701a77229ee9ff2d89ef53c67b82ab95b7111254831f19`)
  teilt D45−/D50−, D55−/D60− und H55−/H60− tatsächlich nach namentlich
  veröffentlichten Wertungen. Ausschließlich diese konkreten Zuordnungen
  werden übernommen. D60− und H35− erhalten keine Medaille: Gestartet sind
  jeweils nur Läuferinnen bzw. Läufer eines Vereins. Die WOLV-Jahresliste
  nennt zusätzlich D13–14 mit Anna Skern, Matilda Buschek und Annina
  Urbanek, die weder in dieser offiziellen Tagesliste noch in der
  ANNE-Tagesliste erscheinen; dieser Quellenwiderspruch bleibt offen und
  wird nicht automatisch als Wettkampfergebnis eingefügt.
- Event `5156`, Wiener Staffel 2025: In H15− sind laut WOLV drei Staffeln
  eines einzigen Vereins gestartet; trotz vorhandener Rangfolge wird kein
  Wiener Meistertitel vergeben. Die einzelnen Legs werden bei der
  Startzählung als eine Mannschaft pro Team zusammengefasst. Das in diesem
  Event verkürzte Teamlabel „Naturfreunde“ wird anhand der vom WOLV
  veröffentlichten Teamaufstellungen und der verifizierten Mitgliederliste
  eventbezogen Naturfreunde Wien zugeordnet; dieselbe Abkürzung wird nicht
  pauschal bei anderen Veranstaltungen aufgelöst.
- Event `5496`, Nachwuchs/Senioren 2026: Die offizielle WOLV-Liste
  `http://www.wolv.at/events/2026/Wr.NW%20u.Sen-MS%2026.pdf` (SHA-256
  `3b06a7c5c5f6555dd2cfae1251059dc5de2d5d0c434f2d63f50e80da5b2032cd`)
  weist D55−/D60− und H45−/H50− getrennt aus; nur die dort namentlich
  dokumentierten Zuordnungen werden verwendet. Nicht zuordenbare DNS und
  in der Wiener Wertung nicht enthaltene Gäste werden nicht einsortiert.
  H55−: Josef Hilbert erscheint im ANNE-Ergebnis, jedoch nicht in der
  WOLV-Tagesliste. Am 31.10.2026 hat der Nutzer ausdrücklich bestätigt,
  dass die ANNE-Wertung mit Josef Hilbert gilt. Sein Ergebnis wird daher
  als nutzerbestätigte Ergänzung in H55− übernommen; der Unterschied zur
  WOLV-Tagesliste bleibt als Quellenhistorie dokumentiert.
  H65−: WOLV führt Johannes Juraszovich als AK, ANNE als regulär gewertet;
  auch diese Abweichung bleibt ein Prüfhinweis. Die allgemeinen
  Klassen D/H19− sind bei dieser Nachwuchs-/Seniorenmeisterschaft laut
  Tagesliste keine Meisterschaftsklassen.
- Event `5391`, Mittel 2026: Die WOLV-Tagesliste nennt Albert Imriska als
  Meister H−14; die WOLV-Jahresliste nennt an der entsprechenden Stelle
  Adam Imriska, obwohl Adam laut Tagesliste in H15− startete. Keine
  automatische Personenvertauschung allein aufgrund der Jahresübersicht.
- Event `4251`, NÖ und Wiener Mitteldistanzmeisterschaft am 20.04.2024:
  Die vom Nutzer benannte Ausschreibung
  `https://anne-cdn.oefol.at/public/legacy/event_4251_ausschreibung-no-w-ms24-mittel.pdf`
  (SHA-256 `0e8d64ae496c7209f87405f43e98693cdc4a2a7e39422de5bb679832c56f3e26`)
  nennt auf Seite 1 beide Landesmeisterschaften und getrennte Klassen. NÖ:
  D/H−12, −14, −18, 19−, 35−, 45−, 55− und 65−. Wien: D+H−12,
  D/H−14, D/H15−, D/H45−, D55−, H60−, D65− und H75−. Familie,
  Neulinge, Offen und Kinderfähnchen sind Rahmenbewerbe. Die separate
  Ergebnisquelle `event_4251_erg200424.pdf` verwendet dafür die Präfixe
  `N` und `W`. Die Ausschreibung ist derzeit nicht im lokalen Ergebnis-
  Attachment-Inventar enthalten. Die belegten Klassen werden über
  `regional_source_classes.json` der jeweiligen Landeswertung zugeordnet;
  persönliche Berechtigung und Mindeststarter bleiben gesondert zu prüfen.
  Die unabhängige WOLV-Liste `http://www.wolv.at/events/2024/Wr.MS_mittel_24.pdf`
  belegt darüber hinaus für die im Quelleintrag verkürzten Namen die zwei
  unterschiedlichen Personen Sabine und Franz Wareyka-Glaner sowie für
  Nick Dytlewski den Verein Naturfreunde Wien. Die Korrekturen gelten nur für
  diese Quellzeilen; der ursprüngliche Parserwert bleibt beobachtbar.
- Event `4300`, NÖ/W Mixed-Sprintstaffel am 30.06.2024: Die Ausschreibung
  `https://anne-cdn.oefol.at/public/legacy/event_4300_ausschreibung-ol-sprint-und-mixsprint-30-06-2024-mit-29-05-2024.pdf`
  (SHA-256 `d4e43ac12aa201b795be3a446f6d91cca3d1dd8bcd6a24a02fbc56612ea83c6d`)
  nennt auf Seite 3 für Wien `W-16`, `W Allg Klasse`, `W-35`, `W50-`
  und für NÖ `N-14`, `N15-`, `N50-` als Meisterschaftsklassen. Das W in
  `W-16` bezeichnet hier Wien und nicht die internationale Frauenklasse.
  Die Klassen werden über den expliziten Quellnachweis zugeordnet; die
  erforderliche Mindestzahl der wertungsberechtigten Teams ist getrennt zu
  prüfen. Die Ergebnisquelle enthält in `W-16` nur zwei Teams.
- Event `4589`, 7. Austria Cup am 08.09.2024: Die Ausschreibung
  `https://anne-cdn.oefol.at/public/legacy/event_4589_ausschreibung-7-austria-cup-2024-v1-2.pdf`
  (SHA-256 `2eafb6f5d9c74172ff0b40471c6cf2ab6e745b58e70c9e85c7ada36f770ddf58`)
  sagt auf Seite 1 ausdrücklich, dass NÖ/W-Jugend- und Seniorenmeisterschaften
  auf den Austria-Cup-Ergebnissen beruhen. Die genaue Zuordnung der
  Meisterschaftsklassen und Wertungsberechtigten sind separat zu prüfen;
  nicht alle ursprünglichen Wiener Klassenkandidaten sind Meisterschaftsklassen.
  Die konkrete Zusammenlegung und Reihung ist durch die offizielle
  WOLV-Ergebnisliste `http://www.wolv.at/events/2024/Wr.%20Meisterschaften%202024_NW%2BSen.pdf`
  (SHA-256 `5782274e94ee7f88f6feedd168b86303bfa94ae9857a3a5a1d99deca1fc45a90`)
  belegt; die dort zusammengeführten identischen Bahnen werden über die Zeit
  und nicht über die getrennten Austria-Cup-Ränge gereiht.
- Event `4588`, Staffel am 07.09.2024: Die Ausschreibung
  `https://anne-cdn.oefol.at/public/legacy/event_4588_ausschreibung-o-stm-staffel-2024-v1-2.pdf`
  (SHA-256 `18feb6f1289ec3cd132096a0549b160e0e1ad224e93f6989db626fbb4464b58e`)
  legt auf Seite 2 fest, dass nur die Herrenstaffeln für Wien gewertet
  werden; die Damenmeisterschaft wird am 27.10. getrennt ausgetragen.
  Die offizielle WOLV-Ergebnisliste ist
  `http://www.wolv.at/events/2024/Wr.%20Meisterschaften%202024_Staffel.pdf`.
- Event `4617`, Wiener Damenstaffel am 27.10.2024: Die Ausschreibung
  `https://anne-cdn.oefol.at/public/legacy/event_4617_ausschreibung-wr-staffel-ms-damen-27-10-2024-003.pdf`
  (SHA-256 `c1cbe416e97403220b0862bc935cf1e29dd46ae30dda9571279831ac7a0da600`)
  nennt D−14, D15−, D80−, D100− und D120− als Klassen und regelt bei zu
  geringer Teilnahme die Zusammenlegung von Seniorenklassen; sie nennt dabei
  drei Staffeln aus zwei verschiedenen Vereinen. Die tatsächlich ausgetragenen
  Klassen D15−, D80− und D100− sind in der offiziellen, über die
  WOLV-Meisterschaftsübersicht verlinkten Ergebnisliste belegt; die allgemeine
  Mindeststarterprüfung ersetzt keine Prüfung der in
  dieser Ausschreibung genannten zusätzlichen Vereinsbedingung.
- Event `4915`, Wiener/NÖ Langdistanz am 28.09.2025: Die Ausschreibung
  `https://anne-cdn.oefol.at/public/legacy/event_4915_ausschreibung-wr-no-staffel-und-lang-update-05-09-25.pdf`
  (SHA-256 `5434de0aae16ea4f56b925c7227b7f56f75e25762fc62d8dde56b8f3eaba0399`)
  unterscheidet auf Seite 2 für die allgemeine Landesmeisterschaft nur
  D/H19− in Wien und NÖ; die übrigen Altersklassen sind Rahmenbewerbe und
  zugleich separat für die Wiener ASKÖ-Meisterschaft ausgeschrieben. Die
  bestehenden Kandidaten anderer Altersklassen sind deshalb nicht allein
  aufgrund des Veranstaltungstitels als Wiener MS zu bestätigen.
- Event `5247`, Landesmeisterschaften Lang am 28.03.2026: Die Ausschreibung
  `https://anne-cdn.oefol.at/public/2026/01/DszDn13qt8Ly4OYeN2F2X2zKiWxxAZrsBpQIt3PB.pdf`
  (SHA-256 `6ae034ed50420b1af0e63f1ad1825b6a1b7ebc613f7998ce412a341cbcddaedc`)
  nennt auf Seite 1 für Wien nur D/H19 als allgemeine Meisterschaft und
  bezeichnet alle Jugend- und Seniorenklassen für Wien als Rahmenbewerbe.
  D/H19 werden als Wiener Klassen zugeordnet; die übrigen Wiener Kandidaten
  sind keine Wiener Meisterschaftsklassen und werden nicht bestätigt.
- Event `5370`, Nacht-LM am 25.04.2026: Die Ausschreibung
  `https://anne-cdn.oefol.at/public/2026/02/csBAqlJc9kCkOZPBBdeFY7nV1tRweQNS86yUrGJf.pdf`
  (SHA-256 `1bc27fe51c146de56ac3a7efa138323e5dc36cd8c631853daa4d5d615ef7aee5`)
  und die Wiener Liste `http://www.wolv.at/events/2026/Wr.Nacht-MS%2026.pdf`
  belegen die Wiener Klassen. In D19− führt WOLV drei Platzierte, aber keinen
  Titelvermerk. Die offizielle Jahres-Medaillenliste
  `http://www.wolv.at/docs/med26.pdf` klärt den Grund ausdrücklich: Alle drei
  Starterinnen waren vom selben Verein; deshalb gab es keine Wertung. Die
  Rangliste bleibt sichtbar, aber es werden keine Medaillen vergeben. ANNE
  stellt die Namen der Nacht-Paare in zwei durch
  Schrägstriche begrenzten Namensfeldern bereit: Nur eindeutig lesbare Paare
  dieses Events werden in zwei Personen mit gemeinsamer Startnummer und Zeit
  aufgeteilt; verklebte, nicht eindeutig lesbare Namen bleiben offen.
- Events `5382` und `5391`, Mixed-Sprintstaffel am 21.06.2026 und Mittel-LM
  am 26.04.2026: Die ANNE-Ausschreibungen
  `https://anne-cdn.oefol.at/public/2026/05/oIsKgnzVygvPdyZxQmba0fckJpWmnPb0dhcS90zi.pdf`
  (SHA-256 `a342f877cbc29bcc9935336acb5262bf5b3018637aadb30773e88fcde3d213c5`)
  und `https://anne-cdn.oefol.at/public/2026/02/UOYUAEpqmtPnYjJXpCZ9mKS6R9ALBNSBTFJbGLzl.pdf`
  (SHA-256 `3be3dd2bbd0c5815e56059a26e74da4cc64e860cbee9e3beafdd546c05821229`)
  belegen die im Quellenregister erfassten Wiener Klassen. In 5391 werden
  die getrennten D/H−12-Quelldaten gemäß der offiziellen gemeinsamen Wiener
  Ergebnisliste nach Zeit als eine D/H−12-Wertung zusammengeführt.
- Event `4879`, Wiener Sprintmeisterschaft 2025: Die offizielle WOLV-Liste
  `http://www.wolv.at/events/2025/Wr.Sprint-MS.pdf` wurde am 29.10.2026
  vollständig mit den Wiener Siegern abgeglichen (SHA-256
  `4c17ae783b7026683d9bac2d95a55adf404b86bb598bd715f1863e1a82e5a4a0`).
  Zehn Sieger sind dort ausdrücklich mit Wiener Meistertitel bezeichnet;
  sie stehen mit Quellkategorie, ÖFOL-ID, Verein und Zeit in
  `site/data/regional-title-evidence.json`. Die Quelle bestätigt weder die
  übrigen Personenplatzierungen noch einen geschlechtsspezifischen Fördersatz.
  Albert Imriska steht im PDF in `D-12`, in der ANNE-Quelle in der
  gemeinsamen Klasse `Damen&Herren -12`. In `D 65-` ist Herta Pamlitschka
  Erste; die Quelle zeigt nur zwei Starter, daher wird gemäß bestätigter
  Drei-Starter-Regel keine Medaille vergeben. Die WOLV-Liste führt Annika Springer als Achte;
  die bestehende Vereinszuordnungsregel wird dadurch nicht verändert.
- Event `4963`, Wiener Nachtmeisterschaft 2025: Die offizielle WOLV-Liste
  `http://www.wolv.at/events/2025/Wr.Nacht-MS.pdf` wurde am 29.10.2026 mit
  den Wiener Siegerleistungen abgeglichen (SHA-256
  `ae271d4f74eb1bb628f18f3e7e84503e7400df829e34c3a58c0f3b8b6b14d954`).
  Sechs Klassen tragen einen ausdrücklichen Titelvermerk. In H−14 sind Emil
  und Arthur Wally als Siegerpaar genannt; die übrigen fünf Titel gehören
  Einzelpersonen. In D15–18, H15–18, D19− und H75− sind erste Ränge ohne
  ausdrücklichen Titelhinweis verzeichnet; die Quelle zeigt jeweils nur einen
  oder zwei Starter und nach der bestätigten Drei-Starter-Regel keine Medaille.
  Titel und nicht ausdrücklich
  bezeichnete Sieger sind separat in `site/data/regional-title-evidence.json`
  dokumentiert. Eine übereinstimmende Titelzeile belegt die konkrete
  Leistung, nicht automatisch andere Platzierungen oder den Fördersatz.
- Event `4830`, ÖM Mitteldistanz 2025: Die offizielle Ausschreibung auf
  ANNE führt auf Seite 2 die ÖM-Klassen und die separaten Austria-Cup- und
  Rahmenklassen auf. Der genaue, mit den Ergebnis-Klassennamen abgeglichene
  Auszug liegt in `site/data/national-category-evidence.json` (SHA-256 der
  Ausschreibung: `c3d240c5b26fccfb2640e23b94059da0836443943a3db0f7ddc95a648f9cd482`).
  D/H bis 12, 14, 16 Elite und 18 Elite sind ÖM; D/H 15–18 sind bei diesem
  Einzelbewerb nur Austria Cup. Die Formulierung `DH-20E` bleibt wörtlich
  dokumentiert und wird hier nicht als Nachweis einer einzelnen D/H-Klasse
  interpretiert. Die Ausschreibung belegt ausschließlich die Klasse, nicht
  Sieger, Titelvergabe oder individuelle Startberechtigung.
