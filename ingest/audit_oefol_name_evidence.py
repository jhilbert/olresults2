#!/usr/bin/env python3
"""Read-only coverage audit; matching a person is not proof of a title."""
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def norm(value):
    value = unicodedata.normalize('NFKD', value or '').casefold()
    return ' '.join(re.findall(r'[^\W_]+', ''.join(c for c in value if not unicodedata.combining(c))))


def names(value):
    value = norm(value)
    words = value.split()
    # ANNE often prints surname first; rotations retain the order of
    # multi-part given and family names instead of permuting their words.
    return {' '.join(words[i:] + words[:i]) for i in range(len(words))} if words else {value}


def result_names(value):
    # Some official individual results list ex-aequo pairs in one cell.
    # Keep the complete cell as well, but never split a hyphenated surname.
    parts = re.split(r'\s+\+\s+|\s+/\s+|\s+und\s+', value, flags=re.I)
    return set().union(*(names(part) for part in [value, *parts]))


def historical_register_audit():
    """Strict identity candidates only, not evidence of a championship title."""
    awards = json.loads((ROOT / 'site/oefol/meistertafel.json').read_text())['awards']
    users = json.loads((ROOT / 'data/raw/anne/user_index.json').read_text())['users']
    index = defaultdict(list)
    for user in users:
        first, last = user.get('first_name'), user.get('last_name')
        if first and last:
            index[norm(first + ' ' + last)].append(user)
    counts = Counter()
    for award in awards:
        year = award['year']
        if year >= 2013:
            continue
        for member in award['members']:
            counts['members'] += 1
            candidates = index.get(norm(member['name']), [])
            if not candidates:
                counts['no_exact_name'] += 1
                continue
            club = norm(member.get('club') or award.get('club'))
            valid = []
            for user in candidates:
                birth = user.get('year_of_birth')
                if birth and not 6 <= year - birth <= 95:
                    continue
                for membership in user.get('active_memberships', []):
                    start = membership.get('date_from') or ''
                    end = membership.get('date_to') or ''
                    # A year-only award cannot establish dates within the year.
                    if club and norm((membership.get('club') or {}).get('name')) == club and start and start[:4].isdigit() and int(start[:4]) < year and (not end or (end[:4].isdigit() and int(end[:4]) > year)):
                        valid.append(user['oefol_id'])
                        break
            if len(set(valid)) == 1:
                counts['exact_name_and_dated_club'] += 1
            elif valid:
                counts['ambiguous_identity'] += 1
            else:
                counts['name_only_or_club_unconfirmed'] += 1
    return counts


def audit(show_unmatched=False):
    source = json.loads((ROOT / 'site/oefol/meistertafel.json').read_text())
    overlay = json.loads((ROOT / 'site/oefol/person-display.json').read_text())
    corrections = {(c['source_row'], c['source_name']): c for c in overlay['confirmations'] + overlay['corrections']}
    results = defaultdict(list)
    for path in (ROOT / 'data/normalized').glob('*.json'):
        try:
            doc = json.loads(path.read_text())
            year = int((doc.get('docDate') or '')[:4])
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        for category in doc.get('categories', []):
            for result in category.get('results', []):
                if not result.get('name'):
                    continue
                for name in result_names(result['name']):
                    results[(year, name)].append((norm(result.get('club')), doc.get('eventId'), doc.get('sourceUrl')))
    # API results can exist in OLResults without a separate normalized document.
    # Use the stage date when present; a multi-stage event can cross a year.
    db = ROOT / 'site/data/results.db'
    if db.exists():
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
            query = '''SELECT COALESCE(s.date, e.date_from), p.name,
                              r.observed_name, r.official_club, r.club, e.id, e.url
                       FROM result r JOIN person p ON p.id=r.person_id
                       JOIN stage s ON s.id=r.stage_id JOIN event e ON e.id=s.event_id'''
            for date, person, observed, official_club, club, event_id, url in conn.execute(query):
                try:
                    year = int((date or '')[:4])
                except ValueError:
                    continue
                for raw_name in {person, observed} - {None, ''}:
                    for name in result_names(raw_name):
                        for raw_club in {official_club, club} - {None, ''}:
                            results[(year, name)].append((norm(raw_club), event_id, url))
    counts = defaultdict(Counter)
    for award in source['awards']:
        year = award['year']
        for member in award['members']:
            c = corrections.get((member['source_row'], member['name']))
            display = (c['first_name'] + ' ' + c['last_name']) if c else member['name']
            club = norm(member.get('club') or award.get('club'))
            hits = [hit for n in names(display) for hit in results.get((year, n), [])]
            exact_club = [hit for hit in hits if club and hit[0] == club]
            bucket = counts[year]
            bucket['members'] += 1
            bucket['same_name_and_club'] += bool(exact_club)
            bucket['same_name_only'] += bool(hits and not exact_club)
            bucket['no_exact_result'] += not hits
            bucket['explicit_identity_id'] += bool(c and c.get('oefol_id'))
            if show_unmatched and year >= 2013 and not hits:
                print(f"{year}\t{award['format']}\t{award['category']}\t{award.get('club')}\t{member['source_row']}\t{display}")
    return counts


if __name__ == '__main__':
    if '--register-historical' in sys.argv:
        print('Personeneinträge vor 2013; Registertreffer bestätigen nicht den Meistertitel:')
        for key, value in historical_register_audit().items():
            print(f'{key}\t{value}')
        sys.exit(0)
    if '--unmatched' in sys.argv:
        audit(True)
        sys.exit(0)
    print('Personeneinträge nach Quellenjahr; gleicher Name/Verein im selben Jahr ist kein Titelabgleich.')
    print('Jahr\tEinträge\tName+Verein\tNur Name\tKein exakter Ergebnistreffer\tExplizite ID')
    for year, c in sorted(audit().items(), reverse=True):
        print(f"{year}\t{c['members']}\t{c['same_name_and_club']}\t{c['same_name_only']}\t{c['no_exact_result']}\t{c['explicit_identity_id']}")
