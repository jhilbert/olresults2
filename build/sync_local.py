#!/usr/bin/env python3
"""Bring public/private source state current and rebuild the local database.

This is the supported one-command local refresh.  It deliberately preserves
local review decisions and refuses unsafe/non-fast-forward Git updates.
"""
import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCAL_ENV = ROOT / ".env.local"
DEFAULT_GATEWAY_URL = "https://olresults-anne-gateway.hilbert.workers.dev"
REVIEW_PATH = ROOT / "data" / "review" / "verification.json"


class SyncError(RuntimeError):
    pass


def read_local_env(path=LOCAL_ENV):
    values = {}
    if not path.exists():
        return values
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SyncError(f"{path.name}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def save_local_gateway_config(url, token, path=LOCAL_ENV):
    if "\n" in token or "\r" in token:
        raise SyncError("gateway token must be one line")
    content = (
        "# Private local OLResults configuration. Never commit this file.\n"
        f"OLRESULTS_GATEWAY_URL={url}\n"
        f"ANNE_GATEWAY_TOKEN={token}\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)
    os.chmod(path, 0o600)


def gateway_environment(prompt=True):
    file_values = read_local_env()
    env = os.environ.copy()
    for key, value in file_values.items():
        env.setdefault(key, value)
    env.setdefault("OLRESULTS_GATEWAY_URL", DEFAULT_GATEWAY_URL)

    if not env.get("ANNE_GATEWAY_TOKEN"):
        if not prompt or not sys.stdin.isatty():
            raise SyncError(
                "ANNE_GATEWAY_TOKEN is missing. Run sync-local.command once in a terminal "
                "to enter and save it securely.")
        print("Einmalige Einrichtung: Cloudflare/GitHub Sync-Gateway-Token eingeben.")
        token = getpass.getpass("ANNE_GATEWAY_TOKEN (Eingabe bleibt unsichtbar): ").strip()
        if not token:
            raise SyncError("no gateway token entered")
        env["ANNE_GATEWAY_TOKEN"] = token
        save_local_gateway_config(env["OLRESULTS_GATEWAY_URL"], token)
        print(f"private Konfiguration gespeichert: {LOCAL_ENV} (Dateirechte 600)")
    return env


def run(command, env=None, capture=False):
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=capture,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip() if capture else ""
        raise SyncError(
            f"command failed ({completed.returncode}): {' '.join(map(str, command))}"
            + (f"\n{detail}" if detail else ""))
    return completed.stdout.strip() if capture else ""


def git_output(*args):
    return run(["git", *args], capture=True)


def git_lines(*args):
    value = git_output(*args)
    return {line for line in value.splitlines() if line}


def is_ancestor(ancestor, descendant):
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode not in (0, 1):
        raise SyncError("could not compare local and origin/main history")
    return completed.returncode == 0


def update_public_checkout():
    print("\n[1/5] Öffentliche Daten aus GitHub aktualisieren")
    run(["git", "fetch", "origin", "main"])
    head = git_output("rev-parse", "HEAD")
    remote = git_output("rev-parse", "origin/main")
    if head == remote:
        print(f"Git bereits aktuell: {head[:8]}")
        return
    if not is_ancestor(head, remote):
        if is_ancestor(remote, head):
            raise SyncError(
                "the current branch contains commits not on main; refusing to replace "
                "that development state automatically")
        raise SyncError("the current branch diverged from origin/main; manual reconciliation required")

    remote_changes = git_lines("diff", "--name-only", "HEAD..origin/main")
    local_changes = (
        git_lines("diff", "--name-only")
        | git_lines("diff", "--cached", "--name-only")
        | git_lines("ls-files", "--others", "--exclude-standard")
    )
    conflicts = sorted(remote_changes & local_changes)
    if conflicts:
        raise SyncError(
            "GitHub update overlaps local changes; nothing was merged:\n- "
            + "\n- ".join(conflicts))
    run(["git", "merge", "--ff-only", "origin/main"])
    print(f"Git Fast-forward: {head[:8]} -> {remote[:8]}")


def review_overlay_summary():
    if not REVIEW_PATH.exists():
        return
    try:
        assertions = json.loads(REVIEW_PATH.read_text()).get("assertions", [])
    except (OSError, json.JSONDecodeError):
        print("Warnung: lokale verification.json ist nicht lesbar")
        return
    dirty = bool(git_output("status", "--porcelain", "--", str(REVIEW_PATH.relative_to(ROOT))))
    if dirty:
        print(
            f"Lokaler Prüf-Overlay bleibt erhalten: {len(assertions)} Entscheidungen "
            "(nicht auf GitHub).")


def sync_local(env, update_git=True):
    if update_git:
        update_public_checkout()
    else:
        print("\n[1/5] Git-Aktualisierung ausdrücklich übersprungen")

    print("\n[2/5] Eligibility-Ledger aus Cloudflare R2 holen")
    run([sys.executable, "ingest/eligibility_state.py", "pull", "--required"], env=env)

    print("\n[3/5] Privaten ANNE-Personenindex aus Cloudflare R2 holen")
    run([sys.executable, "ingest/identity_state.py", "pull", "--required"], env=env)

    print("\n[4/5] Lokale SQLite-Datenbank bauen")
    review_overlay_summary()
    run([sys.executable, "build/build_db.py"], env=env)

    print("\n[5/5] Lokale SQLite-Datenbank validieren")
    run([sys.executable, "build/validate_db.py"], env=env)
    print("\nLokaler OLResults-Stand ist synchronisiert und validiert.")
    print("Prüftool wie gewohnt mit: python3 site/serve.py")


def main(argv=()):
    parser = argparse.ArgumentParser(description="Synchronize and rebuild the local OLResults DB")
    parser.add_argument(
        "--no-git-update", action="store_true",
        help="use the current checkout instead of fetching/fast-forwarding origin/main")
    parser.add_argument(
        "--no-prompt", action="store_true",
        help="fail instead of prompting when the local gateway token is missing")
    args = parser.parse_args(argv)
    try:
        env = gateway_environment(prompt=not args.no_prompt)
        sync_local(env, update_git=not args.no_git_update)
        return 0
    except (SyncError, OSError, json.JSONDecodeError) as exc:
        print(f"\nLokale Synchronisierung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
