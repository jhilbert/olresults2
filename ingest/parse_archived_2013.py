#!/usr/bin/env python3
"""Reproduce two reviewed 2013 results from the preserved ANNE sources."""
import argparse
import hashlib
import json
from pathlib import Path

from parse_sportsoftware_html import decode, extract_html_title
from parse_sportsoftware_pdf import (parse_relay_pdf, repair_wrapped_champion_names,
                                     normalize_qualitative_result_ranks)
from parse_sportsoftware_text import extract_pre_blocks, parse_legacy_pre_text
from sportsoftware_common import repair_official_club_status_overflow

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    842: ("842-nacht.html", "38727a36b2e46dfc937064af3396f250e303ec30f3cdec78d4c2748b0f2234c2",
          "event_842_OEMNacht2013.html", "2013-10-19", "sportsoftware-html"),
    851: ("851-mannschaft.pdf", "c97aa2506879a0b6c7f835429c61a40a22256f85d39cfcf73f099238785a027a",
          "event_851_erg_mann.pdf", "2013-06-23", "sportsoftware-pdf"),
    883: ("883-wien-noe-staffel.html", "b25bc14bc2b7aa18a50ebbda292675faf50c0db3fb430bf58893e90a202192ac",
          "http://www.olt-transdanubien.at/data/2013/wrnoe_staffelms_erg.htm", "2013-05-01", "sportsoftware-text"),
}


def document(event_id):
    filename, expected_hash, label, date, source = SOURCES[event_id]
    snapshot = ROOT / "data/review/archived_sources/2013" / filename
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != expected_hash:
        raise ValueError(f"Source snapshot changed: {snapshot}")
    if event_id in (842, 883):
        text = decode(snapshot.read_bytes())
        categories = parse_legacy_pre_text(extract_pre_blocks(text), pair_text1=(event_id == 842))
    else:
        categories = parse_relay_pdf(snapshot, team_mode=True)
        categories = repair_wrapped_champion_names(snapshot, categories)
        categories = normalize_qualitative_result_ranks(categories)
        for category in categories:
            for result in category.get("results") or []:
                repair_official_club_status_overflow(result)
    if not categories:
        raise ValueError(f"Empty source: {snapshot}")
    if event_id == 883:
        return {"eventId": event_id, "source": source, "sourceUrl": label,
                "fileName": label, "listType": "relay", "categories": categories}
    result = {"eventId": event_id, "source": source,
              "sourceUrl": "https://anne-cdn.oefol.at/public/legacy/" + label,
              "fileName": label, "listType": "race", "docDate": date}
    if event_id == 842:
        result["docTitle"] = extract_html_title(text)
    result["categories"] = categories
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    for event_id in SOURCES:
        output = ROOT / "data/normalized" / f"{event_id}-0.json"
        content = json.dumps(document(event_id), ensure_ascii=False)
        if args.check:
            if output.read_text() != content:
                raise SystemExit(f"Normalization differs: {output}")
        else:
            output.write_text(content)
        print(f"{output.name}: {'verified' if args.check else 'normalized'}")


if __name__ == "__main__":
    main()
