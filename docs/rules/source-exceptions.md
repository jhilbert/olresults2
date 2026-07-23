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
| Sportart/Stage | `EVENT_SPORT_TYPE_OVERRIDES` und die Stage-Zuordnung in `build/build_db.py` | Fehlende oder widersprüchliche ANNE-Metadaten werden nur für den konkreten bestätigten Event korrigiert. |

## Pflichtangaben für neue Ausnahmen

1. Event-ID, Datum, Kategorie und konkrete Quelle.
2. Beobachteter Fehler und warum eine allgemeine Parserregel riskant wäre.
3. Erwartetes normalisiertes Ergebnis.
4. Regel-ID, normalerweise `EXCEPT-001` plus die betroffene Fachregel.
5. Regressionstest mit dem kleinstmöglichen Quellausschnitt.
6. Falls zeitlich begrenzt: Gültigkeitszeitraum oder Ablösebedingung.

Ausnahmen dürfen keine erzeugte SQLite-Zeile direkt ändern. Sie müssen vor dem
DB-Build in Auswahl, Parsing, Normalisierung oder fachlicher Ableitung wirken.
