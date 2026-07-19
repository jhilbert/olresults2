#!/usr/bin/env python3
"""Run source adapters incrementally, or reparse one explicitly selected event."""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMMANDS = [
    [sys.executable, "ingest/anne_sync.py"],
    [sys.executable, "ingest/build_club_dict.py"],
    [sys.executable, "ingest/parse_sportsoftware_html.py"],
    [sys.executable, "ingest/parse_sportsoftware_pdf.py"],
    [sys.executable, "ingest/parse_sportsoftware_text.py"],
    [sys.executable, "ingest/parse_club_table.py"],
    [sys.executable, "ingest/parse_liveresultat.py"],
]

ATTACHMENT_PARSERS = {
    "parse_sportsoftware_html.py",
    "parse_sportsoftware_pdf.py",
    "parse_sportsoftware_text.py",
    "parse_club_table.py",
}


def command_for(command, delta_file, event_id=None, refresh_source=False):
    result = list(command)
    script = Path(command[1]).name if len(command) > 1 else ""
    if script == "anne_sync.py":
        result.extend(["--delta-file", str(delta_file)])
        if event_id is not None:
            result.extend(["--event-id", str(event_id)])
    elif script in ATTACHMENT_PARSERS:
        if event_id is not None:
            result.extend(["--event-id", str(event_id)])
            if refresh_source:
                result.append("--force-download")
        else:
            result.extend(["--attachment-manifest", str(delta_file)])
    elif script == "parse_liveresultat.py" and event_id is not None:
        result.extend(["--event-id", str(event_id)])
        if refresh_source:
            result.append("--force-download")
    return result


def main(argv=()):
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", type=int,
                    help="reparse all known result attachments for one historic ANNE event")
    ap.add_argument("--refresh-source", action="store_true",
                    help="also re-download that event's attachment files, bypassing the cache")
    args = ap.parse_args(argv)
    if args.refresh_source and args.event_id is None:
        ap.error("--refresh-source requires --event-id to prevent a full historic download")

    with tempfile.TemporaryDirectory(prefix="olresults-sync-") as tmp:
        delta_file = Path(tmp) / "attachment-delta.json"
        for base_command in COMMANDS:
            command = command_for(
                base_command,
                delta_file,
                event_id=args.event_id,
                refresh_source=args.refresh_source,
            )
            print(f"==> {' '.join(command[1:])}", flush=True)
            completed = subprocess.run(command, cwd=ROOT)
            if completed.returncode:
                print(
                    f"source sync stopped: {command[1]} exited with {completed.returncode}",
                    file=sys.stderr,
                )
                return completed.returncode
    print("source sync completed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
