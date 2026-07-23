# Ergebnisprüfung

Die Prüfoberfläche arbeitet auf **Ergebnislisten** (eine Kategorie aus genau
einem Quelldokument), nicht auf einzelnen Datenbankzeilen. Dadurch kann eine
saubere Liste mit einer Entscheidung abgeschlossen werden. Einzelzeilen werden
nur hervorgehoben, wenn eine automatische Prüfung dort einen konkreten Befund
hat.

## Lokal starten

```sh
python3 site/serve.py
```

Danach die vom Server ausgegebene Prüf-URL öffnen (normalerweise
`http://127.0.0.1:8643/review.html`). Ist Port 8643 bereits belegt, wird
automatisch der nächste freie Port verwendet. Der Server bindet nur an
localhost. Entscheidungen werden in `data/review/verification.json` gespeichert.
Auf GitHub Pages ist dieselbe Oberfläche absichtlich nur lesbar.

Tasten: `A` bestätigt die aktuelle Liste und springt zur nächsten offenen
Liste, `F` markiert sie zur Nacharbeit, `J`/`K` wechseln vor/zurück.

## Prüfdimensionen

- `completeness`: Alle Einträge der Originalquelle sind vorhanden.
- `parsing`: Namen, Vereine, Zeiten und DNS/DNF/MP/DSQ/OOC wurden korrekt gelesen.
- `identity`: Jeder personenbezogene Eintrag ist dem richtigen Läuferindex
  zugeordnet. Family ist hier `not_applicable`. Die Oberfläche zeigt getrennt
  Identitätsstatus, Zuordnungsbasis, ÖFOL-ID-Herkunft und unabhängige
  Vereinslisten-Bestätigung.
- `ranking`: Klasse/Bahn und Reihenfolge stimmen mit der Quelle überein.
- `rules`: Nur bei Meisterschaften; Eligibility und Medaillenregel wurden
  geprüft.

Jede Entscheidung enthält den SHA-256-Fingerprint ihrer Eingabeliste. Ändert
sich die Quelle oder die Parserausgabe, wird die alte Entscheidung nicht mehr
angewendet und als `stale_verification` zur erneuten Prüfung markiert.

## Status- und Family-Modell

Der Ergebnisstatus ist einer von `ok`, `dns`, `dnf`, `mp`, `dsq`, `unknown`.
`out_of_competition` (OOC/AK) ist davon unabhängig: auch ein OOC-Läufer kann
etwa DNF sein, nimmt aber nie an Rang- oder Medaillenberechnungen teil. Ein
historisches `nc` ohne ausdrücklichen AK-Hinweis wird nicht geraten, sondern
als `unknown` geprüft.

Eindeutige Family-Kategorien bleiben in der Ergebnisliste, haben aber keinen
`person_id`, `result_kind=family` und `identity_state=not_applicable`. Sie
erscheinen daher weder im Läuferindex noch in persönlichen Statistiken oder
Medaillen. Mehrdeutige Kurzcodes (`F`, `AT-F`) werden nicht automatisch als
Family interpretiert.

Identitätsstatus ist einer von `resolved`, `candidate`, `unresolved`,
`conflict` oder `not_applicable`. `resolved` bedeutet entweder eine direkte
ÖFOL-ID aus der Quelle, einen exakten ANNE-Registertreffer über Name und
Geburtsjahr oder eine bestätigte Vereinsliste. Ein Treffer nur über den
heutigen Verein bleibt bewusst `candidate`.

## Kampagnen

1. ÖM/ÖSTM: Eligibility-, Medaillen- und Listenprüfung.
2. Landesmeisterschaften: Wien, Niederösterreich, Burgenland, Steiermark,
   Oberösterreich, Salzburg, Tirol, Kärnten und Vorarlberg. Gemeinsame Bahnen
   werden anhand expliziter Quellkennungen und historischer Vereinszugehörigkeit
   in getrennte Wertungen partitioniert.

`championship_instance` trennt die nationale Jurisdiktion `AUT` und die neun
Landesjurisdiktionen. `award` ist die gemeinsame Grundlage der nationalen und
regionalen Medaillenspiegel. Die verbindlichen Regeln und noch provisorischen
Ableitungen stehen im [OLRESULTS2-Regelwerk](rules/README.md).

## Sechsstufiger QA-Ablauf

Parseränderungen werden gesammelt und anschließend gemeinsam geprüft, damit
die große SQLite-Datenbank nicht nach jeder Einzelkorrektur neu gebaut werden
muss:

1. **Event- und Quellenabdeckung:** Kalender, Attachment-Inventar,
   normalisierte Parserausgaben und veröffentlichte DB gegeneinander prüfen.
2. **Struktur:** Quellenzahl, geparste Einheiten, Ergebniszeilen, Team-/Leg-
   Gruppierung, Kategorien und Rangabdeckung vergleichen.
3. **Semantik:** Zeiten, Status, AK/OOC, Ränge sowie Kopf-/Fußzeilenartefakte
   auf unmögliche oder nicht gelesene Werte prüfen.
4. **Identität und Verein:** direkte ÖFOL-ID, Name/Jahrgang-Evidenz,
   Kandidatenstatus und kanonische Vereinszuordnung getrennt prüfen.
5. **Meisterschaften:** nationale Eligibility, Landeszuordnung, OOC-/Family-
   Ausschluss und Medailleninvarianten prüfen.
6. **Regression und Release:** normalisierten Diff ansehen, einmal vollständig
   bauen, Qualitätsgate, DB-Invarianten und Tests ausführen.

Die Standardbefehle für die Abschlussphase sind:

```sh
python3 build/normalized_diff.py
python3 build/build_db.py
python3 build/audit_event_coverage.py
python3 build/quality_report.py --fail-on-blockers
python3 build/validate_db.py
python3 -m unittest discover -s tests
```

`cached_sources_without_parsed_rows` ist eine priorisierte Quellen-Queue, aber
nicht automatisch ein fehlendes Event: Bahnlisten, Serienwertungen,
Vereins-/Schulsonderwertungen und schlechtere Parallelquellen können bewusst
außerhalb der veröffentlichten Rennergebnisse bleiben. Ein Releasefehler liegt
vor, wenn ein Event mit Ergebnisquelle gar keine Ergebnisse besitzt oder eine
normalisierte Quelle ohne erklärten Grund unveröffentlicht bleibt.
