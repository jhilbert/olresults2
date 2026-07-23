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

from parse_sportsoftware_html import TableExtractor, parse_bracket_html
from sportsoftware_common import (
    CLUB_LINK_ALLOWLIST, is_expected_source_failure, is_junk_name, parse_status,
    parse_time_loose,
)
from sync_selection import select_jobs

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
FILES = RAW / "files"
OUT = ROOT / "data" / "normalized"

HEADERS = {"User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}
CAT_HEADER_RE = re.compile(r"^\((\d+)\s*/\s*(\d+)\)$")
COMBINED_CAT_HEADER_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<finished>\d+)\s*/\s*(?P<entered>\d+)\)$")
RANK_RE = re.compile(r"^(\d+)\.?$")


def parse_document(html_text):
    ex = TableExtractor()
    ex.feed(html_text)

    categories = []
    current = None
    layout = None
    for table in ex.tables:
        for row in table:
            if not row or all(c in ("", "&nbsp", "&nbsp;") for c in row):
                continue
            combined = COMBINED_CAT_HEADER_RE.fullmatch(row[0].strip())
            if combined:
                current = {
                    "name": combined.group("name").strip(),
                    "declaredStarters": int(combined.group("entered")),
                    "results": [],
                }
                categories.append(current)
                layout = "sportsoftware"
                continue
            if len(row) >= 3 and CAT_HEADER_RE.match(row[1].strip()) and row[2].strip() == "Zeit":
                m = CAT_HEADER_RE.match(row[1].strip())
                current = {"name": row[0].strip(), "declaredStarters": int(m.group(2)),
                           "results": []}
                categories.append(current)
                layout = "counted"
                continue
            if (len(row) >= 3 and row[0].strip()
                    and row[2].strip() == "Zeit"
                    and row[0].strip().casefold() not in {"pl", "platz"}):
                current = {
                    "name": row[0].strip(),
                    "declaredStarters": None,
                    "results": [],
                }
                categories.append(current)
                layout = "simple"
                continue
            if current is None or len(row) < 3:
                continue

            first = row[0].strip()
            rank_m = RANK_RE.match(first)
            is_finisher_row = bool(rank_m)
            is_nonfinisher_row = first in ("", "&nbsp", "&nbsp;")
            is_ooc_row = first.casefold() == "ak"
            if not (is_finisher_row or is_nonfinisher_row or is_ooc_row):
                continue

            if layout == "sportsoftware":
                if len(row) < 6:
                    continue
                name = row[2].strip()
                club = row[4].strip()
                time_text = row[5].strip()
            else:
                name = row[1].strip()
                # Prefer an actual result time anywhere to the right. Club
                # names such as ``OK gittis Klosterneuburg`` legitimately
                # begin with the token ``OK`` and must not become a
                # qualitative status value.
                value_index = next(
                    (index for index, cell in enumerate(row[2:], 2)
                     if parse_time_loose(cell.strip()) is not None),
                    None,
                )
                if value_index is None:
                    status_indices = [
                        index for index, cell in enumerate(row[2:], 2)
                        if parse_status(cell.strip()) is not None
                    ]
                    value_index = (
                        status_indices[-1] if status_indices else None
                    )
                if value_index is None:
                    continue
                club = " ".join(
                    cell.strip() for cell in row[2:value_index]
                    if cell.strip() not in ("&nbsp", "&nbsp;"))
                time_text = row[value_index].strip()
            if is_junk_name(name):
                continue

            result = {"name": name, "club": club, "timeText": time_text}
            if rank_m:
                result["rank"] = int(rank_m.group(1))
            if is_ooc_row:
                result["outOfCompetition"] = True
            seconds = parse_time_loose(time_text)
            if seconds is not None:
                result["timeS"] = seconds
                result["status"] = "ok"
            else:
                result["status"] = parse_status(time_text) or "unknown"
            current["results"].append(result)

    parsed = [c for c in categories if c["results"]]
    for category in parsed:
        if category["declaredStarters"] is None:
            category["declaredStarters"] = len(category["results"])
    return parsed


def fetch(url, dest, force=False):
    if dest.exists() and not force:
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
    ap.add_argument("--event-id", type=int, help="only process one ANNE event")
    ap.add_argument("--attachment-manifest", type=Path,
                    help="only process attachments listed by the current incremental sync")
    ap.add_argument("--force-download", action="store_true",
                    help="re-download selected source files even when cached")
    args = ap.parse_args()

    FILES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = collect_jobs()
    jobs = select_jobs(jobs, args.event_id, args.attachment_manifest)
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"club-table candidate links: {len(jobs)}")

    ok = empty = failed = expected_failed = 0
    for eid, n, f in jobs:
        out_path = OUT / f"{eid}-club{n}.json"
        try:
            shared_cache = FILES / f"{eid}-{n}.html"
            if shared_cache.exists() and not args.force_download:
                data = shared_cache.read_bytes()
            else:
                data = fetch(
                    f["url"], FILES / f"{eid}-club{n}.html", args.force_download)
            text = decode(data)
            cats = parse_document(text)
            if not cats:
                # The saved liveresultat variant has the same category/count
                # blocks but a leading spacer cell and optional team/member
                # rows. Keep one structural implementation for it instead of
                # maintaining a second, subtly different table walker here.
                cats = parse_bracket_html(text)
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
