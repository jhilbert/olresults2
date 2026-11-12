#!/usr/bin/env python3
"""Reproduce the team portion of the original 2017 ÖM Mannschaft PDF.

The separate, existing normalized document retains the individual framework
events.  This document deliberately stops before those individual results.
"""
import argparse
import json
import re
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data/review/archived_sources/2017/2082-1.pdf"
OUTPUT = ROOT / "data/normalized/2082-1.json"
URL = "https://anne-cdn.oefol.at/public/legacy/event_2082_ergebnisliste-oem-mannschaft2017.pdf"
CATEGORY = re.compile(r"^((?:[DH]\s+(?:-\d+|\d+(?:-\d+)?-?)|Offen))(?:\s+\(\d+)?\s+\d+[.,]\d+\s+km\b")
TEAM = re.compile(r"^(?:(\d+|AK)\s+)?(\d+)\s+(.+?)\s+((?:\d+:)?\d+:\d{2}|Fehlst|Disqu|N\s*Ang|Aufg)\s*$", re.I)
MEMBER = re.compile(r"^(.+?)(?:\s+(\d{1,2}))?$")


def parse_lines(lines):
    categories = []
    category = team = None
    pending_champion = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("Einzel-Kurz "):
            break
        match = CATEGORY.match(line)
        if match:
            category = {"name": match.group(1), "sourceUnitCount": 0, "rankingBasis": "time", "results": []}
            categories.append(category)
            team = None
            pending_champion = False
            continue
        if not category:
            continue
        if re.match(r"^1\.\s+und\s+österr\.\s+Meister", line, re.I):
            pending_champion = True
            continue
        match = TEAM.match(line)
        if match:
            place, number, club, result = match.groups()
            status = {"fehlst": "mp", "disqu": "dsq", "n ang": "dns", "aufg": "dnf"}.get(result.lower(), "ok")
            team = {"club": club, "teamName": f"{club} #{number}", "teamNumber": number,
                    "resultKind": "team", "timeText": result, "teamTimeText": result, "status": status}
            if status == "ok":
                parts = list(map(int, result.split(":")))
                seconds = sum(n * 60 ** i for i, n in enumerate(reversed(parts)))
                team.update(timeS=seconds, teamTimeS=seconds)
            if place and place != "AK":
                team["rank"] = int(place)
            elif pending_champion:
                team["rank"] = 1
            if place == "AK":
                team["outOfCompetition"] = True
            if pending_champion:
                team["championship"] = "ÖM"
            pending_champion = False
            category["sourceUnitCount"] += 1
            continue
        if not team:
            raise ValueError(f"Unexpected line outside team: {line!r}")
        member = MEMBER.fullmatch(line)
        if not member:
            raise ValueError(f"Unexpected team member: {line!r}")
        name, year = member.groups()
        row = {"name": name, **team}
        if year is not None:
            y = int(year)
            row["yearOfBirth"] = y + (2000 if y <= 26 else 1900)
            row["strictYearOfBirth"] = True
        category["results"].append(row)
    if len(categories) != 12 or sum(c["sourceUnitCount"] for c in categories) != 84:
        raise ValueError(f"Unexpected source structure: {[(c['name'], c['sourceUnitCount']) for c in categories]}")
    return categories


def document():
    with pdfplumber.open(SNAPSHOT) as pdf:
        lines = [line for page in pdf.pages for line in (page.extract_text() or "").splitlines()]
    if lines[:2] != ["Ergebnisliste", "ÖM Mannschaft 2017"]:
        raise ValueError("Unexpected 2017 source document")
    return {"eventId": 2082, "source": "sportsoftware-pdf", "sourceUrl": URL,
            "snapshotPath": SNAPSHOT.relative_to(ROOT).as_posix(),
            "fileName": "event_2082_ergebnisliste-oem-mannschaft2017-teams.pdf",
            "listType": "race", "docDate": "2017-09-03", "docTitle": "ÖM Mannschaft 2017",
            "categories": parse_lines(lines)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = json.dumps(document(), ensure_ascii=False)
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text() != text:
            raise SystemExit(f"Archive normalization differs: {OUTPUT}")
    else:
        OUTPUT.write_text(text)
    print(f"{OUTPUT.name}: {'verified' if args.check else 'normalized'}")


if __name__ == "__main__":
    main()
