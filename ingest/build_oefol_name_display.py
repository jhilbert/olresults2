#!/usr/bin/env python3
"""Build a presentation overlay, never overwrite Meistertafel source values."""
import json
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def key(value):
    return unicodedata.normalize('NFC', ' '.join(value.split())).casefold()

def build(awards, users, confirmations, corrections=None):
    spellings = defaultdict(set)
    for user in users:
        first, last = user.get('first_name'), user.get('last_name')
        if first and last:
            spellings[key(first + ' ' + last)].add((first, last))
    formats = {}
    for award in awards:
        for member in award['members']:
            name = member['name']
            candidates = spellings.get(key(name), set())
            if len(candidates) == 1:
                first, last = next(iter(candidates))
                formats[name] = {'first_name': first, 'last_name': last}
    # Display formatting only: retain the source's historical surname.
    formats['Helga Kolb'] = {'first_name': 'Helga', 'last_name': 'Kolb'}
    rows = {(m['source_row'],m['name']): m for a in awards for m in a['members']}
    ids = {u['oefol_id']: u for u in users}
    for c in confirmations:
        member = rows.get((c['source_row'],c['source_name']))
        user = ids.get(c['oefol_id']) if c.get('oefol_id') else None
        if not member or member['name'] != c['source_name']:
            raise ValueError(f"Unpassende Quellzeile: {c['source_row']}")
        if c.get('oefol_id') and (not user or (user['first_name'],user['last_name']) != (c['first_name'],c['last_name'])):
            raise ValueError(f"Unpassender Registereintrag: {c['oefol_id']}")
    corrections = corrections or []
    confirmed_rows = {(c['source_row'], c['source_name']) for c in confirmations}
    for c in corrections:
        identity = (c['source_row'], c['source_name'])
        if identity not in rows or identity in confirmed_rows:
            raise ValueError(f"Unpassende oder doppelte Quellkorrektur: {identity}")
    return {'schema_version':1,'formats':formats,'confirmations':confirmations,'corrections':corrections}

def main():
    awards=json.loads((ROOT/'site/oefol/meistertafel.json').read_text())['awards']
    users=json.loads((ROOT/'data/raw/anne/user_index.json').read_text())['users']
    confirmed=json.loads((ROOT/'data/review/oefol_meistertafel_person_confirmations.json').read_text())['confirmations']
    corrections=json.loads((ROOT/'data/review/oefol_meistertafel_name_corrections.json').read_text())['corrections']
    result=build(awards,users,confirmed,corrections)
    (ROOT/'site/oefol/person-display.json').write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n')
    print(f"{len(result['formats'])} formatierte Quellnamen, {len(confirmed)} dokumentierte Ergänzungen")

if __name__ == '__main__': main()
