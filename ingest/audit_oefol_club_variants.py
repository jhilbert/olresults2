#!/usr/bin/env python3
"""List same-person, same-year club differences; evidence, not automatic merges.

The source can contain homonyms, team memberships, club changes, and different
clubs for different disciplines. Original names/clubs and award IDs are shown
so each candidate can be reviewed against the underlying source.
"""
import argparse
import json
import unicodedata
from collections import defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def key(name):
    return ''.join(c for c in unicodedata.normalize('NFKD', name.casefold())
                   if c.isalnum() and not unicodedata.combining(c))


def candidates(data):
    groups = defaultdict(list)
    for award in data['awards']:
        for member in award['members']:
            name, club = member.get('name'), member.get('club') or award.get('club')
            # A surname alone does not establish identity.
            if not name or len(name.split()) < 2 or not club:
                continue
            groups[(key(name), award['year'])].append({
                'name': name, 'club': club, 'discipline': award['discipline'],
                'format': award['format'], 'award_id': award['id']})
    result = []
    for (person, year), rows in sorted(groups.items()):
        for first, second in combinations(rows, 2):
            if key(first['club']) == key(second['club']):
                continue
            result.append({'person_key': person, 'year': year,
                           'same_discipline': first['discipline'] == second['discipline'],
                           'first': first, 'second': second})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--same-discipline', action='store_true')
    args = parser.parse_args()
    data = json.loads((ROOT / 'site/oefol/meistertafel.json').read_text())
    result = candidates(data)
    if args.same_discipline:
        result = [r for r in result if r['same_discipline']]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
