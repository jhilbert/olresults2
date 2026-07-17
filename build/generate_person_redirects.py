#!/usr/bin/env python3
"""Generate redirects from a previously published DB to the current DB.

Used for the one-time transition from encounter-order negative person ids to
deterministic ids.  Only unambiguous legacy identities are mapped; positive
ANNE/ÖFOL ids are already stable and never need redirects.
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


def people_by_identity(db_path, negative_only=False):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        where = " WHERE id < 0" if negative_only else ""
        rows = con.execute(
            f"SELECT id, name_key, year_of_birth FROM person{where}").fetchall()
        by_key = defaultdict(list)
        for person_id, name_key, yob in rows:
            by_key[(name_key, yob)].append(person_id)
        return by_key
    finally:
        con.close()


def result_signatures(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """SELECT r.person_id, s.event_id, r.stage_id, r.category, r.rank,
                      r.status, r.time_s, r.source
               FROM result r JOIN stage s ON s.id = r.stage_id""").fetchall()
        by_person = defaultdict(set)
        for person_id, *signature in rows:
            by_person[person_id].add(tuple(signature))
        return by_person
    finally:
        con.close()


def people_only_in_events(db_path, event_ids):
    if not event_ids:
        return set()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        by_person = defaultdict(set)
        for person_id, event_id in con.execute(
                "SELECT DISTINCT r.person_id, s.event_id FROM result r "
                "JOIN stage s ON s.id = r.stage_id"):
            by_person[person_id].add(event_id)
        allowed = set(event_ids)
        return {person_id for person_id, seen in by_person.items() if seen and seen <= allowed}
    finally:
        con.close()


def merge_redirect_history(existing, fresh, current_ids):
    """Keep published redirect ids valid across more than one migration."""
    combined = {str(old_id): int(target) for old_id, target in existing.items()}
    combined.update({str(old_id): int(target) for old_id, target in fresh.items()})

    def resolve(target):
        seen = set()
        while target not in current_ids and str(target) in combined:
            if target in seen:
                raise RuntimeError(f"person redirect cycle at {target}")
            seen.add(target)
            target = combined[str(target)]
        if target not in current_ids:
            raise RuntimeError(f"person redirect target does not exist: {target}")
        return target

    return {old_id: resolve(target) for old_id, target in combined.items()
            if int(old_id) != resolve(target)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("previous_db", type=Path)
    parser.add_argument("current_db", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--exclude-event-only", type=int, action="append", default=[],
        help="do not redirect old identities whose results exist only in this bad event")
    args = parser.parse_args()

    old = people_by_identity(args.previous_db, negative_only=True)
    new_negative = people_by_identity(args.current_db, negative_only=True)
    new_all = people_by_identity(args.current_db)
    old_identity_by_id = {person_id: identity for identity, ids in old.items() for person_id in ids}
    new_identity_by_id = {person_id: identity for identity, ids in new_all.items() for person_id in ids}
    redirects = {}
    ambiguous = 0
    unmatched = []
    intentionally_excluded = people_only_in_events(args.previous_db, args.exclude_event_only)
    for identity, old_ids in old.items():
        active_old_ids = [old_id for old_id in old_ids if old_id not in intentionally_excluded]
        if not active_old_ids:
            continue
        # Prefer the deterministic legacy identity. If it disappeared because
        # the same observation now resolves to an authoritative positive id,
        # use that only when it is the single remaining candidate.
        new_ids = new_negative.get(identity, [])
        if not new_ids:
            new_ids = new_all.get(identity, [])
        if len(active_old_ids) == 1 and len(new_ids) == 1:
            if active_old_ids[0] != new_ids[0]:
                redirects[str(active_old_ids[0])] = new_ids[0]
        elif not new_ids:
            unmatched.extend(active_old_ids)
        else:
            ambiguous += len(active_old_ids)

    # A removed bad observation can change the majority display spelling of a
    # still-real person. For identity-key misses, recover only a unique result
    # fingerprint match; rows that existed solely in the removed bad source
    # intentionally remain without a redirect.
    if unmatched:
        old_signatures = result_signatures(args.previous_db)
        new_signatures = result_signatures(args.current_db)
        new_by_signature = defaultdict(set)
        for person_id, signatures in new_signatures.items():
            for signature in signatures:
                new_by_signature[signature].add(person_id)
        still_missing = []
        for old_id in unmatched:
            scores = Counter()
            signatures = {
                signature for signature in old_signatures.get(old_id, set())
                if signature[0] not in set(args.exclude_event_only)
            }
            for signature in signatures:
                for person_id in new_by_signature.get(signature, ()):
                    scores[person_id] += 1
            ranked = scores.most_common()
            winner = None
            if ranked:
                top_score = ranked[0][1]
                top_ids = [person_id for person_id, score in ranked if score == top_score]
                if len(top_ids) == 1:
                    winner = top_ids[0]
                else:
                    old_name_key = old_identity_by_id[old_id][0]
                    same_name = [person_id for person_id in top_ids
                                 if new_identity_by_id[person_id][0] == old_name_key]
                    if len(same_name) == 1:
                        winner = same_name[0]
                if winner is not None and not (
                        top_score >= 2 or top_score == len(signatures)):
                    winner = None
            if winner is not None:
                redirects[str(old_id)] = winner
            else:
                still_missing.append(old_id)
        unmatched = still_missing

    missing = len(unmatched)

    existing = json.loads(args.output.read_text()) if args.output.exists() else {}
    current_ids = {person_id for ids in new_all.values() for person_id in ids}
    redirects = merge_redirect_history(existing, redirects, current_ids)
    value = dict(sorted(redirects.items(), key=lambda item: int(item[0]), reverse=True))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(args.output.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=1, sort_keys=False) + "\n")
    temp.replace(args.output)
    print(
        f"wrote {len(redirects)} redirects; "
        f"{len(intentionally_excluded)} intentionally excluded, "
        f"{missing} old identities disappeared, {ambiguous} were ambiguous")
    if unmatched and len(unmatched) <= 20:
        print("unmatched old ids: " + ", ".join(map(str, sorted(unmatched))))
    return 1 if ambiguous else 0


if __name__ == "__main__":
    sys.exit(main())
