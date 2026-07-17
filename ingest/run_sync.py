#!/usr/bin/env python3
"""Run source adapters in the required order and stop on the first failure."""
import subprocess
import sys
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


def main():
    for command in COMMANDS:
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
    sys.exit(main())
