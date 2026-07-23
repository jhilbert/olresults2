#!/usr/bin/env python3
"""Summarise normalized parser-output changes against the current git HEAD."""
import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)


def metrics(document):
    categories = document.get("categories") or []
    rows = [row for category in categories for row in category.get("results") or []]
    return {
        "categories": len(categories), "rows": len(rows),
        "ranked": sum(row.get("rank") is not None for row in rows),
        "timed": sum(row.get("timeS") is not None for row in rows),
        "unknown": sum(row.get("status") == "unknown" for row in rows),
        "ooc": sum(bool(row.get("outOfCompetition")) for row in rows),
    }


def load_head(path):
    result = git("show", f"HEAD:{path.as_posix()}")
    if result.returncode:
        return None
    return json.loads(result.stdout)


def changed_paths(paths=None):
    if paths:
        return [Path(path) for path in paths]
    tracked = git("diff", "--name-only", "--", "data/normalized")
    untracked = git(
        "ls-files", "--others", "--exclude-standard", "--", "data/normalized")
    if tracked.returncode or untracked.returncode:
        raise RuntimeError((tracked.stderr or untracked.stderr).strip())
    names = set(tracked.stdout.splitlines()) | set(untracked.stdout.splitlines())
    return [Path(line) for line in sorted(names) if line.endswith(".json")]


def collect(paths=None):
    changes = []
    for relative in changed_paths(paths):
        current_path = ROOT / relative
        before_doc = load_head(relative)
        after_doc = json.loads(current_path.read_text()) if current_path.exists() else None
        before = metrics(before_doc) if before_doc else None
        after = metrics(after_doc) if after_doc else None
        delta = ({key: after[key] - before[key] for key in before}
                 if before is not None and after is not None else None)
        changes.append({"path": relative.as_posix(), "before": before,
                        "after": after, "delta": delta})
    return {"changed_documents": len(changes), "changes": changes}


def render(report):
    lines = [f"Normalized parser diff: {report['changed_documents']} Dokumente"]
    for change in report["changes"]:
        if change["delta"] is None:
            state = "neu" if change["before"] is None else "entfernt"
            lines.append(f"- {change['path']}: {state}")
            continue
        delta = ", ".join(
            f"{key} {value:+d}" for key, value in change["delta"].items() if value)
        lines.append(f"- {change['path']}: {delta or 'nur Feldwerte geändert'}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = collect(args.paths or None)
        output = (json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                  if args.json else render(report))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output)
        else:
            print(output, end="")
        return 0
    except Exception as exc:
        print(f"normalized diff: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
