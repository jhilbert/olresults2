#!/usr/bin/env python3
"""Pull/push the private ANNE person-index snapshot via the gateway.

The snapshot is deliberately outside Git.  R2 provides the durable encrypted
server-side copy used by local builds and GitHub Actions; the public Pages
artifact receives only identity information derived for actual result rows.
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
STATE_PATH = ROOT / "data" / "raw" / "anne" / "user_index.json"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
MAX_STATE_BYTES = 20 * 1024 * 1024


def config():
    base = os.environ.get("OLRESULTS_GATEWAY_URL", "").rstrip("/")
    token = os.environ.get("ANNE_GATEWAY_TOKEN", "")
    if not base or not token:
        raise RuntimeError("OLRESULTS_GATEWAY_URL and ANNE_GATEWAY_TOKEN must be set")
    return base, token


def validate(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("identity state has an unsupported schema")
    if not isinstance(value.get("fetched_at"), str) or not isinstance(value.get("users"), list):
        raise ValueError("identity state is missing fetched_at or users")
    seen = set()
    for user in value["users"]:
        if not isinstance(user, dict) or not isinstance(user.get("oefol_id"), int):
            raise ValueError("identity state has an invalid user")
        if user["oefol_id"] <= 0 or user["oefol_id"] in seen:
            raise ValueError("identity state has duplicate or invalid ÖFOL IDs")
        seen.add(user["oefol_id"])
    return value


def request(method, path="", body=None):
    base, token = config()
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}/state/identity{path}", data=body,
                                 method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=90, context=SSL_CONTEXT) as response:
        return response.read()


def pull(required):
    try:
        raw = request("GET")
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and not required:
            print("remote ANNE identity state is not initialized")
            return False
        raise
    state = validate(json.loads(raw))
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    print(f"restored ANNE identity state ({len(state['users'])} users; fetched {state['fetched_at']})")
    return True


def push():
    if not STATE_PATH.exists():
        raise FileNotFoundError(f"local identity state missing: {STATE_PATH}")
    state = validate(json.loads(STATE_PATH.read_text()))
    body = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > MAX_STATE_BYTES:
        raise ValueError("identity state exceeds the gateway size limit")
    response = json.loads(request("PUT", body=body))
    if not response.get("ok"):
        raise RuntimeError(f"gateway rejected identity state: {response}")
    print(f"stored ANNE identity state ({response['people']} users; sha256 {response['sha256']})")


def history():
    response = json.loads(request("GET", "/history"))
    for version in response.get("versions", []):
        print(f"{version['key']}  users={version.get('people', '?')}  {version.get('savedAt', '')}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("pull", "push", "history"))
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    if args.command == "pull":
        pull(args.required)
    elif args.command == "push":
        push()
    else:
        history()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"identity state {sys.argv[1] if len(sys.argv) > 1 else ''} failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
