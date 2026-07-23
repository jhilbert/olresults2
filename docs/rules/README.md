# OLRESULTS2-Regelwerk

Dieses Verzeichnis ist das fachliche Book of Record von OLRESULTS2. Es
beschreibt, **was** gelten soll. Der Produktionscode implementiert die Regeln;
automatisierte Tests belegen, dass die Implementierung sie einhält. Ein
Codekommentar allein ist keine neue Fachregel.

## Verbindlichkeit und Änderungen

- `confirmed`: fachlich bestätigt und für veröffentlichte Daten verbindlich.
- `provisional`: derzeit beste, ausdrücklich sichtbare Annahme; darf keine
  endgültige Medaille ohne zusätzliche positive Evidenz erzeugen.
- `draft`: geplant, aber noch nicht produktiv anzuwenden.
- `deprecated`: nur noch für die Herkunft alter Daten dokumentiert.

Eine Regeländerung umfasst immer Regeltext, Code und mindestens einen Test.
Quell- oder eventbezogene Ausnahmen müssen eng begrenzt, begründet und getestet
sein. Direkte Korrekturen der erzeugten SQLite-Datenbank sind nicht zulässig.

## Datenherkunft und Parser

| ID | Status | Regel |
|---|---|---|
| `DATA-001` | confirmed | Jedes Ergebnis verweist auf ein konkretes Quelldokument mit URL beziehungsweise Snapshot, Quellhash und Parserhash. |
| `DATA-002` | confirmed | Beobachtete Namen, Vereine, IDs, Ränge, Zeiten und Status bleiben als Quellevidenz erhalten; Normalisierung darf sie nicht stillschweigend überschreiben. |
| `PARSE-001` | confirmed | Vollständigkeit wird in Quell-Einheiten gemessen: Person bei Einzel/Family, Team bei Paar/Mannschaft/Staffel. Teammitglieder erhöhen die Startzahl nicht. |
| `PARSE-002` | confirmed | Sichtbare Ergebniszeilen mit leerem Wert bleiben erhalten. Ein fehlender Wert wird nicht ohne Evidenz als DNS, MP oder anderer Status geraten. |
| `PARSE-003` | confirmed | Seitenkopf, Seitenfuß, wiederholte Spaltenüberschrift, Kategoriename und Streckendaten dürfen keine Person erzeugen. |
| `PARSE-004` | confirmed | Ein beobachteter Zeittext muss entweder in Sekunden umgewandelt oder als konkreter, prüfbarer Parserbefund ausgewiesen werden. |
| `PARSE-005` | confirmed | Eine Quellenzahl in Klammern kann Meldungen statt sichtbarer Ergebnisse zählen. Eine Abweichung ist deshalb zu erklären, nicht automatisch als Parserfehler zu behandeln. |
| `EXCEPT-001` | confirmed | Eine quell- oder eventbezogene Ausnahme muss eng begrenzt, mit Quelle und Grund dokumentiert sowie durch einen Regressionstest geschützt sein. Globale Heuristiken dürfen nicht aus einem Einzelfall abgeleitet werden. |

## Wettkampfjahr und Saison

| ID | Status | Regel |
|---|---|---|
| `SEASON-001` | confirmed | Das Wettkampfjahr dauert grundsätzlich vom 1. Jänner bis zum 31. Dezember desselben Kalenderjahres. |
| `SEASON-002` | confirmed | Beim Ski-O beginnt das Wettkampfjahr am 1. November des Vorjahres und endet am 31. Oktober. Ein Ski-O-Ergebnis aus November oder Dezember gehört daher zur Saison des folgenden Kalenderjahres. |
| `SEASON-003` | confirmed | Alle Saisonfilter und -gruppierungen in Wettkampf-, Läufer:innen-, Vereins-, Medaillen- und DNS-Sichten verwenden dieselbe disziplinabhängige Wettkampfjahrberechnung. |

## Ergebnis-, Rang- und Statusmodell

