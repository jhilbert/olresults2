#!/usr/bin/env python3
"""Build the independent WOLV 2026 source archive from its retained PDF.

The full source cell is retained verbatim: merged headings, non-award reasons,
empty podium places and names are evidence, not inferred ANNE results.
"""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from extract_wolv_annual import extract_document
from extract_wolv_history import extract as extract_html, extract_pdf

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / 'data/review/archived_sources/2026/wolv-annual.pdf'
OUT = ROOT / 'site/wolv'
EXPECTED_SHA256 = '63f3389af690733090abafc167262407db50a60d565c52dbd0250e5e9d24e56b'


def parse_cell(cell, year=2026):
    text = cell['cell']
    # In the 2019 PDF, the preceding cell's starter count visually overlaps
    # several first-place markers. pdfplumber reads e.g. "(13)) KROUPA"
    # instead of the separately printed "1) KROUPA". Restore only these
    # source-verified names; retain the unmodified cell as source_text.
    parsing_text = text
    if year == 2019:
        parsing_text = re.sub(
            r'(?m)^\s*\(1\d+\)\)\s+(?=(?:KROUPA Günther|DYTLEWSKI Nick|GRÜN Paul|GASSNER Jasmina)\b)',
            '1) ', parsing_text)
    markers = list(re.finditer(r'(?m)^\s*([123])\s*\)\s*', parsing_text))
    medals = []
    for index, match in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(parsing_text)
        value = ' '.join(parsing_text[match.end():end].split())
        value = re.sub(r'\s*\.?\s*\(\d+\)\s*$', '', value).strip()
        # The preserved PDF visibly says KASTNER; PDF extraction inserts a
        # spurious word break in this one known cell. Keep source_text intact.
        if year == 2026 and (cell['page'], cell['row'], cell['column']) == (1, 25, 2):
            value = re.sub(r'\bK\s+A\s*STNER Barbara\b', 'KASTNER Barbara', value)
        # This annual-table cell denotes a shared second place with '+',
        # not a two-person team. ANNE independently prints both at rank 2.
        if (year == 2013 and (cell['page'], cell['row'], cell['column']) == (1, 3, 4)
                and int(match.group(1)) == 2
                and value == 'KASTNER Nicolas NF Wien + WOLFRAM Clemens NF Wien'):
            medals.extend([{'rank': 2, 'text': 'KASTNER Nicolas NF Wien'},
                           {'rank': 2, 'text': 'WOLFRAM Clemens NF Wien'}])
            continue
        if value and re.search(r'[^\s.\-–]', value):
            medals.append({'rank': int(match.group(1)), 'text': value})
    prefix = parsing_text[:markers[0].start()].strip() if markers else ''
    # A count leaking from the preceding visual cell is not a class label.
    prefix = re.sub(r'^(?:\(\d+\)\s*)+', '', prefix).strip()
    if not markers:
        first_line = text.splitlines()[0].strip() if text.splitlines() else ''
        if re.fullmatch(r'(?:D|H|DH)\s*[-\d\s]+:', first_line, re.IGNORECASE):
            prefix = first_line
    return {
        'page': cell['page'], 'row': cell['row'], 'column': cell['column'],
        'source_category': cell['row_heading'], 'source_heading': prefix,
        'discipline': cell['discipline_heading'], 'event_heading': cell['event_heading'],
        'source_text': text, 'shared_notice': cell.get('shared_notice', ''), 'medals': medals,
    }


def build(pdf=PDF):
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != EXPECTED_SHA256:
        raise ValueError('WOLV source changed; review the new document before publishing')
    doc = extract_document(2026, pdf)
    rows = [parse_cell(cell) for cell in doc['cells']]
    if len(rows) != 73:
        raise ValueError(f'unexpected WOLV cell count: {len(rows)}')
    return {'schema_version': 1, 'year': 2026, 'source_url': doc['source_url'],
            'source_sha256': doc['sha256'], 'entries': rows}


