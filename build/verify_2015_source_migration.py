#!/usr/bin/env python3
"""Audit the explicitly approved 2015 Austrian-source migration before rebasing health.

This is a one-time, read-only comparison with the previously published DB,
not an exception to the normal publication health checks.  In particular it
checks row multiplicity, source provenance, and reviewed person redirects.
"""
import argparse
import json
import sqlite3
from pathlib import Path

import validate_db

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    1378: (2318, 384, 'https://anne-cdn.oefol.at/public/legacy/event_1378_ergebnisse_oestm_middle.html'),
    1379: (2353, 400, 'https://anne-cdn.oefol.at/public/legacy/event_1379_ergebnisse_oestm_sprint.html'),
}


def lost_observations(con):
    """Count missing source observations, including repeated identical rows."""
    return con.execute("""WITH old_rows AS (
        SELECT s.event_id,s.number,r.source,r.category,
               COALESCE(r.observed_name,'') nm,COALESCE(r.observed_club,'') club,
               COALESCE(r.observed_rank,'') rank,COALESCE(r.observed_time,'') time,
               COALESCE(r.observed_status,'') status,COUNT(*) n
          FROM previous.result r JOIN previous.stage s ON s.id=r.stage_id
         WHERE s.event_id NOT IN (1378,1379) GROUP BY 1,2,3,4,5,6,7,8,9
    ), new_rows AS (
        SELECT s.event_id,s.number,r.source,r.category,
               COALESCE(r.observed_name,'') nm,COALESCE(r.observed_club,'') club,
               COALESCE(r.observed_rank,'') rank,COALESCE(r.observed_time,'') time,
               COALESCE(r.observed_status,'') status,COUNT(*) n
          FROM main.result r JOIN main.stage s ON s.id=r.stage_id
         WHERE s.event_id NOT IN (1378,1379) GROUP BY 1,2,3,4,5,6,7,8,9
    ) SELECT COALESCE(SUM(o.n-COALESCE(n.n,0)),0)
        FROM old_rows o LEFT JOIN new_rows n
          ON o.event_id=n.event_id AND o.number=n.number AND o.source=n.source
         AND o.category=n.category AND o.nm=n.nm AND o.club=n.club
         AND o.rank=n.rank AND o.time=n.time AND o.status=n.status
       WHERE o.n>COALESCE(n.n,0)""").fetchone()[0]


def verify(previous_db, current_db, eligibility_path, baseline_path):
    baseline = json.loads(baseline_path.read_text())
    previous = validate_db.collect(previous_db, eligibility_path)
    current = validate_db.collect(current_db, eligibility_path)
    if previous != baseline:
        raise ValueError('Published database no longer matches the production health baseline')
    if any(current['eligibility'][key] < baseline['eligibility'][key] for key in ('people', 'decisions')):
        raise ValueError('Eligibility state has shrunk')
    if current['counts']['event'] < baseline['counts']['event'] or current['counts']['stage'] < baseline['counts']['stage']:
        raise ValueError('Events or stages disappeared')
    if current['counts']['person'] != 30749 or current['counts']['result'] != 267208:
        raise ValueError('Candidate counts changed; repeat the migration audit')
    con = sqlite3.connect(f'file:{current_db}?mode=ro', uri=True)
    try:
        con.execute('ATTACH DATABASE ? AS previous', (f'file:{previous_db}?mode=ro',))
        missing = lost_observations(con)
        if missing:
            raise ValueError(f'{missing} source observations outside the approved events disappeared')
        for event_id, (old_count, expected_count, source_url) in EXPECTED.items():
            old_n = con.execute('SELECT COUNT(*) FROM previous.result r JOIN previous.stage s ON s.id=r.stage_id WHERE s.event_id=?', (event_id,)).fetchone()[0]
            urls = con.execute('SELECT d.source_url, COUNT(*) FROM result r JOIN stage s ON s.id=r.stage_id JOIN source_document d ON d.id=r.source_document_id WHERE s.event_id=? GROUP BY d.source_url', (event_id,)).fetchall()
            declared = con.execute('SELECT COALESCE(SUM(rl.parsed_entries),0) FROM result_list rl JOIN stage s ON s.id=rl.stage_id WHERE s.event_id=?', (event_id,)).fetchone()[0]
            if old_n != old_count or urls != [(source_url, expected_count)] or declared != expected_count:
                raise ValueError(f'Event {event_id} source or entry count changed')
        reviewed = json.loads((ROOT / 'data/review/official_2015_person_redirects.json').read_text())['redirects']
        if len(reviewed) != 126:
            raise ValueError('Reviewed redirect set changed')
        for old_id, new_id in reviewed.items():
            row = con.execute('SELECT new_id FROM person_redirect WHERE old_id=?', (int(old_id),)).fetchone()
            if row != (new_id,):
                raise ValueError(f'Reviewed redirect is missing for {old_id}')
            names = con.execute('SELECT p.name_key,q.name_key FROM previous.person p JOIN main.person q ON q.id=? WHERE p.id=?', (new_id,int(old_id))).fetchone()
            if not names or names[0] != names[1]:
                raise ValueError(f'Reviewed redirect name differs for {old_id}')
            matches = con.execute('''SELECT DISTINCT n.person_id FROM previous.result o
                JOIN previous.stage os ON os.id=o.stage_id
                JOIN main.result n ON n.result_list_id=o.result_list_id
                  AND n.observed_name IS o.observed_name
                  AND n.observed_rank IS o.observed_rank AND n.observed_time IS o.observed_time
                WHERE o.person_id=? AND os.event_id NOT IN (1378,1379)''', (int(old_id),)).fetchall()
            if matches != [(new_id,)]:
                raise ValueError(f'Reviewed redirect is not uniquely supported by surviving source rows for {old_id}')
        other_identities = con.execute("""SELECT COUNT(DISTINCT p.id) FROM previous.person p
            LEFT JOIN main.person n ON n.id=p.id
            WHERE n.id IS NULL AND EXISTS (SELECT 1 FROM previous.result r
              JOIN previous.stage s ON s.id=r.stage_id WHERE r.person_id=p.id
              AND s.event_id NOT IN (1378,1379))""").fetchone()[0]
        if other_identities != 140:
            raise ValueError('Affected legacy identities changed; repeat the redirect audit')
    finally:
        con.close()
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('previous_db', type=Path)
    parser.add_argument('current_db', type=Path)
    parser.add_argument('--eligibility', type=Path, required=True)
    parser.add_argument('--baseline', type=Path,
                        default=ROOT / 'data/review/official_2015_previous_health.json')
    args = parser.parse_args()
    try:
        report = verify(args.previous_db, args.current_db, args.eligibility, args.baseline)
    except (ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
        parser.exit(1, f'2015 migration verification failed: {exc}\n')
    print(json.dumps(report, indent=2, sort_keys=True))
    print('2015 migration verification: ok; 0 other source observations lost; 126 reviewed redirects')


if __name__ == '__main__':
    main()
