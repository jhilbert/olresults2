#!/usr/bin/env python3
"""Pull/push the private championship-eligibility state via the ANNE gateway.

The published repository never contains this file.  In CI, a missing remote
state is a hard error: silently building without it changes historical medal
decisions.  The one-time initial migration is performed with ``push`` from a
trusted machine that already has data/raw/anne/user_eligibility.json.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "raw" / "anne" / "user_eligibility.json"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def config():
    base = os.environ.get("OLRESULTS_GATEWAY_URL", "").rstrip("/")
    token = os.environ.get("ANNE_GATEWAY_TOKEN", "")
    if not base or not token:
        raise RuntimeError("OLRESULTS_GATEWAY_URL and ANNE_GATEWAY_TOKEN must be set")
    return f"{base}/state/eligibility", token


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("eligibility state must be an object")
    for user_id, by_event in value.items():
        if not str(user_id).isdigit() or not isinstance(by_event, dict):
            raise ValueError(f"invalid user entry: {user_id!r}")
        for event_id, eligibility in by_event.items():
            if not str(event_id).isdigit() or eligibility not in (True, None, "error"):
                raise ValueError(f"invalid eligibility entry: {user_id}/{event_id}")
    return value


def request(method, body=None):
    url, token = config()
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "olresults-sync/1.0 (+https://github.com/jhilbert/olresults2)",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as response:
        return response.read()


def summarize(state):
    pairs = sum(len(by_event) for by_event in state.values())
    return f"{len(state)} people, {pairs} person/event decisions"


def pull(required=False):
    try:
        raw = request("GET")
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and not required:
            print("remote eligibility state is not initialized")
            return False
        raise
    state = validate(json.loads(raw))
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(STATE_PATH)
    print(f"restored {STATE_PATH} ({summarize(state)})")
    return True


def push():
    if not STATE_PATH.exists():
        raise FileNotFoundError(f"local eligibility state missing: {STATE_PATH}")
    state = validate(json.loads(STATE_PATH.read_text()))
    body = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    response = json.loads(request("PUT", body))
    if not response.get("ok"):
        raise RuntimeError(f"gateway rejected state: {response}")
    print(f"saved {STATE_PATH} ({summarize(state)})")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    pull_parser = sub.add_parser("pull")
    pull_parser.add_argument("--required", action="store_true")
    sub.add_parser("push")
    args = parser.parse_args()
    try:
        if args.command == "pull":
            pull(args.required)
        else:
            push()
    except Exception as exc:
        print(f"eligibility state {args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
