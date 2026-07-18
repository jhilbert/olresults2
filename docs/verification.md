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

Danach `http://127.0.0.1:8643/review.html` öffnen. Der Server bindet nur an
localhost. Entscheidungen werden in `data/review/verification.json`
gespeichert. Auf GitHub Pages ist dieselbe Oberfläche absichtlich nur lesbar.

Tasten: `A` bestätigt die aktuelle Liste und springt zur nächsten offenen
Liste, `F` markiert sie zur Nacharbeit, `J`/`K` wechseln vor/zurück.

## Prüfdimensionen

- `completeness`: Alle Einträge der Originalquelle sind vorhanden.
- `parsing`: Namen, Vereine, Zeiten und DNS/DNF/MP/DSQ/OOC wurden korrekt gelesen.
- `identity`: Jeder personenbezogene Eintrag ist dem richtigen Läuferindex
  zugeordnet. Family ist hier `not_applicable`.
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

## Kampagnen

1. ÖM/ÖSTM: bestehende Eligibility- und Medaillenlogik plus Listenprüfung.
2. Wiener Meisterschaften: erkannte Kandidaten werden zuerst katalogisiert.
   Wiener Medaillen entstehen erst nach bestätigter Instanz und einer aktiven,
   versionierten Wiener Regelmenge.

`championship_instance` trennt AUT und WIEN. `award` ist die gemeinsame
Grundlage künftiger nationaler und Wiener Medaillenspiegel. Die derzeitige
Wiener Regelmenge ist bewusst `draft`; Kandidaten werden noch nicht als Awards
veröffentlicht.
