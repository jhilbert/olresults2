import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "generate_person_redirects", ROOT / "build" / "generate_person_redirects.py")
redirects = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(redirects)


def make_db(path, people, result_rows):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE person (id INTEGER PRIMARY KEY, name_key TEXT, year_of_birth INTEGER);
        CREATE TABLE stage (id INTEGER PRIMARY KEY, event_id INTEGER);
        CREATE TABLE result (
            person_id INTEGER, stage_id INTEGER, category TEXT, rank INTEGER,
            status TEXT, time_s INTEGER, source TEXT);
        INSERT INTO stage VALUES (1204, 1204);
        INSERT INTO stage VALUES (2, 2);
    """)
    con.executemany("INSERT INTO person VALUES (?,?,?)", people)
    con.executemany("INSERT INTO result VALUES (?,?,?,?,?,?,?)", result_rows)
    con.commit()
    con.close()


class PersonRedirectTests(unittest.TestCase):
    def test_excludes_bad_event_only_people_and_recovers_yob_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            previous, current, output = tmp / "old.db", tmp / "new.db", tmp / "redirects.json"
            make_db(previous, [(-1, "ghost row", None), (-2, "akira pucher", None)], [
                (-1, 1204, "Split", 1, "ok", 10, "sportsoftware-text"),
                (-2, 1204, "Split", 2, "ok", 20, "sportsoftware-text"),
                (-2, 2, "H21", 3, "ok", 600, "sportsoftware-html"),
            ])
            make_db(current, [(-999, "akira pucher", 2001)], [
                (-999, 2, "H21", 3, "ok", 600, "sportsoftware-html"),
            ])
            argv = ["generate_person_redirects.py", str(previous), str(current), str(output),
                    "--exclude-event-only", "1204"]
            with patch.object(sys, "argv", argv):
                self.assertEqual(redirects.main(), 0)
            self.assertEqual(json.loads(output.read_text()), {"-2": -999})


if __name__ == "__main__":
    unittest.main()
