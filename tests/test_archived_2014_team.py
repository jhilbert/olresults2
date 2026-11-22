import sys
import sqlite3
import importlib.util
import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ingest'))
import parse_archived_2014_team as source
spec = importlib.util.spec_from_file_location('build_db_2014_team', ROOT / 'build/build_db.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class Archived2014TeamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = source.document()

    def test_exact_source_and_normalized_snapshot(self):
        self.assertEqual(self.doc['eventId'], 1076)
        self.assertEqual(len(self.doc['categories']), 13)
        self.assertEqual(sum(c['sourceUnitCount'] for c in self.doc['categories']), 118)
        self.assertEqual(source.OUTPUT.read_text(), __import__('json').dumps(self.doc, ensure_ascii=False))
        exposed_pdf = ROOT / 'site/sources/1076-oem-mannschaft-2014.pdf'
        self.assertEqual(hashlib.sha256(exposed_pdf.read_bytes()).hexdigest(), source.SHA256)

    def test_six_wien_champion_teams_and_unawarded_class(self):
        winners = {(c['name'], r['teamNumber']) for c in self.doc['categories'] for r in c['results']
                   if r.get('championship') == 'ÖM' and r['club'] == 'Naturfreunde Wien'}
        self.assertEqual(winners, {('H 15-18','55'), ('H 50-','94'), ('D -14','47'),
                                   ('D 15-18','65'), ('D 40-','83'), ('D 50-','104')})
        d60 = next(c for c in self.doc['categories'] if c['name'] == 'D 60-')
        self.assertTrue(all('championship' not in r for r in d60['results']))
        self.assertFalse(any(r['name'].lower() == 'vakant' for c in self.doc['categories'] for r in c['results']))

    def test_user_confirmed_roster_maps_only_exact_event_observations(self):
        profiles = build.AnneProfileIndex([
            {'oefol_id':6741, 'first_name':'Florian', 'last_name':'Kurz', 'year_of_birth':1997},
            {'oefol_id':3258, 'first_name':'Dominik', 'last_name':'Jandl', 'year_of_birth':1998},
            {'oefol_id':6783, 'first_name':'Clemens', 'last_name':'Wolfram', 'year_of_birth':1999},
        ])
        registry = build.PersonRegistry(profiles)
        for name, year, pid in [('Florian Kurz',1998,6741), ('Dominik Jandl',1997,3258), ('Clemens Wolfram',1996,6783)]:
            self.assertEqual(registry.confirmed_event_identity(1076, 'H 15-18', 'team', name, year, 'Naturfreunde Wien'), pid)
            self.assertIsNone(registry.confirmed_event_identity(1076, 'H 15-18', 'team', name, year, 'OLC Graz'))
            self.assertIsNone(registry.confirmed_event_identity(1077, 'H 15-18', 'team', name, year, 'Naturfreunde Wien'))

    def test_katharina_keeps_source_year_but_does_not_match_on_it(self):
        category = next(c for c in self.doc['categories'] if c['name'] == 'Offen')
        row = next(r for r in category['results'] if r['name'] == 'Katharina Gittmaier')
        self.assertEqual((row['teamNumber'], row['club'], row['sourceYearPrinted']), ('115', 'HSV Ried', '89'))
        self.assertNotIn('yearOfBirth', row)
        self.assertNotIn('strictYearOfBirth', row)
        self.assertNotIn('championship', row)

    def test_confirm_only_published_winners_without_resolving_identities(self):
        con = sqlite3.connect(':memory:')
        con.executescript('''CREATE TABLE stage(id INTEGER,event_id INTEGER,number INTEGER);
            CREATE TABLE source_document(id TEXT,source_url TEXT,snapshot_sha256 TEXT);
            CREATE TABLE national_minimum_starters(stage_id INTEGER,category TEXT,state TEXT);
            CREATE TABLE official_national_title(result_id INTEGER PRIMARY KEY,source_url TEXT,
              invitation_url TEXT,invitation_sha256 TEXT,evidence_note TEXT);
            CREATE TABLE result(id INTEGER,stage_id INTEGER,source_document_id TEXT,
              category TEXT,team_number TEXT,observed_name TEXT,result_kind TEXT,rank INTEGER,
              status TEXT,out_of_competition INTEGER,championship TEXT,championship_eligibility_state TEXT,
              note TEXT,national_rank INTEGER,championship_source_scope TEXT,identity_state TEXT);''')
        con.execute('INSERT INTO stage VALUES (1,1076,1)')
        con.execute('INSERT INTO source_document VALUES (?,?,?)', ('source', self.doc['sourceUrl'], source.SHA256))
        rid = 0
        for category in self.doc['categories']:
            winners = [r for r in category['results'] if r.get('championship') == 'ÖM']
            if not winners:
                continue
            con.execute('INSERT INTO national_minimum_starters VALUES (1,?,?)', (category['name'], 'unresolved'))
            for row in winners:
                rid += 1
                con.execute('INSERT INTO result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (rid, 1, 'source', category['name'], row['teamNumber'], row['name'], 'team', 1,
                     'ok', 0, 'ÖM', 'eligible', row['note'], None, 'inferred', 'candidate'))
        build.confirm_2014_team_source_winners(con.cursor())
        self.assertEqual(con.execute('SELECT COUNT(*) FROM result WHERE national_rank=1 AND identity_state=\'candidate\'').fetchone()[0], 33)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM official_national_title').fetchone()[0], 33)
        con.execute("UPDATE source_document SET snapshot_sha256='changed'")
        with self.assertRaises(ValueError):
            build.confirm_2014_team_source_winners(con.cursor())


if __name__ == '__main__':
    unittest.main()
