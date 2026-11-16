#!/usr/bin/env python3
"""Read archived WOLV Excel-as-HTML tables without discarding source cells."""
from html.parser import HTMLParser
import hashlib
import re
from pathlib import Path


class TableReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.depth += 1
            if self.depth == 1:
                self.table = []
        elif self.depth == 1 and tag == 'tr':
            self.row = []
        elif self.depth == 1 and tag in ('td', 'th') and self.row is not None:
            self.cell = {'parts': [], 'rowspan': int(attrs.get('rowspan') or 1),
                         'colspan': int(attrs.get('colspan') or 1)}
        elif self.cell is not None and tag == 'br':
            self.cell['parts'].append('\n')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell['parts'].append(data)

    def handle_endtag(self, tag):
        if self.depth == 1 and tag in ('td', 'th') and self.cell is not None:
            text = ''.join(self.cell.pop('parts')).replace('\xa0', ' ')
            text = '\n'.join(' '.join(line.split()) for line in text.split('\n')).strip()
            self.cell['text'] = text
            self.row.append(self.cell)
            self.cell = None
        elif self.depth == 1 and tag == 'tr' and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.tables.append(self.table)
                self.table = None


def table_grid(rows):
    occupied, result = {}, []
    for row in rows:
        previous = {col: remaining - 1 for col, remaining in occupied.items() if remaining > 1}
        cells = {col: None for col in occupied}
        col = 0
        for cell in row:
            while col in cells:
                col += 1
            cells[col] = cell['text']
            for offset in range(cell['colspan']):
                if offset:
                    cells[col + offset] = None
                if cell['rowspan'] > 1:
                    previous[col + offset] = cell['rowspan'] - 1
            col += cell['colspan']
        occupied = previous
        result.append([cells.get(i) for i in range(max(cells, default=-1) + 1)])
    return result


def read_tables(path):
    parser = TableReader()
    parser.feed(Path(path).read_bytes().decode('windows-1252', errors='replace'))
    return [table_grid(table) for table in parser.tables]


def continued_cell(previous, text):
    """Excel exports sometimes split a single visual cell across rows."""
    prior = previous.strip()
    if re.fullmatch(r'\.?\s*\(\d+\)', text.strip()):
        return bool(prior)
    rank = re.match(r'^([123])\s*\)', text)
    prior_ranks = re.findall(r'(?m)^\s*([123])\s*\)', prior)
    if rank:
        if prior_ranks:
            return int(rank.group(1)) == int(prior_ranks[-1]) + 1
        return bool(re.fullmatch(r'(?:D|H|DH)\s*[-\d\s]+:', prior, re.I))
    return bool(re.fullmatch(r'(?:D|H|DH)\s*[-\d\s]+:', prior, re.I)
                and re.match(r'^(?:keine\s+Wertung\b|nicht\s+ausgetragen\b)', text, re.I))


def extract(year, path):
    tables = read_tables(path)
    main = next((t for t in tables if t and any(
        re.search(r'LANGDISTANZ|TAG\s*-\s*OL', c or '', re.I) for c in t[0])), None)
    if not main or len(main[0]) < 5:
        raise ValueError(f'{year}: cannot locate yearly WOLV table')
    heading = main[0]
    dates = main[1] if len(main) > 1 else []
    entries = []
    last_by_column = {}
    for row_index, row in enumerate(main[2:], 2):
        label = row[0] if row else None
        if not label:
            continue
        for column in range(1, min(len(row),len(heading))):
            text = (row[column] or '').strip()
            if not text:
                continue
            previous = last_by_column.get(column)
            if previous and previous['last_row'] == row_index - 1 and continued_cell(previous['cell'], text):
                previous['cell'] += '\n' + text
                previous['last_row'] = row_index
                continue
            entry = {'page': 1, 'row': row_index, 'last_row': row_index, 'column': column,
                     'row_heading': label, 'discipline_heading': heading[column] or '',
                     'event_heading': dates[column] if column < len(dates) else '', 'cell': text}
            entries.append(entry)
            last_by_column[column] = entry
    return entries


def extract_pdf(year, path):
    """Extract the main matrix and every additional standalone relay table."""
    import pdfplumber
    entries = []
    with pdfplumber.open(path) as doc:
        for page_index, page in enumerate(doc.pages, 1):
            for table_index, table in enumerate(page.extract_tables()):
                if not table or not table[0]:
                    continue
                head = table[0]
                if str(head[0] or '').strip() == str(year) and len(head) >= 5:
                    has_date_row = len(table) > 1 and not table[1][0]
                    dates = table[1] if has_date_row else head
                    start = 2 if has_date_row else 1
                    for row_index, row in enumerate(table[start:], start):
                        if not row or not row[0]:
                            continue
                        for column in range(1, min(len(head), len(row))):
                            if row[column]:
                                entries.append({'page': page_index, 'row': row_index, 'column': column,
                                    'row_heading': row[0], 'discipline_heading': head[column] or '',
                                    'event_heading': dates[column] if column < len(dates) else '',
                                    'cell': row[column]})
                elif len(head) == 1 and re.search(r'STAFFEL', head[0] or '', re.I):
                    heading = head[0]
                    shared_notice = ''
                    for row_index, row in enumerate(table[1:], 1):
                        text = row[0] if row else None
                        notice = ''
                        if not text:
                            continue
                        # Only actual category cells; preserve a leading date
                        # as part of the heading, not as a fake category.
                        if not (re.search(r'(?m)^\s*[123]\s*\)', text)
                                or re.search(r'keine\s+wertung|nicht\s+ausgetragen', text, re.I)):
                            if text.strip().endswith(':') and shared_notice:
                                notice = shared_notice
                            else:
                                heading += '\n' + text
                                if re.search(r'nicht\s+ausgetragen', text, re.I):
                                    shared_notice = text
                                continue
                        elif not text.strip().endswith(':') and not re.search(r'(?m)^\s*[123]\s*\)', text) \
                                and not re.search(r'^[^\n]+:', text):
                            shared_notice = text
                            heading += '\n' + text
                            continue
                        entries.append({'page':page_index,'row':1000 + table_index * 100 + row_index,
                            'column':0,'row_heading':text.splitlines()[0],
                            'discipline_heading':head[0], 'event_heading':heading, 'cell':text,
                            'shared_notice':notice})
    if not entries:
        raise ValueError(f'{year}: no WOLV classifications extracted from {path}')
    return entries