| ID | Status | Regel |
|---|---|---|
| `STATUS-001` | confirmed | Der normalisierte Status ist genau `ok`, `dns`, `dnf`, `mp`, `dsq` oder `unknown`. Historische Synonyme werden auf diese Werte abgebildet. |
| `STATUS-002` | confirmed | `AK`/`OOC` ist das unabhängige Feld `out_of_competition`, kein Ergebnisstatus. OOC kann gleichzeitig Zeit, DNS, DNF, MP oder DSQ besitzen. |
| `STATUS-003` | confirmed | Nicht rekonstruierbare Quellwerte bleiben `unknown`; die Oberfläche muss zwischen unlesbarer Quelle und Parserfehler unterscheiden. |
| `STATUS-004` | confirmed | Negative Zeitwerte sind Quell- oder API-Sentinels, keine messbaren Laufzeiten. Der Rohwert bleibt als Evidenz erhalten, wird aber nicht in `time_s` oder `team_time_s` gespeichert. |
| `RANK-001` | confirmed | OOC-Leistungen erhalten keinen regulären numerischen Rang und nehmen an keiner Medaillenberechnung teil; sie bleiben am Ende der Ergebnisdarstellung sichtbar. |
| `RANK-002` | confirmed | Eine Rang-Zeit-Inversion ist nur bei echten Zeitwertungen verdächtig. Score-, Serien-, Spezial- und autoritative ANNE-Rankings sind ausgenommen. |
| `RANK-003` | confirmed | Annullierte Kategorien und ausdrücklich reine Bahnlisten benötigen keine Rangfolge. |

## Personen, Family und Vereine

| ID | Status | Regel |
|---|---|---|
| `FAMILY-001` | confirmed | Family-Kombinationen bleiben als Ergebnis sichtbar, erhalten aber keine `person_id`, keine persönliche Statistik und keine Medaille. |
| `IDENT-001` | confirmed | Eine ÖFOL-ID aus dem ANNE-Personenregister ist der kanonische Personen-Identifier. Quell-ID, Registertreffer und Vereinslistenbestätigung bleiben getrennte Evidenzen. |
| `IDENT-002` | confirmed | Exakter Name plus Geburtsjahr beziehungsweise direkte ÖFOL-ID kann eine Identität auflösen. Nur heutiger Verein plus Name bleibt ein Kandidat, weil heutige Mitgliedschaft historische Ergebnisse nicht umschreibt. |
| `IDENT-003` | confirmed | Mehrdeutige oder widersprüchliche Evidenz erzeugt einen Prüf- oder Konfliktstatus und darf keine automatische Personenverschmelzung auslösen. |
| `IDENT-004` | confirmed | Ein sichtbares Ergebnis ohne verwendbaren Personenbezeichner (zum Beispiel eine SI-Kartennummer im Namensfeld) bleibt als personlose Quellleistung erhalten, erzeugt aber weder Läuferprofil noch Meisterschaftswertung. |
| `CLUB-001` | confirmed | Quellschreibweisen dürfen auf einen bestätigten kanonischen Verein normalisiert werden; beobachteter Vereinsname und kanonischer Verein bleiben getrennt nachvollziehbar. |

## Paar, Mannschaft und Staffel

| ID | Status | Regel |
|---|---|---|
| `TEAM-001` | confirmed | Teamnummer ist der bevorzugte Gruppierungsschlüssel; sonst Teamname, zuletzt ein eng begrenzter stabiler Ersatzschlüssel. |
| `TEAM-002` | confirmed | Ein gemeldetes Team ohne veröffentlichte Mitgliedernamen bleibt als eine memberlose Teamleistung erhalten und erzeugt keine künstliche Person. |
| `TEAM-003` | confirmed | Teamrang und Teamstatus gelten für alle Mitglieder. Der individuelle Leg-Status bleibt zusätzlich erhalten, um MP/DSQ-Verursacher und einzelne Leg-Zeiten anzuzeigen. |
| `TEAM-004` | confirmed | Dieselbe Person darf mehrere Legs laufen, wird aber pro Teamleistung in Personen-, Vereins- und Medaillensichten nur einmal gezählt. |
| `TEAM-005` | confirmed | Nachtlauf-Paare werden als eine Start-/Rang-Einheit und zugleich als getrennt identifizierbare Personen gespeichert. |
| `TEAM-006` | confirmed | Mannschaft bedeutet gemeinsamer Lauf mit einer Teamzeit; Staffel bedeutet aufeinanderfolgende Legs. Beide Modelle dürfen nicht gegenseitig interpretiert werden. |
| `TEAM-007` | confirmed | Eine ausdrücklich veröffentlichte Mannschaftswertung aus getrennten Einzelläufen bleibt ebenfalls ein Teamresultat: Teamrang und Mannschaftssumme werden gemeinsam gespeichert, Einzelzeiten und Einzelstatus je Mitglied zusätzlich. Sie wird nicht als Staffel mit Legs interpretiert. |

