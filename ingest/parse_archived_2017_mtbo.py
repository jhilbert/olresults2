#!/usr/bin/env python3
"""Normalize the preserved international 2017 MTBO lists without re-ranking them.

The bold Austrian champion annotation is kept separately in ``note``.  The
printed placement is the international placement, not an Austrian podium rank.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from parse_sportsoftware_html import decode, parse_document, TableExtractor

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / 'data/review/archived_sources/2017'
SOURCES = (
    ('sprint', 1, '2017-07-15', '126ff69cfcecdc10cfa2f44839568801055e53ed9be3d37f78d47b9fa6625f6e', 294),
    ('lang', 3, '2017-07-16', '2db359016caf2f5c923ceb7ab68275c394902777d308d22bd616db98f579e17e', 270),
)
CHAMPION = re.compile(r'^(Österreichische(?:r)? (?:Staats)?Meister(?:in)?)(.+)$', re.I)


def document(spec):
    name, number, date, digest, expected = spec
    path = ARCHIVE / f'1865-{name}-international.html'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f'Archived MTBO source changed: {path}')
    text = decode(raw)
    categories = parse_document(text)
    extractor = TableExtractor()
    extractor.feed(text)
    nations = {}
    for table in extractor.tables:
        for cells in table:
            if len(cells) < 5 or (cells[2] and not re.fullmatch(r'[A-Z]{3}(?:/[A-Z]{3})*', cells[2])):
                continue
            match = CHAMPION.match(cells[1])
            person = match.group(2).strip() if match else cells[1].strip()
            key = (person, cells[3].strip(), cells[4].strip())
            if key in nations and nations[key] != cells[2]:
                raise ValueError(f'Conflicting printed nations: {key}')
            nations[key] = cells[2]
    if len(categories) != 25 or sum(len(c['results']) for c in categories) != expected:
        raise ValueError(f'Unexpected MTBO source structure: {path}')
    for category in categories:
        for row in category['results']:
            match = CHAMPION.match(row['name'])
            if match:
                row['name'] = match.group(2).strip()
                row['note'] = 'Originalquelle: ' + match.group(1)
            key = (row['name'], row.get('club', '').strip(), row.get('timeText', '').strip())
            if key not in nations:
                raise ValueError(f'Printed nation could not be matched: {key}')
            row['sourceNat'] = nations[key]
    suffix = 's1' if number == 1 else 's3'
    return {'eventId': 1865, 'source': 'sportsoftware-html',
            'sourceUrl': f'http://www.mtbodays.at/mtbodays2017/live/all/{suffix}_OEM_mtbodays_erg.html',
            'snapshotPath': path.relative_to(ROOT).as_posix(),
            'fileName': f'mtbodays-2017-results-e{number}.html', 'listType': 'race',
            'docDate': date, 'categories': categories}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for index, spec in enumerate(SOURCES):
        path = ROOT / 'data/normalized' / f'1865-{index}.json'
        content = json.dumps(document(spec), ensure_ascii=False)
        if args.check:
            if not path.is_file() or path.read_text() != content:
                raise SystemExit(f'Archive normalization differs: {path}')
        else:
            path.write_text(content)
        print(f'{path.name}: {"verified" if args.check else "normalized"}')


if __name__ == '__main__':
    main()
