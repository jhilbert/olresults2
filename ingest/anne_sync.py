#!/usr/bin/env python3
"""Sync events, structured results, stages and attachment indexes from ANNE.

Existing attachment entries keep their index forever.  The automatic sync
only emits newly discovered attachment URLs for downstream parsers.  Historic
events can be selected explicitly with ``--event-id``; re-downloading a known
URL is intentionally a parser-level, explicit operation.
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sportsoftware_common import (  # noqa: E402
    CLUB_LINK_ALLOWLIST, MANUAL_ATTACHMENT_OVERRIDES, MANUAL_HTML_OVERRIDES,
    MANUAL_PDF_OVERRIDES,
)

BASE = os.environ.get("ANNE_BASE_URL", "https://anne-api.oefol.at/v1").rstrip("/")
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)",
}
if os.environ.get("ANNE_GATEWAY_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {os.environ['ANNE_GATEWAY_TOKEN']}"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
REFRESH_DAYS = 30
WORKERS = 6
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
NATIONAL_CHAMPIONSHIP_RE = re.compile(
    r"(?i)(?<![A-Za-zÄÖÜäöü])(?:Ö|OE)[–-]?(?:ST)?M(?![A-Za-zÄÖÜäöü])|"
    r"\b(?:österreich\w*\s+)?staatsmeister|\bösterreich\w*\s+meisterschaft")
CHAMPIONSHIP_RANKING_ATTACHMENT_RE = re.compile(
    r"(?i)meisterschafts[-_ ]?wertung")
RESULT_NAMED_ATTACHMENT_RE = re.compile(
    r"(?i)(?:ergebnis(?:se|liste)?|results?|gesamtwertung|"
    r"(?:^|[-_ ])(?:oe|o|ö)(?:st)?m[-_ ].*[-_ ]erg(?:ebnis)?(?:[-_. ]|$))")
NON_RESULT_ATTACHMENT_RE = re.compile(
    r"(?i)(?:split|zwischenzeit|bahndat|meldung|startlist|startzeit|"
    r"einladung|ausschreibung|l[aä]uferinfo|wettkampfinfo|bulletin|protokoll)")


def get(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return json.load(resp)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_events():
    events, page = [], 1
    while True:
        d = get(f"{BASE}/event?perPage=100&page={page}")
        events.extend(d["data"])
        if page >= d["meta"]["lastPage"]:
            break
        page += 1
        time.sleep(0.2)
    (RAW / "events.json").write_text(json.dumps(events, ensure_ascii=False))
    print(f"events: {len(events)}")
    return events


def needs_fetch(path, event, force, refresh_cutoff):
    if force or not path.exists():
        return True
    return (event.get("dateFrom") or "")[:10] >= refresh_cutoff


def sync_event(event, force, refresh_cutoff):
    eid = event["id"]
    fetched = []
    results_path = RAW / "results" / f"{eid}.json"
    if event.get("hasOfficialResults") or event.get("hasUnofficialResults"):
        if needs_fetch(results_path, event, force, refresh_cutoff):
            results_path.write_text(json.dumps(get(f"{BASE}/event/{eid}/results"), ensure_ascii=False))
            fetched.append("results")
    # Not gated on hasOfficialResults/hasUnofficialResults - a legacy event
    # (results only as PDF/HTML attachments, no structured API data at all)
    # can still have real stage metadata worth having, and for a multi-day
    # one it's the only reliable source of per-day dates: build_db.py's
    # legacy path has to guess a date from each attachment's own printed
    # header otherwise, which is really just "when this PDF/HTML was last
    # (re)generated" - confirmed real: event 4114 ("O-Festival 2023"), a
    # 3-day meet whose 3 separate result files were all reprinted on the
    # same later day, so they all guessed the identical wrong date and
    # silently collapsed into one stage instead of three.
    if event.get("stageCount", 0) > 0:
        stages_path = RAW / "stages" / f"{eid}.json"
        if needs_fetch(stages_path, event, force, refresh_cutoff):
            stages_path.write_text(json.dumps(get(f"{BASE}/event/{eid}/stages"), ensure_ascii=False))
            fetched.append("stages")
    return eid, fetched


def has_unusable_structured_results(eid):
    """Some events flagged hasOfficialResults=True actually carry garbage API
    data (SI-card numbers as names like firstName='1212' lastName='605060', no
    category, all disqualified — e.g. event 1127). Those never get an
    attachment fallback otherwise, since they look 'already handled' by the
    results flag. A name is unusable if it has no 2+ letter alphabetic run."""
    path = RAW / "results" / f"{eid}.json"
    if not path.exists():
        return False
    try:
        rows = json.loads(path.read_text())
    except Exception:
        return False
    if not rows or any(r.get("teamMembers") for r in rows):
        return False  # empty, or a relay/team (handled via teamMembers)
    name_re = re.compile(r"[A-Za-zÀ-ÿ]{2,}")
    return not any(name_re.search(f"{r.get('firstName') or ''} {r.get('lastName') or ''}")
                   for r in rows)


def merge_attachment_index(existing, fetched):
    """Append genuinely new URLs without renumbering historic attachments."""
    merged = list(existing or [])
    urls = {item.get("url") for item in merged}
    added = []
    for item in fetched or []:
        if not item.get("url") or item["url"] in urls:
            continue
        merged.append(item)
        urls.add(item["url"])
        added.append((len(merged) - 1, item))
    return merged, added


def has_national_championship_signal(event):
    text = " ".join(str(event.get(key) or "")
                    for key in ("shortTitle", "title", "slug"))
    return bool(NATIONAL_CHAMPIONSHIP_RE.search(text))


def is_championship_ranking_attachment(attachment):
    """Accept an explicitly named national ranking even when ANNE labels it
    ``other``.  Do not broaden this to every filename containing ÖM/ÖSTM:
    invitations, split times and jury documents use the same event title."""
    text = f"{attachment.get('fileName') or ''} {attachment.get('url') or ''}"
    return bool(CHAMPIONSHIP_RANKING_ATTACHMENT_RE.search(text)
                and not NON_RESULT_ATTACHMENT_RE.search(text))


def is_result_named_attachment(attachment):
    """Recover result files that ANNE classified as ``other``/``splittimes``.

    The attachment type is not reliable in the historic catalog.  A positive
    result filename plus a conservative negative filter is safer than either
    accepting every ``other`` file from a championship event or silently
    missing an official result list.  The downstream parser still classifies
    true cumulative/split reports as non-race data.
    """
    text = f"{attachment.get('fileName') or ''} {attachment.get('url') or ''}"
    return bool(RESULT_NAMED_ATTACHMENT_RE.search(text)
                and not NON_RESULT_ATTACHMENT_RE.search(text))


def sync_attachments(events, known, force=False, refresh_cutoff=None, event_ids=None):
    """Refresh only new/recent/selected attachment indexes and return additions.

    Reading an attachment *index* is a small JSON request; the attachment file
    itself is not fetched here.  Events older than ``refresh_cutoff`` are not
    revisited automatically once present in ``attachments.json``.
    """
    today = date.today().isoformat()
    event_ids = set(event_ids or [])

    def eligible(e):
        eid = int(e["id"])
        is_selected = eid in event_ids
        has_legacy_need = (
            not (e.get("hasOfficialResults") or e.get("hasUnofficialResults"))
            or has_unusable_structured_results(eid)
        )
        has_championship_need = has_national_championship_signal(e)
        needs_index = (
            force
            or str(eid) not in known
            or is_selected
            or (refresh_cutoff and (e.get("dateFrom") or "")[:10] >= refresh_cutoff)
        )
        return (
            (e.get("dateFrom") or "9999")[:10] <= today
            and needs_index
            and (is_selected or has_legacy_need or has_championship_need)
        )

    todo = [e for e in events
            if eligible(e)]
    print(f"attachment indexes to fetch: {len(todo)}")

    def is_club_allowlisted(url):
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "") in CLUB_LINK_ALLOWLIST

    def check(e):
        d = get(f"{BASE}/event/{e['id']}/attachments")
        if not isinstance(d, list):
            return str(e["id"]), []
        # ANNE sometimes mislabels the actual results page on a club-allowlisted
        # domain (e.g. type "splittimes" for a page that's really the results),
        # so accept any attachment there regardless of its assigned type
        res = [a for a in d if (a.get("type") == "results"
                                or is_championship_ranking_attachment(a)
                                or is_result_named_attachment(a)
                                or is_club_allowlisted(a.get("url", "")))]
        return str(e["id"]), [
            {"url": a["url"], "fileName": a["fileName"], "mimeType": a["mimeType"]}
            for a in res
        ]

    additions = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(check, e) for e in todo]):
            eid, files = fut.result()
            merged, added = merge_attachment_index(known.get(eid), files)
            known[eid] = merged
            additions.extend({"eventId": int(eid), "index": index, **item}
                             for index, item in added)

    def add_overrides(overrides, mime_type):
        for eid, values in overrides.items():
            if event_ids and int(eid) not in event_ids:
                continue
            existing = known.get(str(eid)) or []
            incoming = [
                {"url": url, "fileName": filename, "mimeType": mime_type}
                for url, filename in values
            ]
            merged, added = merge_attachment_index(existing, incoming)
            known[str(eid)] = merged
            additions.extend({"eventId": int(eid), "index": index, **item}
                             for index, item in added)

    add_overrides(MANUAL_ATTACHMENT_OVERRIDES, "text/link")
    add_overrides(MANUAL_PDF_OVERRIDES, "application/pdf")
    add_overrides(MANUAL_HTML_OVERRIDES, "text/html")

    (RAW / "attachments.json").write_text(json.dumps(known, ensure_ascii=False))
    additions.sort(key=lambda item: (item["eventId"], item["index"]))
    return additions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch everything")
    ap.add_argument("--event-id", type=int, action="append",
                    help="refresh only this historic event (repeatable)")
    ap.add_argument("--championship-attachments", action="store_true",
                    help="refresh attachment indexes for every past ÖM/ÖSTM event")
    ap.add_argument("--attachments-only", action="store_true",
                    help="skip structured result/stage snapshots")
    ap.add_argument("--delta-file", type=Path,
                    help="write newly discovered attachment keys for incremental parsers")
    args = ap.parse_args()

    (RAW / "results").mkdir(parents=True, exist_ok=True)
    (RAW / "stages").mkdir(parents=True, exist_ok=True)

    events = fetch_events()
    refresh_cutoff = (date.today() - timedelta(days=REFRESH_DAYS)).isoformat()
    today = date.today().isoformat()
    past = [e for e in events if (e.get("dateFrom") or "9999")[:10] <= today]

    selected_ids = set(args.event_id or [])
    selected = [e for e in past if not selected_ids or int(e["id"]) in selected_ids]
    if selected_ids:
        missing = selected_ids - {int(e["id"]) for e in selected}
        if missing:
            ap.error(f"unknown or future event-id(s): {', '.join(map(str, sorted(missing)))}")

    if args.championship_attachments:
        selected = [e for e in past if has_national_championship_signal(e)]
        selected_ids = {int(e["id"]) for e in selected}

    with_results = [e for e in selected
                    if e.get("hasOfficialResults") or e.get("hasUnofficialResults")]
    print(f"past events: {len(past)}, with structured results: {len(with_results)}")

    fetched = 0
    if not args.attachments_only:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(sync_event, e, args.force, refresh_cutoff) for e in with_results]
            for fut in as_completed(futs):
                eid, what = fut.result()
                if what:
                    fetched += 1
                    if fetched % 50 == 0:
                        print(f"  fetched {fetched}", flush=True)
    print(f"result snapshots fetched/updated: {fetched}")

    att_path = RAW / "attachments.json"
    known = json.loads(att_path.read_text()) if att_path.exists() else {}
    additions = sync_attachments(
        selected if selected_ids else events,
        known,
        force=args.force,
        refresh_cutoff=refresh_cutoff,
        event_ids=selected_ids,
    )
    print(f"new attachments discovered: {len(additions)}")
    if args.delta_file:
        args.delta_file.parent.mkdir(parents=True, exist_ok=True)
        args.delta_file.write_text(json.dumps({"attachments": additions}, ensure_ascii=False))
    print("done")


if __name__ == "__main__":
    sys.exit(main())
