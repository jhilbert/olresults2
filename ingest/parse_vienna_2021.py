#!/usr/bin/env python3
"""Recover the parallel Vienna/ASKÖ rankings in the 9 October 2021 PDF.

Each runner remains one physical result; the printed Vienna rank columns are
recorded separately as source-scoped championship evidence. This also keeps
the PDF's explicitly split 45/50 and 65/70 classes distinct.
"""
import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import pdfplumber
from sportsoftware_common import parse_status, parse_time_loose

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data/review/archived_sources/2021/3325-0.pdf"
NORMALIZED = ROOT / "data/normalized/3325-0.json"
EVIDENCE = ROOT / "data/review/regional_source_classes_3325.json"
URL = "https://anne-cdn.oefol.at/public/legacy/event_3325_ergebnisliste-wr-nw-sen-ms-wr-ask-ms-2021-10-09.pdf"
EXPECTED_SHA = "0b5da502c513970f0477f18d741c8369bd667ef425745f7509dc8dc92e572f33"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def lines(pdf):
    for page in pdf.pages:
        groups = defaultdict(list)
        for word in page.extract_words():
            groups[round(word["top"])].append(word)
        for y in sorted(groups):
            yield sorted(groups[y], key=lambda w: w["x0"])


def category_tokens(text):
    return [(("D" if gender == "Damen" else "H") + age)
            for gender, age in re.findall(r"\b(Damen|Herren)\s+(-?\d+(?:-\d+)?-?)", text)]


def parse_document():
    source_bytes = SOURCE.read_bytes()
    # A changed source requires a fresh positional and content review.
    if digest(source_bytes) != EXPECTED_SHA:
        raise ValueError("2021 source PDF changed; positional mapping requires review")
    categories, splits, ranks = [], {}, defaultdict(dict)
    pending_wien = None
    category = None
    category_names = []
    pending_numbers = {}
    with pdfplumber.open(SOURCE) as pdf:
        for words in lines(pdf):
            if not words:
                continue
            text = " ".join(w["text"] for w in words)
            left = " ".join(w["text"] for w in words if w["x0"] < 185)
            if text.startswith("Ergebnisliste "):
                continue
            if left.startswith("Wr. MS "):
                pending_wien = category_tokens(left)
                if not pending_wien or len(pending_wien) > 2:
                    raise ValueError(f"Unexpected Vienna class heading: {left}")
                continue
            if left.startswith("ASKÖ MS "):
                name = left.strip()
                category = {"name": name, "results": []}
                categories.append(category)
                category_names = pending_wien or []
                pending_wien = None
                pending_numbers = {}
                if category_names:
                    splits[name] = {key: [] for key in category_names}
                    splits[name]["__unassigned__"] = []
                continue
            if category is None:
                continue
            numbers = {}
            for w in words:
                if not re.fullmatch(r"\d+", w["text"]):
                    continue
                x = w["x0"]
                for key, low, high in (("overall", 60, 78), ("vienna1", 90, 120),
                                        ("vienna2", 124, 153), ("askoe", 157, 180)):
                    if low <= x < high:
                        numbers[key] = int(w["text"])
            if numbers:
                pending_numbers = numbers
            tail = " ".join(w["text"] for w in words if w["x0"] >= 490)
            surname = " ".join(re.sub(r"^\d+\)", "", w["text"])
                               for w in words if 185 <= w["x0"] < 290
                               or (170 <= w["x0"] < 185
                                   and re.match(r"\d+\)[A-ZÄÖÜ]", w["text"])))
            given = " ".join(w["text"] for w in words if 290 <= w["x0"] < 360)
            club = " ".join(w["text"] for w in words if 360 <= w["x0"] < 490)
            if tail and (parse_time_loose(tail.replace(",", ":")) is not None or parse_status(tail)) \
                    and not (surname and given and club):
                raise ValueError(f"Incomplete result columns: {text}")
            if not (surname and given and club and tail):
                continue
            if not (re.fullmatch(r"\d{1,3}[:,]\d{2}", tail) or parse_status(tail)):
                raise ValueError(f"Unexpected result: {text}")
            name = f"{surname} {given}"
            seconds = parse_time_loose(tail.replace(",", ":"))
            row = {"name": name, "club": club, "timeText": tail,
                   "status": "ok" if seconds is not None else (parse_status(tail) or "unknown")}
            if seconds is not None:
                row["timeS"] = seconds
            if pending_numbers.get("overall") is not None:
                row["rank"] = pending_numbers["overall"]
            category["results"].append(row)
            if category_names:
                found = [(i, key, pending_numbers.get(f"vienna{i+1}"))
                         for i, key in enumerate(category_names)
                         if pending_numbers.get(f"vienna{i+1}") is not None]
                if len(found) > 1:
                    raise ValueError(f"Multiple Vienna categories for {name}")
                if found:
                    _, key, rank = found[0]
                    splits[category["name"]][key].append(name)
                    ranks[key][name] = rank
                else:
                    splits[category["name"]]["__unassigned__"].append(name)
            pending_numbers = {}
    categories = [c for c in categories if c["results"]]
    if len(categories) != 15:
        raise ValueError(f"Unexpected category count: {len(categories)}")
    # The single H15-18 Vienna class includes the WAT-OL starter whose
    # printed outcome is DNF and therefore has no position in either column.
    # Unlike multi-class courses, no age split has to be guessed here.
    single = splits["ASKÖ MS Herren 15-18"]
    if "STÖCHER Jacopo" not in single["__unassigned__"]:
        raise ValueError("Expected H15-18 DNF source row is missing")
    single["__unassigned__"].remove("STÖCHER Jacopo")
    single["H15-18"].append("STÖCHER Jacopo")
    document = {"eventId":3325, "source":"sportsoftware-pdf", "sourceUrl":URL,
                "fileName":SOURCE.name.replace("3325-0", "event_3325_ergebnisliste-wr-nw-sen-ms-wr-ask-ms-2021-10-09"),
                "snapshotPath":SOURCE.relative_to(ROOT).as_posix(), "listType":"race",
                "docDate":"2021-10-09", "categories":categories}
    return document, splits, dict(ranks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    document, splits, ranks = parse_document()
    normalized = json.dumps(document, ensure_ascii=False)
    evidence = {"schema_version":1, "events":{"3325":{"date":"2021-10-09", "stage_number":1,
        "jurisdictions":{"WIEN":{"partial":True, "evidence_kind":"official-results",
          "url":URL, "result_document_url":URL, "sha256":EXPECTED_SHA,
          "normalized_sha256":digest(normalized.encode()),
          "note":"Die Wiener Rangspalten und Klassenteilungen stehen in der offiziellen ANNE-Ergebnisliste; der ASKÖ-Rang ist separat und kein zweiter Start.",
          "categories":splits, "official_ranks":ranks}}}}}
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    for path, content in ((NORMALIZED, normalized), (EVIDENCE, encoded)):
        if args.check:
            if not path.is_file() or path.read_text() != content:
                raise SystemExit(f"Source normalization differs: {path}")
        else:
            path.write_text(content)
        print(f"{path.name}: {'verified' if args.check else 'normalized'}")
    print(f"{len(document['categories'])} courses, {sum(len(c['results']) for c in document['categories'])} source rows")


if __name__ == "__main__":
    main()
