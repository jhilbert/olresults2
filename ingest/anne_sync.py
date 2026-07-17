#!/usr/bin/env python3
"""Sync events, structured results, stages and attachment indexes from the ANNE API.

Idempotent: existing snapshots are kept unless --force is given or the event
is recent (events within REFRESH_DAYS of today are re-fetched, since results
can still be corrected after publication).
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


def sync_attachments(events, known, force):
    """Fetch attachment indexes for past events not yet in attachments.json,
    plus events whose structured API results turned out to be unusable."""
    today = date.today().isoformat()
    todo = [e for e in events
            if (e.get("dateFrom") or "9999")[:10] <= today
            and (force or str(e["id"]) not in known)
            and (not (e.get("hasOfficialResults") or e.get("hasUnofficialResults"))
                 or has_unusable_structured_results(e["id"]))]
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
        res = [a for a in d if a.get("type") == "results" or is_club_allowlisted(a.get("url", ""))]
        return str(e["id"]), [
            {"url": a["url"], "fileName": a["fileName"], "mimeType": a["mimeType"]}
            for a in res
        ]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(check, e) for e in todo]):
            eid, files = fut.result()
            known[eid] = files

    for eid, overrides in MANUAL_ATTACHMENT_OVERRIDES.items():
        existing = known.get(str(eid)) or []
        urls = {a["url"] for a in existing}
        for url, filename in overrides:
            if url not in urls:
                existing.append({"url": url, "fileName": filename, "mimeType": "text/link"})
        known[str(eid)] = existing

    for eid, overrides in MANUAL_PDF_OVERRIDES.items():
        existing = known.get(str(eid)) or []
        urls = {a["url"] for a in existing}
        for url, filename in overrides:
            if url not in urls:
                existing.append({"url": url, "fileName": filename, "mimeType": "application/pdf"})
        known[str(eid)] = existing

    for eid, overrides in MANUAL_HTML_OVERRIDES.items():
        existing = known.get(str(eid)) or []
        urls = {a["url"] for a in existing}
        for url, filename in overrides:
            if url not in urls:
                existing.append({"url": url, "fileName": filename, "mimeType": "text/html"})
        known[str(eid)] = existing

    (RAW / "attachments.json").write_text(json.dumps(known, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch everything")
    args = ap.parse_args()

    (RAW / "results").mkdir(parents=True, exist_ok=True)
    (RAW / "stages").mkdir(parents=True, exist_ok=True)

    events = fetch_events()
    refresh_cutoff = (date.today() - timedelta(days=REFRESH_DAYS)).isoformat()
    today = date.today().isoformat()
    past = [e for e in events if (e.get("dateFrom") or "9999")[:10] <= today]

    with_results = [e for e in past
                    if e.get("hasOfficialResults") or e.get("hasUnofficialResults")]
    print(f"past events: {len(past)}, with structured results: {len(with_results)}")

    fetched = 0
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
    sync_attachments(events, known, force=args.force)
    print("done")


if __name__ == "__main__":
    sys.exit(main())
