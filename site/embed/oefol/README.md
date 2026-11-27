# ÖFOL-Meistertafel einbetten

Die Ansicht nutzt dieselben Daten und dieselbe Logik wie `/oefol/`; sie enthält keinen eigenen Seitenkopf oder Fußbereich. Die Filter und die Auswahl der Ansicht bleiben beim Scrollen innerhalb des iFrames sichtbar.

```html
<iframe
  src="https://jhilbert.github.io/olresults2/embed/oefol/"
  title="Österreichische Meistertafel"
  loading="lazy"
  style="display:block;width:100%;height:900px;border:0;"
></iframe>
```

Die Höhe kann an die Website angepasst werden. Für Sticky-Filter muss der iFrame eine begrenzte Höhe haben und sein Inhalt innerhalb des iFrames scrollen. Bei automatischer Anpassung an die volle Inhaltshöhe scrollt stattdessen die umgebende Website; der Inhalt des iFrames kann dabei nicht am äußeren Browserfenster haften.
