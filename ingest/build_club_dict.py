#!/usr/bin/env python3
"""Build data/clubs.json: a set of known club names used to split runner name
from club in flowing-layout PDFs (e.g. night-O run in pairs, where the two
names and the club run together with no fixed column).

Sources: the canonical ANNE club list (/v1/club) plus every distinct clubName
seen in structured API results (broad coverage of legacy spellings and foreign
clubs). Names are lightly cleaned; very short or punctuation-only ones dropped.

Also writes data/official_clubs.json - just the /v1/club registry itself
(type == "club" only, not the regional sub-federations also on that
endpoint), with no legacy-spelling noise mixed in. build_db.py uses this
one, not clubs.json, to canonicalize the Vereine feature's club identity -
the site's "club" shown on an individual result is left exactly as the
source spelled it (some events genuinely used non-official names), but the
Vereine section needs one unambiguous name per real club."""
import json
import os
import re
import ssl
import time
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "anne"
OUT = ROOT / "data" / "clubs.json"
OFFICIAL_OUT = ROOT / "data" / "official_clubs.json"
HEADERS = {"Accept": "application/json",
           "User-Agent": "olresults-sync/0.1 (+https://github.com/josefhilbert/olresults)"}
BASE = os.environ.get("ANNE_BASE_URL", "https://anne-api.oefol.at/v1").rstrip("/")
if os.environ.get("ANNE_GATEWAY_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {os.environ['ANNE_GATEWAY_TOKEN']}"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def get(url):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers=HEADERS), timeout=30, context=SSL_CONTEXT))


def clean(name):
    name = re.sub(r"^[\s.\-]+", "", (name or "").strip())  # leading dots/dashes
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def main():
    clubs = set()
    official = []

    # canonical registered clubs
    try:
        page = 1
        while True:
            d = get(f"{BASE}/club?perPage=200&page={page}")
            data = d if isinstance(d, list) else d.get("data", [])
            for c in data:
                clubs.add(clean(c.get("name")))
                if c.get("type") == "club":
                    official.append({"code": c.get("code"), "name": clean(c.get("name"))})
            meta = d.get("meta") if isinstance(d, dict) else None
            if not meta or page >= meta.get("lastPage", 1):
                break
            page += 1
            time.sleep(0.2)
    except Exception as e:
        print("warning: /v1/club fetch failed:", e)

    if official:
        official.sort(key=lambda c: c["name"])
        OFFICIAL_OUT.write_text(json.dumps(official, ensure_ascii=False))
        print(f"wrote {OFFICIAL_OUT} ({len(official)} official clubs)")

    # every clubName seen in structured results
    for f in (RAW / "results").glob("*.json"):
        for r in json.loads(f.read_text()):
            clubs.add(clean(r.get("clubName")))
            for m in (r.get("teamMembers") or []):
                pass  # members don't carry club separately

    # common "no club" markers so they're recognised as a club token too
    clubs.update({"Vereinslos", "kein Verein", "ohne Verein"})

    # keep names that carry a real alphabetic token and aren't trivially short
    result = sorted(c for c in clubs
                    if c and len(c) >= 3 and re.search(r"[A-Za-zÀ-ÿ]{2,}", c))
    OUT.write_text(json.dumps(result, ensure_ascii=False))
    print(f"wrote {OUT} ({len(result)} clubs)")


if __name__ == "__main__":
    main()
