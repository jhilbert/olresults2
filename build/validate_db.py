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
    "person_club_membership": "person_id, club, sport_type, valid_from, source",
    "person_alias": "person_id, name, source",
    "person_redirect": "old_id",
    "person_tombstone": "old_id",
    "source_document": "id",
    "championship_source_entry": "id",
    "result_list": "id",
    "result": "id",
    "audit_issue": "id",
    "verification_assertion": "scope_type, scope_key, dimension",
    "championship_rule_set": "id",
    "championship_jurisdiction": "code",
    "club_jurisdiction": "club",
    "championship_instance": "id",
    "regional_category_mapping": "id",
    "championship_entry": "id",
    "championship_entry_result": "championship_entry_id, result_id",
    "award": "id",
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
        for table in ("source_document", "championship_source_entry",
                      "person_identifier", "person_club_membership", "person_alias", "person_redirect",
                      "person_tombstone", "result_list", "audit_issue", "verification_assertion"):
            counts[table] = scalar(con, f"SELECT COUNT(*) FROM {table}")
        for table in ("championship_rule_set", "championship_jurisdiction",
                      "club_jurisdiction", "championship_instance",
                      "regional_category_mapping", "championship_entry",
                      "championship_entry_result", "award"):
            counts[table] = scalar(con, f"SELECT COUNT(*) FROM {table}")
        missing_provenance = scalar(
            con, "SELECT COUNT(*) FROM result WHERE source_document_id IS NULL")
        if missing_provenance:
            raise RuntimeError(f"{missing_provenance} result rows have no source document")
        missing_identity_basis = scalar(
            con, "SELECT COUNT(*) FROM result WHERE identity_basis = 'unknown'")
        if missing_identity_basis:
            raise RuntimeError(f"{missing_identity_basis} result rows have no identity basis")
        missing_list = scalar(con, "SELECT COUNT(*) FROM result WHERE result_list_id IS NULL")
        if missing_list:
            raise RuntimeError(f"{missing_list} result rows have no result-list review unit")
        illegal_status = scalar(
            con, """SELECT COUNT(*) FROM result
                    WHERE status NOT IN ('ok','dns','dnf','mp','dsq','unknown')""")
        if illegal_status:
            raise RuntimeError(f"{illegal_status} results use a non-normalized status")
        negative_elapsed_time = scalar(
            con, """SELECT COUNT(*) FROM result
                    WHERE time_s < 0 OR team_time_s < 0""")
        if negative_elapsed_time:
            raise RuntimeError(
                f"{negative_elapsed_time} results store a negative elapsed-time sentinel")
        illegal_eligibility = scalar(
            con, """SELECT COUNT(*) FROM result
                    WHERE championship_eligibility_state NOT IN
                          ('eligible','ineligible','provisional','unknown')""")
        if illegal_eligibility:
            raise RuntimeError(
                f"{illegal_eligibility} results use an invalid eligibility state")
        unmatched_explicit_championship = scalar(
            con, """SELECT COUNT(*) FROM championship_source_entry
                    WHERE evidence_kind = 'official_championship_inclusion'
                      AND result_id IS NULL""")
        if unmatched_explicit_championship:
            raise RuntimeError(
                f"{unmatched_explicit_championship} official championship ranking rows "
                "did not match a result")
        family_identity = scalar(
            con, """SELECT COUNT(*) FROM result WHERE result_kind = 'family'
                    AND (person_id IS NOT NULL OR identity_state != 'not_applicable'
                         OR championship IS NOT NULL)""")
        if family_identity:
            raise RuntimeError(f"{family_identity} Family results leak into person/championship data")
        personless_ordinary = scalar(
            con, """SELECT COUNT(*) FROM result
                    WHERE person_id IS NULL AND result_kind != 'family'
                      AND NOT (result_kind IN ('pair','relay','team')
                               AND identity_state = 'not_applicable'
                               AND identity_basis IN (
                                   'not-applicable-memberless-team',
                                   'not-applicable-relay-placeholder')
                               AND team_name IS NOT NULL
                               AND championship IS NULL)""")
        personless_ordinary -= scalar(
            con, """SELECT COUNT(*) FROM result
                    WHERE person_id IS NULL AND result_kind IN ('individual', 'pair')
                      AND identity_state = 'not_applicable'
                      AND identity_basis =
                          'not-applicable-unidentified-source'
                      AND championship IS NULL""")
        if personless_ordinary:
            raise RuntimeError(
                f"{personless_ordinary} ordinary results have no person mapping")
        ooc_championship = scalar(
            con, "SELECT COUNT(*) FROM result WHERE out_of_competition = 1 AND championship IS NOT NULL")
        if ooc_championship:
            raise RuntimeError(f"{ooc_championship} OOC results still carry championship eligibility")
        duplicate_relay_awards = scalar(
            con,
            """SELECT COUNT(*) FROM (
                   SELECT a.championship_instance_id, r.person_id,
                          COALESCE('n:' || r.team_number,
                                   't:' || r.team_name, 'c:' || r.club) AS team_key
                   FROM award a JOIN result r ON r.id = a.result_id
                   WHERE r.result_kind = 'relay'
                   GROUP BY a.championship_instance_id, r.person_id, team_key
                   HAVING COUNT(*) > 1)""")
        if duplicate_relay_awards:
            raise RuntimeError(
                f"{duplicate_relay_awards} relay medal groups count one person more than once")
        regional_double_assignment = scalar(
            con,
            """SELECT COUNT(*) FROM (
                   SELECT ce.stage_id, ce.competitor_key
                     FROM championship_entry ce
                     JOIN championship_instance ci ON ci.id = ce.championship_instance_id
                    WHERE ci.championship_type = 'LMS'
                    GROUP BY ce.stage_id, ce.competitor_key
                   HAVING COUNT(DISTINCT ci.jurisdiction) > 1)""")
        if regional_double_assignment:
            raise RuntimeError(
                f"{regional_double_assignment} performances belong to multiple state championships")
        regional_entries_without_results = scalar(
            con,
            """SELECT COUNT(*) FROM championship_entry ce
                WHERE NOT EXISTS (SELECT 1 FROM championship_entry_result cer
                                   WHERE cer.championship_entry_id = ce.id)""")
        if regional_entries_without_results:
            raise RuntimeError(
                f"{regional_entries_without_results} regional entries have no source results")
        regional_family_leaks = scalar(
            con,
            """SELECT COUNT(*) FROM championship_entry_result cer
                 JOIN result r ON r.id = cer.result_id WHERE r.result_kind = 'family'""")
        if regional_family_leaks:
            raise RuntimeError(
                f"{regional_family_leaks} Family rows leak into regional championships")
        duplicate_individual_results = scalar(
            con,
            """SELECT COUNT(*) FROM (
                   SELECT result_list_id, person_id, observed_name, observed_club,
                          observed_rank, observed_status, observed_time
                   FROM result
                   WHERE result_kind = 'individual'
                   GROUP BY result_list_id, person_id, observed_name, observed_club,
                            observed_rank, observed_status, observed_time
                   HAVING COUNT(*) > 1)""")
        if duplicate_individual_results:
            raise RuntimeError(
                f"{duplicate_individual_results} exact individual result groups are duplicated")
        awards_without_positive_eligibility = scalar(
            con,
            """SELECT COUNT(*) FROM award a JOIN result r ON r.id = a.result_id
               WHERE r.championship_eligibility_state NOT IN ('eligible', 'provisional')""")
        if awards_without_positive_eligibility:
            raise RuntimeError(
                f"{awards_without_positive_eligibility} medals lack positive eligibility evidence")
        identifier_conflicts = scalar(
            con,
            """SELECT COUNT(*) FROM (
                   SELECT scheme, identifier FROM person_identifier
                   GROUP BY scheme, identifier HAVING COUNT(DISTINCT person_id) > 1
               )""")
        memberships_without_registry = scalar(
            con,
            """SELECT COUNT(*) FROM person_club_membership pcm
               WHERE NOT EXISTS (
                   SELECT 1 FROM person_identifier pi
                   WHERE pi.person_id = pcm.person_id
                     AND pi.scheme = 'oefol_id'
                     AND pi.source = 'anne-user-registry'
               )""")
        if memberships_without_registry:
            raise RuntimeError(
                f"{memberships_without_registry} club memberships lack an ANNE /user identity")
        by_source = dict(con.execute(
            "SELECT source, COUNT(*) FROM result GROUP BY source ORDER BY source"))
        by_kind = dict(con.execute(
            "SELECT result_kind, COUNT(*) FROM result GROUP BY result_kind ORDER BY result_kind"))
        counts["championship_result"] = scalar(
            con, "SELECT COUNT(*) FROM result WHERE championship IS NOT NULL")
        return {
            "schema_version": scalar(con, "PRAGMA user_version"),
            "logical_sha256": logical_fingerprint(con),
            "counts": counts,
            "result_by_source": by_source,
            "result_by_kind": by_kind,
            "eligibility": state_counts(eligibility_path),
            "quality": {
                "results_without_provenance": missing_provenance,
                "results_without_identity_basis": missing_identity_basis,
                "results_without_result_list": missing_list,
                "illegal_statuses": illegal_status,
                "negative_elapsed_times": negative_elapsed_time,
                "illegal_eligibility_states": illegal_eligibility,
                "unmatched_explicit_championship_rows": unmatched_explicit_championship,
                "family_identity_leaks": family_identity,
                "personless_non_family": personless_ordinary,
                "ooc_championship_leaks": ooc_championship,
                "duplicate_relay_awards": duplicate_relay_awards,
                "regional_double_assignments": regional_double_assignment,
                "regional_entries_without_results": regional_entries_without_results,
                "regional_family_leaks": regional_family_leaks,
                "duplicate_individual_results": duplicate_individual_results,
                "awards_without_positive_eligibility": awards_without_positive_eligibility,
                "identifier_conflicts": identifier_conflicts,
                "memberships_without_registry": memberships_without_registry,
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
