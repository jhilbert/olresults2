#!/usr/bin/env python3
"""Normalize the organizer's user-supplied 2014 ÖM team result list."""
import argparse
import hashlib
import json
import re
from pathlib import Path

import pdfplumber
from parse_archived_2017 import CATEGORY, TEAM, MEMBER

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / 'data/review/archived_sources/2014/1076-official-result.pdf'
OUTPUT = ROOT / 'data/normalized/1076-0.json'
SHA256 = '3cf744e6e6a842876eaad21755ebc8c79ed8f43e4b19a8303520c8c8567d1191'


def document():
    if hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest() != SHA256:
        raise ValueError('2014 official source differs from the reviewed PDF')
    with pdfplumber.open(SNAPSHOT) as pdf:
        lines = [line.strip() for page in pdf.pages for line in (page.extract_text() or '').splitlines()]
    if lines[:3] != ['Ergebnisliste', 'ÖM Mannschaft 2014', 'Untergeng, 7. September 2014']:
        raise ValueError('Unexpected 2014 result-list header')
    categories, category, team = [], None, None
    pending_champion = False
    for line in lines[3:]:
        if line.startswith('Einzel-Kurz '):
            break
        match = CATEGORY.match(line)
        if match:
            category = {'name': match.group(1), 'sourceUnitCount': 0, 'rankingBasis': 'time', 'results': []}
            categories.append(category)
            team, pending_champion = None, False
            continue
        if re.match(r'^1\.\s+und\s+österr\.\s+Meister', line, re.I):
            pending_champion = True
            continue
        match = TEAM.match(line)
        if match:
            place, number, club, result = match.groups()
            status = {'fehlst':'mp', 'disqu':'dsq', 'n ang':'dns', 'aufg':'dnf'}.get(result.lower(), 'ok')
            team = {'club':club, 'teamName':f'{club} #{number}', 'teamNumber':number,
                    'resultKind':'team', 'timeText':result, 'teamTimeText':result, 'status':status}
            if status == 'ok':
                seconds = sum(int(n)*60**i for i,n in enumerate(reversed(result.split(':'))))
                team.update(timeS=seconds, teamTimeS=seconds)
            if place and place != 'AK':
                team['rank'] = int(place)
            elif pending_champion:
                team['rank'] = 1
            if place == 'AK':
                team['outOfCompetition'] = True
            if pending_champion:
                team.update(championship='ÖM', note='Originalquelle: Österreichischer Meistertitel')
            pending_champion = False
            category['sourceUnitCount'] += 1
            continue
        if not team:
            raise ValueError(f'Unexpected line outside a team: {line!r}')
        member = MEMBER.fullmatch(line)
        if not member:
            raise ValueError(f'Unexpected team member: {line!r}')
        name, year = member.groups()
        if name.casefold() == 'vakant':
            category['sourceVacantSlots'] = category.get('sourceVacantSlots', 0) + 1
            continue
        row = {'name':name, 'nameCanonicalOrder':True, **team}
        if (category['name'], team['teamNumber'], team['club'], name, year) == (
                'Offen', '115', 'HSV Ried', 'Katharina Gittmaier', '89'):
            # The user confirmed this observation belongs with the existing
            # HSV Ried person despite the implausible year in the source.
            # Keep the printed value as evidence, not as an identity input.
            row['sourceYearPrinted'] = '89'
            row['note'] = 'Quelljahrgang 89 laut Nutzerbestätigung nicht für Personenzuordnung verwendet'
        elif year is not None:
            y = int(year)
            row['yearOfBirth'] = y + (2000 if y <= 14 else 1900)
            row['strictYearOfBirth'] = True
        category['results'].append(row)
    expected = ['H 19-', 'D 19-', 'H -14', 'H 15-18', 'H 40-', 'H 50-',
                'H 60-', 'D -14', 'D 15-18', 'D 40-', 'D 50-', 'D 60-', 'Offen']
    if [c['name'] for c in categories] != expected or any(len(c['results']) + c.get('sourceVacantSlots', 0) != 3*c['sourceUnitCount'] for c in categories):
        raise ValueError('Unexpected category order or team roster size')
    return {'eventId':1076, 'source':'sportsoftware-pdf',
            'sourceUrl':'sources/1076-oem-mannschaft-2014.pdf',
            'snapshotPath':SNAPSHOT.relative_to(ROOT).as_posix(),
            'fileName':'1076-oem-mannschaft-2014.pdf', 'listType':'race',
            'docDate':'2014-09-07', 'docTitle':'ÖM Mannschaft 2014', 'categories':categories}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    text = json.dumps(document(), ensure_ascii=False)
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text() != text:
            raise SystemExit('Archived 2014 normalization differs')
    else:
        OUTPUT.write_text(text)
    print(f'{OUTPUT.name}: {"verified" if args.check else "normalized"}')


if __name__ == '__main__':
    main()
