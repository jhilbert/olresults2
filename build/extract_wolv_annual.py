#!/usr/bin/env python3
"""Extract WOLV annual medal-table cells as review evidence, not decisions.

The first page uses merged cells and the second page contains relay teams.
Keep the row heading and entire original cell: splitting classes, choosing an
event, and matching a name to a person require separate verification.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path


def extract_cell(text):
    text = text or ""
    match = re.search(r"^\s*1\s*\)\s*(.*?)(?=^\s*2\s*\)|\Z)", text,
                      flags=re.MULTILINE | re.DOTALL)
    winner = " ".join(match.group(1).split()) if match else None
    return {"cell": text, "first_place_text": winner}


def extract_document(year, path):
    import pdfplumber

    path = Path(path)
    with pdfplumber.open(path) as doc:
        if len(doc.pages) != 2:
            raise ValueError(f"{path}: expected two pages")
        first = doc.pages[0].extract_table()
        second = doc.pages[1].extract_table()
        if not first or not second or len(first[0]) != 6:
            raise ValueError(f"{path}: annual medal table layout changed")
        headings = first[0]
        dates = first[1]
        cells = []
        for row_index, row in enumerate(first[2:], 2):
            for column in range(1, 6):
                if row[column]:
                    cells.append({"page": 1, "row": row_index, "column": column,
                                  "row_heading": row[0], "discipline_heading": headings[column],
                                  "event_heading": dates[column], **extract_cell(row[column])})
        for row_index, row in enumerate(second[1:], 1):
            if row[0]:
                cells.append({"page": 2, "row": row_index, "column": 0,
                              "row_heading": row[0].splitlines()[0],
                              "discipline_heading": second[0][0],
                              "event_heading": second[0][0], **extract_cell(row[0])})
    return {"year": year, "source_url": f"http://www.wolv.at/docs/med{year % 100:02}.pdf",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "extraction_status": "unmatched_review_only", "cells": cells}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True,
                        help="directory containing wolv-meister-24.pdf etc.")
    parser.add_argument("--output", type=Path,
                        help="write the generated review snapshot to this file")
    args = parser.parse_args()
    result = {"schema_version": 1,
              "purpose": "Review evidence only; no automatic event, person or award matching.",
              "documents": [extract_document(year, args.pdf_dir / f"wolv-meister-{year % 100:02}.pdf")
                            for year in (2024, 2025, 2026)]}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
