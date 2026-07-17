#!/usr/bin/env python3
"""Validate a built database before it is committed or published.

The committed baseline is deliberately small: it records counts, not private
rows.  A normal sync may grow them, but an unexplained large decrease stops the
pipeline before a partial scrape can replace the public database.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "site" / "data" / "results.db"
DEFAULT_BASELINE = ROOT / "data" / "build_health.json"
CORE_COUNTS = ("event", "stage", "person", "result")
FINGERPRINT_ORDER = {
    "event": "id",
    "stage": "id",
    "person": "id",
    "person_identifier": "scheme, identifier, person_id",
    "person_alias": "person_id, name, source",
    "person_redirect": "old_id",
    "source_document": "id",
    "result": "id",
}


def scalar(con, sql, params=()):
    return con.execute(sql, params).fetchone()[0]


def state_counts(path):
    if not path.exists():
        raise RuntimeError(f"eligibility state is missing: {path}")
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise RuntimeError("eligibility state is not an object")
    return {
        "people": len(state),
        "decisions": sum(len(events) for events in state.values()),
    }


def logical_fingerprint(con):
    """Hash logical rows in canonical order, independent of SQLite page layout."""
    digest = hashlib.sha256()
    for table, order_by in FINGERPRINT_ORDER.items():
        digest.update(f"table:{table}\n".encode())
        cursor = con.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
        for row in cursor:
            digest.update(json.dumps(
                row, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def collect(db_path, eligibility_path):
    if not db_path.exists():
        raise RuntimeError(f"database is missing: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = scalar(con, "PRAGMA integrity_check")
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"SQLite foreign_key_check found {len(fk_errors)} errors")
        counts = {table: scalar(con, f"SELECT COUNT(*) FROM {table}") for table in CORE_COUNTS}
        for table in ("source_document", "person_identifier", "person_alias", "person_redirect"):
            counts[table] = scalar(con, f"SELECT COUNT(*) FROM {table}")
        missing_provenance = scalar(
            con, "SELECT COUNT(*) FROM result WHERE source_document_id IS NULL")
        if missing_provenance:
            raise RuntimeError(f"{missing_provenance} result rows have no source document")
        missing_identity_basis = scalar(
            con, "SELECT COUNT(*) FROM result WHERE identity_basis = 'unknown'")
        if missing_identity_basis:
            raise RuntimeError(f"{missing_identity_basis} result rows have no identity basis")
        identifier_conflicts = scalar(
            con,
            """SELECT COUNT(*) FROM (
                   SELECT scheme, identifier FROM person_identifier
                   GROUP BY scheme, identifier HAVING COUNT(DISTINCT person_id) > 1
               )""")
        by_source = dict(con.execute(
            "SELECT source, COUNT(*) FROM result GROUP BY source ORDER BY source"))
        by_kind = dict(con.execute(
            "SELECT result_kind, COUNT(*) FROM result GROUP BY result_kind ORDER BY result_kind"))
        counts["championship_result"] = scalar(
            con, "SELECT COUNT(*) FROM result WHERE championship IS NOT NULL")
        return {
            "schema_version": 1,
            "logical_sha256": logical_fingerprint(con),
            "counts": counts,
            "result_by_source": by_source,
            "result_by_kind": by_kind,
            "eligibility": state_counts(eligibility_path),
            "quality": {
                "results_without_provenance": missing_provenance,
                "results_without_identity_basis": missing_identity_basis,
                "identifier_conflicts": identifier_conflicts,
            },
        }
    finally:
        con.close()


def validate_against_baseline(current, baseline, max_drop):
    errors = []
    for name in CORE_COUNTS:
        old = baseline.get("counts", {}).get(name)
        new = current["counts"][name]
        if old and new < old * (1 - max_drop):
            errors.append(
                f"{name} dropped from {old} to {new} (more than {max_drop:.1%})")
    for name in ("people", "decisions"):
        old = baseline.get("eligibility", {}).get(name)
        new = current["eligibility"][name]
        if old is not None and new < old:
            errors.append(f"eligibility {name} shrank from {old} to {new}")
    old_conflicts = baseline.get("quality", {}).get("identifier_conflicts")
    new_conflicts = current["quality"]["identifier_conflicts"]
    if old_conflicts is not None and new_conflicts > old_conflicts:
        errors.append(
            f"external identifier conflicts increased from {old_conflicts} to {new_conflicts}")
    if errors:
        raise RuntimeError("build health regression:\n- " + "\n- ".join(errors))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--eligibility", type=Path,
                        default=ROOT / "data" / "raw" / "anne" / "user_eligibility.json")
    parser.add_argument("--max-drop", type=float, default=0.02)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    try:
        current = collect(args.db, args.eligibility)
        if args.baseline.exists():
            validate_against_baseline(current, json.loads(args.baseline.read_text()), args.max_drop)
        elif not args.write_baseline:
            raise RuntimeError(f"build baseline is missing: {args.baseline}")
        if args.report:
            write_json(args.report, current)
        if args.write_baseline:
            write_json(args.baseline, current)
        print(json.dumps(current, indent=2, sort_keys=True))
        print("database health: ok")
        return 0
    except Exception as exc:
        print(f"database health: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
