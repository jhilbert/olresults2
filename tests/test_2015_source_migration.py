import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'build'))
SPEC = importlib.util.spec_from_file_location('verify_2015_source_migration', ROOT / 'build/verify_2015_source_migration.py')
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class MigrationAuditTests(unittest.TestCase):
    def test_detects_missing_duplicate_but_allows_the_approved_events(self):
        con = sqlite3.connect(':memory:')
        con.execute("ATTACH DATABASE ':memory:' AS previous")
        for db in ('main', 'previous'):
            con.execute(f'CREATE TABLE {db}.stage(id INTEGER, event_id INTEGER, number INTEGER)')
            con.execute(f'''CREATE TABLE {db}.result(stage_id INTEGER, source TEXT, category TEXT,
                observed_name TEXT, observed_club TEXT, observed_rank TEXT, observed_time TEXT, observed_status TEXT)''')
            con.executemany(f'INSERT INTO {db}.stage VALUES (?,?,?)', [(1, 4000, 1), (2, 1378, 1)])
        row = (1, 'html', 'D14', 'Runner', 'Club', '1', '20:00', 'ok')
        con.executemany('INSERT INTO previous.result VALUES (?,?,?,?,?,?,?,?)', [row, row, (2, 'html', 'D14', 'Removed', '', '2', '', 'ok')])
        con.execute('INSERT INTO main.result VALUES (?,?,?,?,?,?,?,?)', row)
        self.assertEqual(audit.lost_observations(con), 1)
        con.execute('INSERT INTO main.result VALUES (?,?,?,?,?,?,?,?)', row)
        self.assertEqual(audit.lost_observations(con), 0)


if __name__ == '__main__':
    unittest.main()
