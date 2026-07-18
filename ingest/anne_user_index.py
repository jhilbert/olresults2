#!/usr/bin/env python3
"""Synchronise ANNE's paginated /user registry into one private snapshot.

The snapshot deliberately contains only identity-resolution data: ÖFOL-ID,
name, birth year, ANNE's raw verification bit, nationality, gender and the
currently reported memberships.  It is ignored by Git and is never copied to
the public Pages database wholesale.  The database builder consumes it only
when a person actually occurs in an imported result.

Unlike championship eligibility, this is a registry snapshot rather than an
event decision.  A fresh snapshot may change names or current memberships, so
the stored ``fetched_at`` timestamp is part of the evidence and consumers must
never rewrite a historic result's observed club from it.
"""
import argparse
import datetime as dt
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "raw" / "anne" / "user_index.json"
BASE = os.environ.get("ANNE_BASE_URL", "https://anne-api.oefol.at/v1").rstrip("/")
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "olresults-identity-sync/1.0 (+https://github.com/jhilbert/olresults2)"


def request_headers():
    gateway_token = os.environ.get("ANNE_GATEWAY_TOKEN")
    api_key = os.environ.get("ANNE_API_KEY")
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if gateway_token:
        headers["Authorization"] = f"Bearer {gateway_token}"
    elif api_key:
        headers["X-API-Key"] = api_key
    else:
        raise RuntimeError("ANNE_API_KEY or ANNE_GATEWAY_TOKEN is required")
    return headers


def get_json(url):
    request = urllib.request.Request(url, headers=request_headers())
    with urllib.request.urlopen(request, timeout=45, context=SSL_CONTEXT) as response:
        return json.load(response)


def clean_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def clean_year(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 1800 <= value <= 2100 else None


def normalise_club(value):
    if isinstance(value, dict):
        return {
            "name": clean_text(value.get("name")),
            "code": clean_text(value.get("code")),
        }
    return {"name": clean_text(value), "code": None}


def normalise_memberships(value):
    memberships = []
    for membership in value or []:
        if not isinstance(membership, dict):
            continue
        club = normalise_club(membership.get("club"))
        if not club["name"] and not club["code"]:
            continue
        memberships.append({
            "club": club,
            "sport_type": clean_text(membership.get("sportType")),
            "date_from": clean_text(membership.get("dateFrom")),
            "date_to": clean_text(membership.get("dateTo")),
            "active": bool(membership.get("active")),
        })
    return memberships


def normalise_user(row):
    if not isinstance(row, dict):
        return None
    try:
        oefol_id = int(row.get("id"))
    except (TypeError, ValueError):
        return None
    if oefol_id <= 0:
        return None
    return {
        "oefol_id": oefol_id,
        "first_name": clean_text(row.get("firstName")),
        "last_name": clean_text(row.get("lastName")),
        "year_of_birth": clean_year(row.get("yearOfBirth")),
        "gender": clean_text(row.get("gender")),
        "nationality": clean_text(row.get("nationality")),
        "anne_is_verified": bool(row.get("isVerified")),
        "active_memberships": normalise_memberships(row.get("activeMemberships")),
    }


def parse_fetched_at(snapshot):
    try:
        return dt.datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None


def is_fresh(max_age_hours):
    if not OUT_PATH.exists():
        return False
    try:
        snapshot = json.loads(OUT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    fetched_at = parse_fetched_at(snapshot)
    if not fetched_at:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    return now - fetched_at <= dt.timedelta(hours=max_age_hours)


def fetch_all_users():
    page = 1
    per_page = 100
    users = {}
    total = None
    while True:
        payload = get_json(f"{BASE}/user?perPage={per_page}&page={page}")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"unexpected /user response on page {page}")
        meta = payload.get("meta") or {}
        total = meta.get("total", total)
        for row in payload["data"]:
            user = normalise_user(row)
            if user:
                users[user["oefol_id"]] = user
        last_page = meta.get("lastPage")
        if not isinstance(last_page, int) or last_page < page:
            raise RuntimeError(f"missing or invalid /user pagination metadata on page {page}")
        print(f"fetched ANNE user page {page}/{last_page} ({len(users)} usable records)")
        if page >= last_page:
            break
        page += 1
        time.sleep(0.08)
    return users, total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-hours", type=float,
                        help="skip when the local private snapshot is newer")
    args = parser.parse_args()
    if args.max_age_hours is not None and is_fresh(args.max_age_hours):
        print(f"ANNE user index is newer than {args.max_age_hours:g} hours; skipping")
        return 0

    users, reported_total = fetch_all_users()
    if reported_total is not None and len(users) > reported_total:
        raise RuntimeError("normalised user count exceeds ANNE's reported total")
    snapshot = {
        "schema_version": 1,
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "anne-api:/v1/user",
        "source_total": reported_total,
        "users": [users[user_id] for user_id in sorted(users)],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUT_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    tmp_path.replace(OUT_PATH)
    print(f"wrote {OUT_PATH} ({len(users)} users; ANNE reported {reported_total})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ANNE user index sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
