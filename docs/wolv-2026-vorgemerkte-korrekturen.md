# WOLV-Pilot 2026: vorgemerkte Korrekturen

Diese Punkte wurden im lokalen Arbeitsstand umgesetzt und werden beim nächsten
Datenbank-Build angewendet. Die unveränderten Quellwerte bleiben erhalten.

## Wiener Mitteldistanz, 26.04.2026, Event 5391: Verein WAT-OL

- ANNE liefert den Quellvereinsnamen `W WAT-OL`. Das führende `W` bezeichnet
  Wien und gehört nicht zum Vereinsnamen. Der Nutzer hat dies bestätigt.
- Der Quellwert wird `WAT-OL` zugeordnet, ohne den beobachteten Quelltext zu
  überschreiben. Auch andere alleinstehende Landespräfixe werden nur dann
  entfernt, wenn der vollständige Rest eindeutig ein bekannter Verein oder
  ein geprüfter Vereinsalias ist. Unbekannte Restnamen bleiben unverändert.
- Im aktuellen Pilot sind 14 Ergebniszeilen betroffen. Bei H15− verhindert
  die fehlende offizielle Vereinszuordnung den WOLV-Abgleich aller drei
  Medaillenplätze; bei H75− betrifft sie den Zweitplatzierten Reinhard Siegert.
- D55−: Auch für die Zweitplatzierte Sonja Wlcek ist `W WAT-OL` nicht als
  Verein erkannt. Die drei Plätze (Claudia Bonek, Sonja Wlcek und Renate
  Biel-Pretting) stimmen zwischen WOLV und OLResults überein.
- H75−: WOLV und OLResults nennen Werner Burmann und Reinhard Siegert auf
  Platz 1 und 2. Zwei weitere Teilnehmer sind mit MP gelistet; ein dritter
  Medaillenplatz ist in der WOLV-Quelle nicht ausgewiesen.
- H15−, H75− und D55− sowie die anderen betroffenen Klassen werden im
  regenerierten Datenbankstand und WOLV-Abgleich geprüft.
