#!/usr/bin/env python3
"""Structure a pinned ÖFOL Meistertafel snapshot without joining OLResults.

One title is a source award; a team has multiple source members. Every source
row remains inspectable, including headings and rows that cannot be classified.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from parse_oefol_meistertafel import parse, SOURCE_URL

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'data/review/archived_sources/oefol/meistertafel-2026-11-22.html'
OUT = ROOT / 'site/oefol/meistertafel.json'
SOURCE_SHA256 = '0bd215192f8c15da76062c1c47144a52f488af812e3de910728e58130651cb7d'
SNAPSHOT_2026 = ROOT / 'data/review/archived_sources/oefol/meistertafel-2026-11-25.html'
SHA256_2026 = 'c0423643da0e44dcfb117f8d2547065b255201002628b0b1c3420576ed92d0ea'
CLASS = re.compile(r'^(?:D|H|DH|D/H|H/D|H\+D|D\+H|Mixed)\s*(?:[-–]?\d|Elite|Allg)', re.I)
TITLE = re.compile(r'(?:staats)?meister', re.I)
SECTION = re.compile(r'^(?:ol|mtb[ -]?o|ski[ -]?o(?:l)?)(?:\s+\d{4})?$', re.I)
FORMATS = re.compile(r'^(?:sprint(?:-ol)?|mittel(?:distanz)?|lang(?:distanz|strecken-ol)?|ol[ -]lang(?:distanz)?|tag-ol|ol|nacht(?:-ol)?|staffel|mannschaft|mixed.*staffel|knock.?out(?: sprint)?|massenstart|verfolgung|ultralang|(?:ol|marathon)[ -]marathon|marathon-ol|mtb[ -]?o.*|ski[ -]?o(?:l)?.*)$', re.I)
EMPTY = re.compile(r'^(?:(?:\d{4}\s+)?nicht\s+(?:vergeben|ausgetragen)|keine(?:r|s)?\s+wertung|kein(?:e|er)?\s+meister(?:in)?)$', re.I)

# The 2021/2022 source prints these team members without any separator.
# Explicitly reviewed strings, not a general two-words-per-person assumption:
# names such as "Rose Marie Reindl" must remain a single person.
UNDELIMITED_TEAMS = '''
Cleo Machold|Matilda Buschek|Anna Skern
Jonas Ochenbauer|David Kaltenbacher|Tobias Kaltenbacher
Maria Beck|Mika Asenbauer|Sona Asenbauer
Linus Dobler|Nikolas Hinterplattner|Oskar Hinterplattner
Claudia Bonek|Barbara Kastner|Marina Skern
Pierre Kaltenbacher|Markus Plohn|Stephan Seeböck
Ingrid Adenstedt|Elisabeth Knapp|Hedi Berger
Emanuel Braun|Georg Wittberger|Michael Stockmayer
Ernst Bonek|Roland Arbter|Peter Bonek
Camilla Aigmüller|Charlotte Brischnik|Moritz Greiner
Thomas Hudax|Karin Leonhardt|Matthias Leonhardt
Peter Bonek|Barbara Kastner|Ferri Gassner
Mika Asenbauer|Sona Asenbauer|Anna Skern
Lukas Frohner|Michael Fuchs|Florian Kummerer
Anika Gassner|Tina Tiefenböck|Lina Buchberger
Martin Binder|Klaus Schgaguler|Felix Meizer
Barbara Kastner|Claudia Bonek|Marina Skern
Bernd Bäuchler|Pierre Kaltenbacher|Markus Plohn
Günther Kroupa|Wolfgang Waldhäusl|Boris Kastner-Jirka
Roland Arbter|Josef Zapletal|Frederic Genevois
Hannah Winkler|Laura König|Flora Aigmüller
Marina Skern|Christine Calvet|Barbara Kastner
Manuela Hlosta|Riki Tiefenböck|Vera Arbter
Jacopo Stöcher|Kiara-Sophie Piskorz|Simon Ballik
Jasmina Gassner|Nicolas Kastner|Jannis Bonek|Ylvi Kastner
Pierre Kaltenbacher|Anita Seeböck|Markus Plohn
Wolfgang Waldhäusl|Barbara Kastner|Ferri Gassner
Charlotte Brischnik|Eva Monsberger|Carola Koren
David Kaltenbacher|Jonas Ochenbauer|Niklas Ochenbauer
Jasmina Gassner|Tina Tiefenböck|Ylvi Kastner
Leon Ebster|Robert Merl|Christian Wartbichler
Christine Calvet|Marina Skern|Barbara Kastner
Jürgen Egger|Herwig Allwinger|Andreas Pölzl
Martin Brantner|Michael Stockmayer|Georg Wittberger
Claus Hermann|Valentin Pidner|Franz Ponweiser
'''
TEAM_NAMES = {' '.join(line.split('|')): line.split('|')
              for line in UNDELIMITED_TEAMS.strip().splitlines()}
# The source put the first part of the club into the name cell.
TEAM_NAMES['Pierre Kaltenbacher Markus Plohn HSV'] = ['Pierre Kaltenbacher', 'Markus Plohn']


def discipline(label, current=None):
    text = label.lower().replace(' ', '').replace('-', '')
    if text.startswith('mtbo'):
        return 'MTBO'
    if text.startswith('skio'):
        return 'Ski-O'
    if text == 'ol' or re.fullmatch(r'ol\d{4}', text):
        return 'OL'
    return current


def team_format(fmt):
    return bool(re.search(r'staffel|mannschaft', fmt or '', re.I))


def cells(row):
    return [c['text'] for c in row['cells']]


def member_names(value, fmt):
    value = value.strip().strip(',').strip()
    if team_format(fmt):
        value = re.sub(r'(?<=\w)\.\s+(?=[A-ZÄÖÜ][a-zäöüß]+\s)', ', ', value)
    pieces = [part.strip() for part in re.split(r'\s*,\s*|\s+und\s+|\s+/\s+', value) if part.strip()]
    if len(pieces) > 1 and (team_format(fmt) or re.search(r'nacht', fmt or '', re.I)
                            or all(len(p.split()) >= 2 for p in pieces)):
        return [name for piece in pieces for name in TEAM_NAMES.get(piece, [piece])]
    if team_format(fmt) and value in TEAM_NAMES:
        return TEAM_NAMES[value][:]
    return [value] if value else []


def gender_from_source(title, category, fmt=None):
    cat = (category or '').strip()
    if re.search(r'mixed', fmt or '', re.I) or re.match(r'^(?:DH|D/H|H/D|H\+D|D\+H|Mixed)', cat, re.I) or re.search(r'Meister(?:Innen|\*innen)', title):
        return 'mixed'
    if re.match(r'^D(?:\s|\d|-)', cat, re.I): return 'female'
    if re.match(r'^H(?:\s|\d|-)', cat, re.I): return 'male'
    if re.search(r'meisterin', title, re.I): return 'female'
    if re.search(r'meister', title, re.I) and 'meisterschaft' not in title.lower(): return 'male'
    return None


def structure(raw, expected_sha=SOURCE_SHA256, only_year=None):
    parsed = parse(raw)
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('Source snapshot changed: review and repin before replacing the data')
    awards, source_rows = [], []
    for table in parsed['tables']:
        if only_year is not None and table['year'] != only_year:
            continue
        year, disc, fmt, current, section_conflict = table['year'], None, None, None, None
        rowspans = {}
        for row in table['rows']:
            raw_cells = cells(row)
            # Some historical HTML declares a rowspan that extends into the
            # next explicit title (e.g. 2014 Mannschaft). A real new title is
            # authoritative; do not prepend stale inherited cells to it.
            if raw_cells and len(raw_cells) >= 3 and (TITLE.search(raw_cells[0]) or CLASS.match(raw_cells[0])):
                rowspans.clear()
            # Expand HTML rowspans while keeping the original cells below.
            expanded, col = [], 0
            for cell in row['cells']:
                while col in rowspans:
                    value, remaining = rowspans[col]
                    expanded.append(value)
                    rowspans[col] = (value, remaining - 1)
                    if remaining == 1: del rowspans[col]
                    col += 1
                expanded.append(cell['text'])
                span = int(cell.get('rowspan', '1'))
                if span > 1: rowspans[col] = (cell['text'], span - 1)
                col += 1
            while col in rowspans:
                value, remaining = rowspans[col]
                expanded.append(value)
                rowspans[col] = (value, remaining - 1)
                if remaining == 1: del rowspans[col]
                col += 1
            source_id = f"{table['table_id']}:{row['source_row']}"
            rec = {'id':source_id, 'year':year, 'cells':row['cells'], 'kind':'unclassified', 'award_id':None}
            source_rows.append(rec)
            x = expanded + [''] * (8 - len(expanded))
            first = x[0].strip()
            if first == 'Bearbeiten':
                rec['kind'] = 'editorial'; continue
            if first == str(year) and not any(x[1:]):
                rec['kind'] = 'year_heading'; continue
            # Spreadsheet-shaped 1991–94 rows and 1995–98 rows.
            tabular8 = first == str(year) and x[2] in ('Meisterschaft','Staatsmeisterschaft')
            tabular6 = first in ('Meisterschaft','Staatsmeisterschaft') and bool(x[1])
            if tabular8 or tabular6:
                if tabular8:
                    disc0, title0, format0, cat, given, family, club = x[1:8]
                    d = discipline(disc0)
                else:
                    title0, format0, cat, given, family, club = x[:6]
                    d = discipline(format0, disc or 'OL')
                state = 'ÖSTM' if title0 == 'Staatsmeisterschaft' else 'ÖM'
                # A combined surname+club in a single cell cannot be split safely.
                ambiguous = bool(given and family and not club and len(family.split()) > 1)
                name = ' '.join(v for v in (given, family) if v).strip()
                if not name or EMPTY.match(name):
                    rec['kind']='no_named_winner'; current=None; continue
                current = {'id':source_id, 'year':year, 'discipline':d, 'format':format0 or None,
                    'category':cat or None, 'title_type':state, 'title_text':title0,
                    'gender':gender_from_source(title0, cat, format0),
                    'club':club or None, 'members':[], 'source_rows':[source_id],
                    'notes':['name_or_club_ambiguous'] if ambiguous else []}
                current['printed_names'] = name
                names = member_names(name, format0)
                current['members'].extend({'name':n.strip(), 'source_row':source_id} for n in names if n.strip())
                awards.append(current); rec.update(kind='award',award_id=source_id); continue
            # Section headings may carry a contradictory source year. Preserve it,
            # but do not let an apparent typo silently overwrite table year.
            nonempty = [v for v in raw_cells if v]
            if SECTION.fullmatch(first) and len(nonempty) == 1:
                found_year = re.search(r'\b(?:19|20)\d{2}\b', first)
                if found_year or disc is None:
                    disc, fmt, current = discipline(first, disc), (first if first.lower() == 'ol' else None), None
                    rec['kind'] = 'discipline_heading'
                    section_conflict = first if found_year and int(found_year.group()) != year else None
                    if section_conflict: rec['year_conflict'] = True
                    continue
            if first and FORMATS.fullmatch(first) and any(re.search(r'\babgesagt\b', v, re.I) for v in x[1:]):
                disc, fmt, current = discipline(first, disc or 'OL'), first, None
                rec['kind'] = 'cancelled_format'; continue
            header = bool(first and FORMATS.fullmatch(first) and
                          (len(nonempty) == 1 or (len(x)>1 and x[1].lower() == 'kategorie')))
            if header:
                disc, fmt, current = discipline(first, disc or 'OL'), first, None
                rec['kind'] = 'format_heading'; continue
            # The page sometimes contains presentation labels without a result.
            if first and TITLE.search(first) and not any(x[1:]):
                rec['kind'] = 'section_heading'; current = None; continue
            if (current and len(raw_cells) < len(expanded) and raw_cells
                    and not TITLE.search(raw_cells[0]) and not CLASS.match(raw_cells[0])
                    and TITLE.search(first)):
                name = x[2].strip()
                if name and not EMPTY.match(name):
                    current['members'].extend({'name':n, 'source_row':source_id,
                                               'club':x[3] or None} for n in member_names(name, fmt))
                    current['source_rows'].append(source_id)
                    rec.update(kind='continuation',award_id=current['id']); continue
            is_title = bool(TITLE.search(first))
            is_class = bool(CLASS.match(first))
            if is_title or is_class:
                title_text = first
                title_type = 'ÖSTM' if re.search('staatsmeister', first, re.I) else 'ÖM'
                if is_class:
                    cat = first
                    # Historical layouts use first name, surname, club in columns 2–4.
                    if x[2] and x[3]: name, club = ' '.join((x[1],x[2])).strip(), x[3]
                    elif x[1] and x[3]: name, club = x[1], x[3]
                    else: name, club = x[1], x[2] or None
                else:
                    cat = x[1]
                    # Older state champion tables omit the category column:
                    # title | given name | surname | club (plus empty cells).
                    # Do not interpret a given name as an age category.
                    omitted_category = bool(re.search(r'staatsmeister', first, re.I)
                        and cat and not re.search(r'\d|elite|klasse|damen|herren|mixed', cat, re.I)
                        and x[2] and x[3] and not x[4])
                    if omitted_category:
                        name, club, cat = ' '.join((x[1], x[2])), x[3], ''
                    if not cat:
                        match = re.search(r'\b((?:bis|ab)\s*\d+|\d+\s+bis\s+\d+)',first,re.I)
                        cat = match.group(1) if match else ''
                    # 2025 uses separate given/surname columns; prior 5-cell
                    # layouts usually leave the final cell empty or use it for notes.
                    if omitted_category:
                        pass
                    elif len(expanded) == 5 and x[4] and x[4].lower() not in ('(ex aequo)','ex aequo'):
                        if x[3]: name, club = ' '.join(v for v in (x[2],x[3]) if v), x[4]
                        else: name, club = x[2], x[4]
                    else: name, club = x[2], x[3]
                if not name or EMPTY.match(name) or EMPTY.match(' '.join(v for v in (name,club) if v)):
                    rec['kind']='no_named_winner'; current=None; continue
                current = {'id':source_id, 'year':year, 'discipline':disc, 'format':fmt,
                    'category':cat or None, 'title_type':title_type, 'title_text':title_text,
                    'gender':gender_from_source(title_text, cat, fmt),
                    'club':club or None, 'members':[], 'source_rows':[source_id], 'notes':[]}
                if is_class: current['notes'].append('title_type_from_category_table')
                if section_conflict: current['notes'].append('section_year_conflict:' + section_conflict)
                # Team lists use commas/"und" for distinct members. Retain the
                # complete printed string as well as the reversible split.
                pieces = member_names(name, fmt)
                # Some cells repeat the separately recorded club after the
                # winner's name, including in ex-aequo rows. Strip only an
                # exact matching club suffix, never infer a club from a name.
                if club:
                    suffix = ' ' + club.strip()
                    cleaned = [p[:-len(suffix)].strip() if p.endswith(suffix)
                               and len(p[:-len(suffix)].split()) >= 2 else p for p in pieces]
                    if cleaned != pieces:
                        current['notes'].append('club_repeated_in_name_cell')
                    pieces = cleaned
                current['members'] = [{'name':p.strip(), 'source_row':source_id} for p in pieces if p.strip()]
                if year == 2026 and len(pieces) == 1 and len(expanded) == 5 and x[2] and x[3] and x[4]:
                    current['members'][0]['display_name'] = f'{x[2]} {x[3].upper()}'
                current['printed_names'] = name
                if year == 2026 and club and club.endswith(' (nicht in der Ergebnisliste)'):
                    current['source_club'] = club
                    current['club'] = club.removesuffix(' (nicht in der Ergebnisliste)')
                    current['notes'].append('source_note:nicht in der Ergebnisliste')
                if source_id == 'tablepress-9:row-232' and not club and name == 'Maria Hauser':
                    # The club's own 2024 season report identifies this exact
                    # Ski-O D15-17 sprint championship as a Naturfreunde Kitzbühel title.
                    current['source_club'] = None
                    current['club'] = 'Naturfreunde Kitzbühel'
                    current['club_evidence_url'] = 'https://kitzbuehel.naturfreunde.at/berichte/2024/bericht-ski-orientierung-2024/'
                    current['notes'].append('club_from_corroborating_source')
                if source_id == 'tablepress-26:row-186' and club == 'NF V,' and name == 'Helena Reinisch':
                    current['source_club'] = club
                    current['club'] = 'NF Villach'
                    current['notes'].append('club_inferred_from_user_confirmation')
                if source_id == 'tablepress-15:row-278' and club == 'Kitzbühel' and name == 'Lisa KRIMBACHER':
                    current['source_club'] = club
                    current['club'] = 'NF Kitzbühel'
                    current['notes'].append('club_inferred_same_person_same_season_and_sport')
                if source_id == 'tablepress-26:row-80' and club == 'NF Villach' and name == 'Vera Arbter':
                    current['source_club'] = club
                    current['club'] = 'NF Wien'
                    current['notes'].append('club_inferred_same_person_same_season_and_sport')
                if source_id == 'tablepress-10:row-106' and name.endswith('Plohn HSV') and club == 'OL Wr. Neustadt':
                    current['club'] = 'HSV OL Wr. Neustadt'
                    current['notes'].append('club_split_across_name_and_club_cells')
                awards.append(current); rec.update(kind='award',award_id=source_id); continue
            # Multi-row teams and rowspan-based ex-aequo winners.
            continuation = None
            continuation_club = None
            if current:
                if len(raw_cells) < len(expanded) and len(raw_cells) <= 2:
                    continuation = raw_cells[0] if raw_cells else None
                elif not first and x[2]:
                    continuation = x[2]
                    continuation_club = x[3] or None
                elif first and len(nonempty) <= 2 and not FORMATS.fullmatch(first):
                    continuation = first
                    if (len(nonempty) == 1 and re.match(r'^(?:Naturfreunde|NF\b|OLC\b|HSV\b|OC\b|ASKÖ\b|SU\b|WAT\b)', first, re.I)
                            and current['members'] and not current['members'][-1].get('club')):
                        if not current['club'] and len(current['members']) > 1:
                            current['club'] = first
                            current['notes'].append('club_from_team_continuation')
                        else:
                            current['members'][-1]['club'] = first
                        current['source_rows'].append(source_id)
                        rec.update(kind='club_continuation',award_id=current['id']); continue
                    if len(nonempty) == 2 and not current['club']:
                        current['club'] = nonempty[1]; current['notes'].append('club_from_team_continuation')
            if continuation and not EMPTY.match(continuation):
                current['members'].extend({**{'name':n, 'source_row':source_id},
                                           **({'club':continuation_club} if continuation_club else {})}
                                          for n in member_names(continuation, fmt))
                current['source_rows'].append(source_id)
                rec.update(kind='continuation',award_id=current['id']); continue
    return {'schema_version':1,'source_url':SOURCE_URL,'source_sha256':expected_sha,
            'source_snapshot':'data/review/archived_sources/oefol/meistertafel-2026-11-22.html',
            'scope':'Quellenabschrift; keine OLResults-Personenverknüpfung oder automatische Vergabe.',
            'official_clubs':json.loads((ROOT / 'data/official_clubs.json').read_text()),
            'awards':awards,'source_rows':source_rows}


def combined_data():
    original = structure(SNAPSHOT.read_bytes())
    latest = structure(SNAPSHOT_2026.read_bytes(), SHA256_2026, only_year=2026)
    if not latest['awards'] or any(a['year'] != 2026 for a in latest['awards']):
        raise ValueError('2026 source section missing or invalid')
    if any(r['kind'] == 'unclassified' for r in latest['source_rows']):
        raise ValueError('Unclassified 2026 source rows require review')
    original['awards'] = latest['awards'] + original['awards']
    original['source_rows'] = latest['source_rows'] + original['source_rows']
    original['additional_sources'] = [{'year':2026, 'source_url':SOURCE_URL,
        'source_snapshot':str(SNAPSHOT_2026.relative_to(ROOT)), 'source_sha256':SHA256_2026}]
    return original


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--check',action='store_true'); args=parser.parse_args()
    data=combined_data()
    text=json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n'
    if args.check:
        if not OUT.is_file() or OUT.read_text()!=text: raise SystemExit('Structured source differs')
    else:
        OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(text)
    print(f"{len(data['awards'])} source awards, {sum(len(x['members']) for x in data['awards'])} names, "
          f"{sum(x['kind']=='unclassified' for x in data['source_rows'])} unclassified source rows")


if __name__=='__main__': main()
