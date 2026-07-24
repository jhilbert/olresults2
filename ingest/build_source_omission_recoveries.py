#!/usr/bin/env python3
"""Build normalized DNS supplements from reviewed ANNE entry evidence.

The result source's category header can count registrations which are omitted
from the visible result rows.  A number alone must never create an anonymous
DNS result.  This adapter emits a supplement only for people who are named in
ANNE's official entry list and whose absence from the complete result source
was reviewed separately.
"""
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "review" / "source_omission_recoveries.json"
OUT = ROOT / "data" / "normalized"


def build_document(event):
    categories = defaultdict(list)
    seen = set()
    for row in event.get("results") or []:
        if row.get("status") != "dns":
            raise ValueError(
                f"event {event.get('eventId')}: only reviewed DNS recoveries "
                "are supported")
        key = (row.get("category"), row.get("entryId"))
        if not row.get("category") or not row.get("name") or key in seen:
            raise ValueError(
                f"event {event.get('eventId')}: incomplete or duplicate recovery {key}")
        seen.add(key)
        categories[row["category"]].append({
            "name": row["name"],
            "club": row.get("club"),
            "yearOfBirth": row.get("yearOfBirth"),
            "userId": row.get("userId"),
            "timeText": "DNS",
            "status": "dns",
            "note": (
                "DNS aus offizieller ANNE-Meldeliste: gemeldet und in der "
                "vollständigen Ergebnisquelle nicht angeführt"
            ),
        })
    return {
        "eventId": event["eventId"],
        "source": "anne-entry-recovery",
        "sourceUrl": event["sourceUrl"],
        "fileName": f"event_{event['eventId']}_anne-entry-recovery.json",
        "listType": "race",
        "docDate": event.get("date"),
        "docTitle": event.get("title"),
        "categories": [
            {"name": name, "results": rows}
            for name, rows in categories.items()
        ],
    }


def main():
    payload = json.loads(CONFIG.read_text())
    if payload.get("schemaVersion") != 1:
        raise ValueError("unsupported source omission recovery schema")
    total = 0
    for event in payload.get("events") or []:
        event_id = int(event["eventId"])
        index = int(event["attachmentIndex"])
        document = build_document(event)
        path = OUT / f"{event_id}-{index}.json"
        path.write_text(json.dumps(document, ensure_ascii=False))
        rows = sum(len(category["results"])
                   for category in document["categories"])
        total += rows
        print(f"{path.relative_to(ROOT)}: {rows} reviewed DNS recoveries")
    print(f"source omission recoveries: {total}")


if __name__ == "__main__":
    main()
