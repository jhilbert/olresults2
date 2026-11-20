# Naturfreunde Wien: einbettbare Medaillenansicht

Die Ansicht verwendet ausschließlich die von OLResults berechneten ÖM-/ÖSTM- und Wiener-MS-Podestplätze. Die Jahreswerte 2000–2012 stammen aus der Vereinsstatistik. Teammedaillen werden mit `clubMedalCounts` wie in der Hauptansicht gezählt.

Der Pages-Workflow erzeugt `data.json` nach Datenbankvalidierung und Qualitätsprüfung mit `node build/export_club_embed.js`. Der Export verwendet dieselben Abfragen wie die Hauptansicht und vergleicht für jedes Jahr und jede Disziplin die Zählung vor und nach der Projektion. Die JSON-Datei wird nicht eingecheckt. Lokal kann sie mit demselben Befehl nach einem Datenbank-Build erzeugt werden.

Einbindung (Höhe nach Bedarf anpassen; der Inhalt ist innerhalb des Rahmens scrollbar):

```html
<iframe src="https://jhilbert.github.io/olresults2/embed/naturfreunde-wien/"
        title="Naturfreunde Wien – Meisterschaftsmedaillen"
        width="100%" height="900" loading="lazy"
        style="border:0; width:100%; max-width:100%;"></iframe>
```

Es werden keine Personen- oder Wettkampflinks in der Ansicht dargestellt. Dies ist keine Zugriffsbeschränkung auf die öffentliche OLResults-Datenbank.
