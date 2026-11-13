#!/usr/bin/env python3
"""Reproduce the reviewed archive-only ÖM weekend from preserved originals.

The archive sources were linked by the historical ANNE page, but are no longer
included in its event feed.  Keep the ÖM classification ahead of the complete
Austria-Cup sheet so duplicate observations represent one start, while the
separate Cup-only categories remain available.
"""
import argparse
import json
from pathlib import Path

from parse_sportsoftware_html import decode, parse_document, parse_relay_document, parse_os2003_relay_pre_html

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "data/review/archived_sources/2015"
OUT = ROOT / "data/normalized"
SOURCES = (
    (1368, 0, "1368-nachwuchs-senioren.pdf", "2015-06-13", "NWundSenMS2015_Ergebnis.pdf", "race-pdf",
     "https://web.archive.org/web/20160207111656id_/http://www.nfwien-ol.at/images/stories/naturfreunde/Berichte/20150613NWundSenMS/NWundSenMS2015_Ergebnis.pdf"),
    (1386, 0, "1386-mixed.html", "2015-04-12", "event_1386_noe_mixed_sprint_staffel.htm", "relay",
     "https://anne-cdn.oefol.at/public/legacy/event_1386_noe_mixed_sprint_staffel.htm"),
    (1423, 0, "1423-mixed.html", "2015-05-01", "resultat-teamsprint.html", "relay",
     "https://web.archive.org/web/20160703225932id_/http://www.ol-sbg.at/OEMS_2015/resultat_teamsprint.html"),
    (1424, 0, "1424-relay.html", "2015-05-02", "resultat-staffel.html", "relay",
     "https://web.archive.org/web/20160704051425id_/http://www.ol-sbg.at/OEMS_2015/resultat_oestmstaffelOL.html"),
    (1425, 0, "1425-national.html", "2015-05-03", "resultat-oem.html", "race",
     "https://web.archive.org/web/20170501152209id_/http://www.ol-sbg.at/OEMS_2015/resultat_OEMSJugundSen.html"),
    (1425, 1, "1425-cup.html", "2015-05-03", "resultat-austriacup.html", "race",
     "https://web.archive.org/web/20160704052550id_/http://www.ol-sbg.at/OEMS_2015/resultat_1AC2015.html"),
)


def document(event_id, filename, date, label, kind, url):
    snapshot = ARCHIVE / filename
    if kind == "race-pdf":
        from parse_sportsoftware_pdf import parse_pdf
        categories, _ = parse_pdf(snapshot)
        if not categories:
            raise ValueError(f"Empty archive source: {snapshot}")
        return {"eventId": event_id, "source": "sportsoftware-pdf", "sourceUrl": url,
                "snapshotPath": snapshot.relative_to(ROOT).as_posix(), "fileName": label,
                "listType": "race", "docDate": date, "categories": categories}
    text = decode(snapshot.read_bytes())
    categories = (parse_os2003_relay_pre_html(text) if event_id == 1386
                  else parse_relay_document(text) if kind == "relay" else parse_document(text))
    if not categories:
        raise ValueError(f"Empty archive source: {snapshot}")
    if event_id == 1424:
        # The invitation explicitly calls only these classes Staatsmeisterschaft;
        # the 2015 result's generic champion annotation says merely "Meister".
        for category in categories:
            if category["name"] in {"Damen 19- Elite", "Herren 19- Elite"}:
                for result in category["results"]:
                    if result.get("championship") == "ÖM":
                        result["championship"] = "ÖSTM"
    return {"eventId": event_id, "source": "sportsoftware-html", "sourceUrl": url,
            "snapshotPath": snapshot.relative_to(ROOT).as_posix(), "fileName": label,
            "listType": kind, "docDate": date, "categories": categories}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify committed normalized files only")
    ap.add_argument("--event-id", type=int, help="only process one event")
    args = ap.parse_args()
    for event_id, index, filename, date, label, kind, url in SOURCES:
        if args.event_id is not None and event_id != args.event_id:
            continue
        path = OUT / f"{event_id}-{index}.json"
        content = json.dumps(document(event_id, filename, date, label, kind, url), ensure_ascii=False)
        if args.check:
            if not path.is_file() or path.read_text() != content:
                raise SystemExit(f"Archive normalization differs: {path}")
        else:
            path.write_text(content)
        print(f"{path.name}: {'verified' if args.check else 'normalized'}")


if __name__ == "__main__":
    main()
