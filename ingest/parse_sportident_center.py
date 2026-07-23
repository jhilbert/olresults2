#!/usr/bin/env python3
"""Normalize reviewed SPORTident Center result snapshots.

SPORTident Center pages are client-rendered and their backing API is not
publicly readable without a browser session.  We therefore keep a small,
reviewable JSON snapshot of the visible result table under
``data/raw/anne/files/<event>-<attachment>.sportident.json``.  This adapter
turns that immutable observation into the same normalized contract as the
other source parsers; it never edits SQLite directly.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from sportsoftware_common import parse_time
from sync_selection import select_jobs

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
OUT = ROOT / "data" / "normalized"

YEAR_RE = re.compile(r"\s+\((\d{4})\)\s*$")
COURSE_RE = re.compile(
    r"(?P<km>\d+(?:\.\d+)?)\s*km\s*/\s*"
    r"(?P<climb>\d+)\s*m\s*/\s*(?P<controls>\d+)\s*controls",
    re.I,
)


def collect_jobs():
    path = RAW / "attachments.json"
    attachments = json.loads(path.read_text()) if path.exists() else {}
    jobs = []
    for event_id, entries in attachments.items():
        for index, entry in enumerate(entries or []):
            host = urlparse(entry.get("url") or "").hostname or ""
            if host.casefold() != "center.sportident.com":
                continue
            snapshot = FILES / f"{event_id}-{index}.sportident.json"
            if snapshot.exists():
                jobs.append((int(event_id), index, entry, snapshot))
    return jobs


def split_name_year(value):
    value = (value or "").strip()
    match = YEAR_RE.search(value)
    if not match:
        return value, None
    return value[:match.start()].strip(), int(match.group(1))


def normalize_snapshot(event_id, entry, snapshot):
    payload = json.loads(snapshot.read_text())
    categories = []
    for source_category in payload.get("categories") or []:
        course_match = COURSE_RE.search(source_category.get("course") or "")
        results = []
        for source_row in source_category.get("rows") or []:
            name, year = split_name_year(source_row.get("name"))
            time_text = (source_row.get("time") or "").strip()
            status = "mp" if time_text.casefold() == "mp" else "ok"
            seconds = parse_time(time_text)
            rank_text = str(source_row.get("rank") or "").strip()
            row = {
                "name": name,
                "club": (source_row.get("club") or "").strip(),
                "rank": int(rank_text) if rank_text.isdigit() else None,
                "timeText": time_text,
                "status": status,
            }
            if year is not None:
                row["yearOfBirth"] = year
            if seconds is not None:
                row["timeS"] = seconds
            behind = parse_time((source_row.get("behind") or "").lstrip("+"))
            if behind is not None:
                row["timeBehindS"] = behind
            results.append(row)
        category = {
            "name": source_category["category"],
            "sourceCategory": source_category["category"],
            "declaredStarters": source_category.get("declared"),
            "sourceUnitCount": len(results),
            "results": results,
        }
        if course_match:
            category.update({
                "courseLengthM": round(float(course_match.group("km")) * 1000),
                "courseClimbM": int(course_match.group("climb")),
                "courseControls": int(course_match.group("controls")),
            })
        categories.append(category)
    return {
        "eventId": event_id,
        "source": "sportident-center",
        "sourceUrl": entry["url"],
        "fileName": entry.get("fileName") or entry["url"],
        "listType": "race",
        "docDate": payload.get("date"),
        "docTitle": payload.get("title"),
        "categories": categories,
    }


def main(argv=()):
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--attachment-manifest", type=Path)
    # Kept for run_sync's common attachment-parser interface.  A reviewed
    # snapshot is deliberately never replaced by an unauthenticated fetch.
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args(argv)

    jobs = select_jobs(
        collect_jobs(), args.event_id, args.attachment_manifest)
    OUT.mkdir(parents=True, exist_ok=True)
    parsed = 0
    for event_id, index, entry, snapshot in jobs:
        normalized = normalize_snapshot(event_id, entry, snapshot)
        (OUT / f"{event_id}-{index}.json").write_text(
            json.dumps(normalized, ensure_ascii=False))
        parsed += 1
    print(f"SPORTident Center snapshots parsed: {parsed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
