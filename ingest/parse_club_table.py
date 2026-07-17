#!/usr/bin/env python3
"""Parse custom (non-SportSoftware) result tables on club-allowlisted domains.

Some clubs publish results with their own hand-built HTML rather than a
SportSoftware export. olc-wienerwald.at's newer pages use one table per event
with a category header row, then finisher rows, then non-finisher rows:

    <tr><td>A Damen</td><td>(15 / 15)</td><td>Zeit</td><td></td></tr>
    <tr><td>1.</td><td>Katarina Fedorova</td><td>Sokol Pezinok</td><td>22:27</td></tr>
    <tr><td>2.</td><td>Josephine Greiner</td><td>Naturfreunde Wien</td><td>23:21</td><td>+0:54</td></tr>
    ...
    <tr><td>&nbsp;</td><td>Hannes Kolar</td><td>Naturfreunde Wien</td><td>Fehlst.</td></tr>

Distinct from parse_sportsoftware_text.py: no <pre> block, no "Pl Stnr Name
Verein Zeit" header, no SportSoftware branding comment - just a plain table.
Detected by the "(finished / entered)" category marker, which SportSoftware
never uses (it prints "(N)" alone).
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from parse_sportsoftware_html import TableExtractor
from sportsoftware_common import (
    CLUB_LINK_ALLOWLIST, is_expected_source_failure, is_junk_name, parse_status,
    parse_time_loose,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
OUT = ROOT / "data" / "normalized"

HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}
CAT_HEADER_RE = re.compile(r"^\((\d+)\s*/\s*(\d+)\)$")
RANK_RE = re.compile(r"^(\d+)\.?$")


def parse_document(html_text):
    ex = TableExtractor()
    ex.feed(html_text)

    categories = []
    current = None
    for table in ex.tables:
        for row in table:
            if not row or all(c in ("", "&nbsp", "&nbsp;") for c in row):
                continue
            if len(row) >= 3 and CAT_HEADER_RE.match(row[1].strip()) and row[2].strip() == "Zeit":
                m = CAT_HEADER_RE.match(row[1].strip())
                current = {"name": row[0].strip(), "declaredStarters": int(m.group(2)),
                           "results": []}
                categories.append(current)
                continue
            if current is None or len(row) < 3:
                continue

            first = row[0].strip()
            rank_m = RANK_RE.match(first)
            is_finisher_row = bool(rank_m)
            is_nonfinisher_row = first in ("", "&nbsp", "&nbsp;")
            if not (is_finisher_row or is_nonfinisher_row):
                continue

            name = row[1].strip()
            if is_junk_name(name):
                continue
            club = row[2].strip()
            time_text = row[3].strip() if len(row) > 3 else ""

            result = {"name": name, "club": club, "timeText": time_text}
            if rank_m:
                result["rank"] = int(rank_m.group(1))
            seconds = parse_time_loose(time_text)
            if seconds is not None:
                result["timeS"] = seconds
                result["status"] = "ok"
            else:
                result["status"] = parse_status(time_text) or "unknown"
            current["results"].append(result)

    return [c for c in categories if c["results"]]


def fetch(url, dest):
    if dest.exists():
        return dest.read_bytes()
    safe_url = urllib.parse.quote(url, safe=":/?&=%#")
    data = urllib.request.urlopen(
        urllib.request.Request(safe_url, headers=HEADERS), timeout=30).read()
    dest.write_bytes(data)
    time.sleep(0.15)
    return data


def decode(data):
    head = data[:1000].decode("ascii", "ignore").lower()
    if "utf-8" in head:
        return data.decode("utf-8", "replace")
    return data.decode("windows-1252", "replace")


def domain_of(url):
    return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")


def collect_jobs():
    attachments = json.loads((RAW / "attachments.json").read_text())
    jobs = []
    for eid, files in attachments.items():
        for n, f in enumerate(files or []):
            if f["mimeType"] == "text/link" and domain_of(f["url"]) in CLUB_LINK_ALLOWLIST:
                jobs.append((int(eid), n, f))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = collect_jobs()
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"club-table candidate links: {len(jobs)}")

    ok = empty = failed = expected_failed = 0
    for eid, n, f in jobs:
        out_path = OUT / f"{eid}-club{n}.json"
        try:
            data = fetch(f["url"], FILES / f"{eid}-club{n}.html")
            text = decode(data)
            if "(" not in text or " / " not in text:
                empty += 1  # cheap pre-check before the full table walk
                continue
            cats = parse_document(text)
            if not cats:
                empty += 1
                continue
            out_path.write_text(json.dumps({
                "eventId": eid,
                "source": "club-table",
                "sourceUrl": f["url"],
                "fileName": f["fileName"] or f["url"],
                "listType": "race",
                "categories": cats,
            }, ensure_ascii=False))
            ok += 1
        except Exception as e:
            if is_expected_source_failure("club-table", eid, n):
                expected_failed += 1
                print(f"  EXPECTED UNAVAILABLE {eid}-{n} {f['url']}: {e}", file=sys.stderr)
            else:
                failed += 1
                print(f"  FAIL {eid}-{n} {f['url']}: {e}", file=sys.stderr)
    print(f"parsed: {ok}, empty: {empty}, expected unavailable: {expected_failed}, failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
