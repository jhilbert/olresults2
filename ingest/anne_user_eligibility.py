#!/usr/bin/env python3
"""Fetch ANNE's authoritative championshipEligibility flag for every runner
on record with a non-Austrian nationality, via the authenticated
GET /v1/user/:id endpoint (requires ANNE_API_KEY - a personal API key from
an ANNE account with clubManager+ role). Caches to
data/raw/anne/user_eligibility.json, keyed by (ANNE userId, event id).

Why per (person, event) rather than just per person: eligibility isn't a
permanent attribute - someone can gain or lose it at any point in their
career, so a single global flag per person would eventually go stale one
way or the other. Instead, a person's eligibility is checked once, the
first time a given event of theirs is seen, and that determination is
locked to that specific event forever - a later status change only affects
NEW events synced from then on, never rewrites an event already decided.
This is also why the cache must never be recomputed wholesale: only
genuinely new (userId, eventId) pairs are ever fetched.

For the backlog of events already in the database before this feature
existed, there's no way to know what was true back when each of them
happened - those get a one-time "current status" check instead, same as
any newly-discovered pair. Everything synced from here on gets its own
independently-locked, close-to-real-time determination.

Why this exists: person.nationality alone is not a reliable ÖM/ÖSTM
eligibility signal - it reflects passport/birth nationality, not
competition eligibility, and several long-tenured Austrian club members are
on record with a foreign nationality yet hold an explicit
championshipEligibility override (confirmed by hand: Vera Arbter/CHE,
Marina Skern/RUS, Frederic Genevois/FRA, all real ÖM medalists per their
club's own records). See build_db.py's use of this cache.

Candidates are read from the already-BUILT site/data/results.db, not raw
ANNE snapshots directly: person.nationality is only embedded in ANNE's own
structured API results, but the same person's OTHER results (from a scraped
SportSoftware PDF/HTML/text export, the majority of this dataset) carry no
such field at all - only build_db.py's person-identity resolution links
those legacy rows to the same ANNE userId. Querying raw JSON directly would
therefore only ever check a foreign person's ANNE-API-tier events and
silently miss every legacy-tier event of theirs. This is why this script
must run in a build -> check eligibility -> build again cycle: the first
build resolves identities and championship tags so this script has
something to query, the second build applies whatever new decisions this
script just locked in.
"""
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
OUT_PATH = RAW / "user_eligibility.json"
DB_PATH = ROOT / "site" / "data" / "results.db"
BASE = os.environ.get("ANNE_BASE_URL", "https://anne-api.oefol.at/v1").rstrip("/")
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def scan_foreign_candidates():
    """Every distinct (personId, eventId) pair in the built database where
    the runner has a championship tag and ANNE reports a non-Austrian
    nationality for them - the only people championshipEligibility could
    possibly matter for. Everyone else is either Austrian already or has no
    ANNE account at all (a synthetic person id), in which case this API has
    nothing to tell us."""
    if not DB_PATH.exists():
        return set()
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute("""
            SELECT DISTINCT p.id, s.event_id
            FROM result r
            JOIN stage s ON s.id = r.stage_id
            JOIN person p ON p.id = r.person_id
            WHERE r.championship IS NOT NULL AND p.id > 0
              AND p.nationality IS NOT NULL AND p.nationality NOT IN ('', 'AUT')
        """).fetchall()
    finally:
        con.close()
    return {(pid, str(eid)) for pid, eid in rows}


def fetch_eligibility(user_id, api_key=None, gateway_token=None):
    if gateway_token:
        gateway_root = BASE[:-3] if BASE.endswith("/v1") else BASE
        url = f"{gateway_root}/eligibility/{user_id}"
        headers = {"Authorization": f"Bearer {gateway_token}", "Accept": "application/json",
                   "User-Agent": "olresults-sync/1.0 (+https://github.com/jhilbert/olresults2)"}
    else:
        url = f"{BASE}/user/{user_id}"
        headers = {"X-API-Key": api_key, "Accept": "application/json",
                   "User-Agent": "olresults-sync/1.0 (+https://github.com/jhilbert/olresults2)"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as response:
            d = json.load(response)
    except Exception:
        return "error"
    if "championshipEligibility" not in d:
        return "error"
    return d.get("championshipEligibility")


def main():
    force = "--force" in sys.argv
    api_key = os.environ.get("ANNE_API_KEY")
    gateway_token = os.environ.get("ANNE_GATEWAY_TOKEN")
    if not api_key and not gateway_token:
        print("ANNE_API_KEY/ANNE_GATEWAY_TOKEN not set - skipping "
              "(existing cache, if any, is left as-is)")
        return

    if not DB_PATH.exists():
        print(f"{DB_PATH} doesn't exist yet - run build/build_db.py first, then this "
              "script, then build/build_db.py again to apply what it found")
        return

    cache = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    candidates = scan_foreign_candidates()
    # "error" entries (a transient API/network failure) are always retried;
    # a real True/null result is a locked-in decision, only ever revisited
    # with --force (a deliberate, explicit re-check - not something the
    # nightly sync does on its own)
    todo = [(uid, eid) for uid, eid in candidates
            if force or eid not in cache.get(str(uid), {})
            or cache[str(uid)][eid] == "error"]
    print(f"foreign-nationality (person, event) pairs on record: {len(candidates)}, "
          f"to fetch: {len(todo)}")

    fetched = {}
    for uid, eid in todo:
        if uid not in fetched:
            fetched[uid] = fetch_eligibility(uid, api_key, gateway_token)
            time.sleep(0.1)
        cache.setdefault(str(uid), {})[eid] = fetched[uid]

    OUT_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))
    n_pairs = sum(len(v) for v in cache.values())
    n_true = sum(1 for v in cache.values() for e in v.values() if e is True)
    print(f"wrote {OUT_PATH} ({len(cache)} people, {n_pairs} (person, event) decisions, "
          f"{n_true} with an eligibility override)")


if __name__ == "__main__":
    main()