def build_archive():
    manifest = json.loads((ROOT / 'data/review/wolv-archive-manifest.json').read_text())
    years = []
    for year_text, source in sorted(manifest['years'].items(), key=lambda item: int(item[0])):
        year = int(year_text)
        path = ROOT / source['path']
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != source['sha256']:
            raise ValueError(f'WOLV source {year} changed; inspect before updating the archive')
        if year == 2026:
            item = build(path)  # retains the independently checked pilot parser
        else:
            cells = extract_html(year, path) if path.suffix == '.html' else extract_pdf(year, path)
            item = {'schema_version':1,'year':year,'source_url':source['url'],
                    'source_sha256':digest,'entries':[parse_cell(cell,year) for cell in cells]}
        item['source_file'] = f'sources/{year}/index{".html" if path.suffix == ".html" else ".pdf"}'
        years.append(item)
    return {'schema_version':1,'years':years}


def write_database(data, path):
    """Create a separate source database, without copying inferred ANNE data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='wolv-', suffix='.db', dir=path.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    with sqlite3.connect(temp_path) as con:
        con.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE source (year INTEGER PRIMARY KEY, url TEXT NOT NULL, sha256 TEXT NOT NULL);
            CREATE TABLE classification (id INTEGER PRIMARY KEY, year INTEGER NOT NULL REFERENCES source(year),
                page INTEGER NOT NULL, source_row INTEGER NOT NULL, source_column INTEGER NOT NULL,
                category TEXT NOT NULL, heading TEXT NOT NULL, discipline TEXT NOT NULL,
                event_heading TEXT NOT NULL, source_text TEXT NOT NULL, shared_notice TEXT NOT NULL,
                UNIQUE(year, page, source_row, source_column));
            CREATE TABLE medal (classification_id INTEGER NOT NULL REFERENCES classification(id),
                ordinal INTEGER NOT NULL, rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 3),
                source_text TEXT NOT NULL, PRIMARY KEY(classification_id, ordinal));
        ''')
        for year_data in data.get('years', [data]):
            con.execute('INSERT INTO source VALUES (?,?,?)',
                        (year_data['year'],year_data['source_url'],year_data['source_sha256']))
            for entry in year_data['entries']:
                cur = con.execute('''INSERT INTO classification
                (year,page,source_row,source_column,category,heading,discipline,event_heading,source_text,shared_notice)
                VALUES (?,?,?,?,?,?,?,?,?,?)''', (year_data['year'],entry['page'],entry['row'],entry['column'],
                entry['source_category'],entry['source_heading'],entry['discipline'],
                entry['event_heading'],entry['source_text'],entry['shared_notice']))
                con.executemany('INSERT INTO medal VALUES (?,?,?,?)',
                                [(cur.lastrowid,i,m['rank'],m['text']) for i,m in enumerate(entry['medals'],1)])
    temp_path.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=OUT / 'data.json')
    parser.add_argument('--copy-source', action='store_true')
    args = parser.parse_args()
    data = build_archive()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_database(data, args.output.parent / 'wolv.db')
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    if args.copy_source:
        manifest = json.loads((ROOT / 'data/review/wolv-archive-manifest.json').read_text())
        for year_data in data['years']:
            year = year_data['year']
            dest = args.output.parent / year_data['source_file']
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / manifest['years'][str(year)]['path'], dest)
            if dest.suffix == '.html':
                css = (ROOT / 'data/review/archived_sources/2014/wolv-stylesheet.css' if year == 2014
                       else ROOT / 'data/review/archived_sources/wolv-1994-2011-stylesheet.css')
                shutil.copyfile(css, dest.parent / 'stylesheet.css')
    print(f"WOLV archive: {len(data['years'])} years, {sum(len(y['entries']) for y in data['years'])} source cells")


if __name__ == '__main__':
    main()
