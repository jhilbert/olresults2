#!/usr/bin/env python3
"""Preserve the public ÖFOL Meistertafel as unverified, source-faithful rows.

No event/person/category matching or award decisions are made here. Row and
cell boundaries are kept precisely because older tables use different layouts.
"""
import argparse
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data/review/oefol_meistertafel_rows.json"
SOURCE_URL = "https://www.orienteeringaustria.at/termine/meistertafel/"


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.years = []
        self.summary = None
        self.table = None
        self.row = None
        self.cell = None
        self.tables = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.years.append(None)
        elif tag == "summary" and self.years and self.table is None:
            self.summary = []
        elif tag == "table" and self.table is None and (attrs.get("id") or "").startswith("tablepress-"):
            self.table = {"table_id": attrs["id"], "year": self.years[-1] if self.years else None, "rows": []}
        elif self.table is not None and tag == "tr":
            self.row = {"source_row": attrs.get("class", ""), "cells": []}
        elif self.row is not None and tag in ("td", "th"):
            self.cell = {"text": [], "rowspan": attrs.get("rowspan"), "colspan": attrs.get("colspan")}
        elif self.cell is not None and tag == "br":
            self.cell["text"].append("\n")

    def handle_data(self, data):
        if self.summary is not None:
            self.summary.append(data)
        if self.cell is not None:
            self.cell["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "summary" and self.summary is not None:
            match = re.search(r"\b((?:19|20)\d{2})\b", " ".join(self.summary))
            if match and self.years:
                self.years[-1] = int(match.group(1))
            self.summary = None
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            value = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.cell["text"])).strip()
            cell = {"text": value}
            for key in ("rowspan", "colspan"):
                if self.cell[key]:
                    cell[key] = self.cell[key]
            self.row["cells"].append(cell)
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            if any(c["text"] for c in self.row["cells"]):
                self.table["rows"].append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None
        elif tag == "details" and self.years:
            self.years.pop()


def parse(raw):
    parser = Tables()
    parser.feed(raw.decode("utf-8-sig", errors="replace"))
    parser.close()
    return {"schema_version": 1, "source_url": SOURCE_URL,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "scope": "Source rows only; event, category, identity and title matches remain unverified.",
            "tables": parser.tables}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("html", type=Path)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()
    data = parse(args.html.read_bytes())
    if not data["tables"]:
        raise SystemExit("No source tables found; existing observations were not overwritten")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(args.output)
    print(f"{len(data['tables'])} tables, {sum(len(t['rows']) for t in data['tables'])} rows; {args.output}")


if __name__ == "__main__":
    main()