## Meisterschaften

| ID | Status | Regel |
|---|---|---|
| `CHAMP-AUT-001` | confirmed | ÖM/ÖSTM-Eligibility benötigt positive Evidenz: offizielle Meisterschaftswertung, ÖFOL-ID mit Nationalität AUT oder bei anderer Nationalität `championshipEligibility=true`; historische eventbezogene Entscheidungen werden nicht aus dem heutigen Zustand rückwirkend neu abgeleitet. |
| `CHAMP-AUT-002` | provisional | Fehlt eine vollständige Wertung, kann Zugehörigkeit zu einem ÖFOL-Verein nur eine vorläufige Eligibility erzeugen. |
| `CHAMP-AUT-003` | confirmed | OOC/AK, Family und DNS erhalten keine nationale Medaille. Eine veröffentlichte Medaille benötigt positive Eligibility-Evidenz. |
| `CHAMP-REG-001` | confirmed | Eine einzelne Leistung darf höchstens einer Landesmeisterschaftswertung zugeordnet sein. Gemeinsame Bahnen sind keine gemeinsame Medaillenwertung. |
| `CHAMP-REG-002` | confirmed | Explizite Quellkennungen wie `W`, `NÖ`, `B`, `St` haben Vorrang vor einer Ableitung aus dem heutigen Verein. |
| `CHAMP-REG-003` | provisional | Ohne explizite Quellkennung ist historische Vereinszugehörigkeit die bevorzugte Evidenz. Heutige Vereinszugehörigkeit allein bleibt prüfpflichtig. |
| `CHAMP-REG-004` | confirmed | Family und OOC dürfen keine Landesmeisterschaftsmedaille erzeugen. |

## Prüfung und Veröffentlichung

| ID | Status | Regel |
|---|---|---|
| `VERIFY-001` | confirmed | Prüfeinheit ist eine Kategorie aus genau einem Quelldokument. Jede Entscheidung ist an deren Eingabefingerprint gebunden. |
| `VERIFY-002` | confirmed | Eine Liste ohne Blocker, Warnung, offene Landeszuordnung oder manuelle Markierung gilt deterministisch als automatisch bestätigt. |
| `VERIFY-003` | confirmed | Manuelle Bestätigung dokumentiert Quelltreue; sie repariert weder Parserwerte noch Identitäten und überstimmt Befunde nur ausdrücklich sichtbar. |
| `RELEASE-001` | confirmed | Vor Veröffentlichung laufen Parser-Golden-Tests, vollständige Tests, DB-Integritäts- und Fachinvarianten sowie ein Qualitätsreport. Parserblocker stoppen die Veröffentlichung. |
| `RELEASE-002` | confirmed | Parseränderungen werden zunächst als normalisierter Diff geprüft; mehrere Reparaturen dürfen anschließend gemeinsam in einem reproduzierbaren DB-Build validiert werden. |

## Verknüpfte Implementierung

- Parser: `ingest/parse_sportsoftware_*.py`,
  `ingest/parse_sportident_center.py`, `ingest/sportsoftware_common.py`
- Datenmodell, Identität und Meisterschaften: `build/build_db.py`
- Saison- und weitere reine Oberflächenregeln: `site/domain_rules.js`
- Release-Invarianten: `build/validate_db.py`
- Prüfoberfläche: `site/review.js`
- Parser-Golden-Fälle: `tests/fixtures/parser/`, `tests/test_parser_golden.py`
- Auditcode-Zuordnung: [`audit-catalog.json`](audit-catalog.json)
- Quellbezogene Ausnahmen: [`source-exceptions.md`](source-exceptions.md)

Die technischen Hintergründe bleiben ergänzend in
[`../identity-provenance.md`](../identity-provenance.md) und
[`../verification.md`](../verification.md) dokumentiert.
