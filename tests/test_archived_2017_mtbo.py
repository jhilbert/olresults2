import sys
import sqlite3
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ingest'))
import parse_archived_2017_mtbo as source
spec = importlib.util.spec_from_file_location('build_db_2017_mtbo', ROOT / 'build/build_db.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class Archived2017MtboTests(unittest.TestCase):
    def test_confirm_two_youth_winners_only_and_fail_closed(self):
        con = sqlite3.connect(':memory:')
        con.executescript('''CREATE TABLE stage(id INTEGER,event_id INTEGER,number INTEGER);
            CREATE TABLE result(id INTEGER,stage_id INTEGER,category TEXT,observed_name TEXT,
              observed_nation TEXT,status TEXT,out_of_competition INTEGER,championship TEXT,
              championship_eligibility_state TEXT,note TEXT,national_rank INTEGER,
              championship_source_scope TEXT);
            CREATE TABLE official_national_title(result_id INTEGER,source_url TEXT);''')
        con.execute('INSERT INTO stage VALUES (3,1865,3)')
        url = 'http://www.mtbodays.at/mtbodays2017/live/all/s3_OEM_mtbodays_erg.html'
        for rid, category, name in ((1, 'Women 14', 'Schnetz Lena'), (2, 'Men 17', 'Kratky Jakob')):
            con.execute('INSERT INTO result VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (rid, 3, category, name, 'AUT', 'ok', 0, 'ÖM', 'eligible',
                         'Originalquelle: Österreichischer Meister', None, 'full_field'))
            con.execute('INSERT INTO official_national_title VALUES (?,?)', (rid, url))
        con.execute('INSERT INTO result VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (3, 3, 'Men 17', 'Koffler Georg', 'AUT', 'ok', 0, 'ÖM',
                     'eligible', None, None, 'full_field'))
        build.confirm_2017_mtbo_youth_champions(con.cursor())
        self.assertEqual(con.execute('SELECT id,national_rank,championship_source_scope FROM result ORDER BY id').fetchall(),
                         [(1, 1, 'winner_only'), (2, 1, 'winner_only'), (3, None, 'full_field')])
        con.execute("UPDATE official_national_title SET source_url='https://wrong.example' WHERE result_id=1")
        with self.assertRaises(ValueError):
            build.confirm_2017_mtbo_youth_champions(con.cursor())

    def test_champion_annotation_preserves_international_place(self):
        sprint, long = (source.document(spec) for spec in source.SOURCES)
        for document, rank in ((sprint, 2), (long, 3)):
            women = next(c for c in document['categories'] if c['name'] == 'Women 40')
            winner = next(r for r in women['results'] if r['name'] == 'Kastner Barbara')
            self.assertEqual(winner['rank'], rank)
            self.assertEqual(winner['sourceNat'], 'AUT')
            self.assertIn('Österreichische Meisterin', winner['note'])
            self.assertNotIn('championship', winner)
        self.assertEqual(sprint['docDate'], '2017-07-15')
        self.assertEqual(long['docDate'], '2017-07-16')

    def test_awards_require_both_reviewed_complete_sources(self):
        con = sqlite3.connect(':memory:')
        con.executescript('''CREATE TABLE stage(id INTEGER,event_id INTEGER,number INTEGER);
            CREATE TABLE source_document(id TEXT,snapshot_sha256 TEXT,source_url TEXT,normalized_sha256 TEXT);
            CREATE TABLE official_national_title(result_id INTEGER PRIMARY KEY,source_url TEXT,
              invitation_url TEXT,invitation_sha256 TEXT,evidence_note TEXT);
            CREATE TABLE ineligible_starter(event_id INTEGER,person_id INTEGER);
            CREATE TABLE result(id INTEGER,stage_id INTEGER,source_document_id TEXT,
              note TEXT,status TEXT,rank INTEGER,out_of_competition INTEGER,
              championship TEXT,national_rank INTEGER,championship_eligibility_state TEXT,
              championship_eligibility_basis TEXT,championship_source_scope TEXT,
              category TEXT,observed_nation TEXT,person_id INTEGER);''')
        rid = 0
        for _, number, _, digest, _ in source.SOURCES:
            count = {1: 14, 3: 12}[number]
            con.execute('INSERT INTO stage VALUES (?,?,?)', (number, 1865, number))
            normalized = {1: '448019399ba72d8ef82c7a5bff471ae340896d4ad4dbe559fe3c9f764d8ed6f9',
                          3: 'fa695d9053fb020bc7acd27f51ddc6f2c9f4474e33ba1ca90725710ca6d9d870'}[number]
            con.execute('INSERT INTO source_document VALUES (?,?,?,?)', (str(number), digest, 'https://example.test/result', normalized))
            for i in range(count):
                rid += 1
                note = 'Originalquelle: Österreichischer Staatsmeister' if i == 0 else 'Originalquelle: Österreichische Meisterin'
                con.execute('INSERT INTO result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (rid, number, str(number), note, 'ok', i + 2, 0, None, None, None, None, None, f'c{i}', 'AUT', rid))
            rid += 1
            con.execute('INSERT INTO result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (rid, number, str(number), None, 'ok', 1, 0, 'ÖM', 1, None, None, None, 'c0', 'CZE', rid))
        self.assertEqual(build.apply_2017_mtbo_source_champions(con.cursor()), 26)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM result WHERE national_rank=1").fetchone()[0], 26)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM result WHERE championship='ÖSTM'").fetchone()[0], 2)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM result WHERE national_rank IS NOT NULL AND note IS NULL").fetchone()[0], 0)
        con.execute("UPDATE source_document SET snapshot_sha256='changed' WHERE id='1'")
        with self.assertRaises(ValueError):
            build.apply_2017_mtbo_source_champions(con.cursor())


if __name__ == '__main__':
    unittest.main()
