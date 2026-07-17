import importlib.util
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_db", ROOT / "build" / "build_db.py")
build_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_db)


class AnneIdentityTests(unittest.TestCase):
    def test_user_id_normalizes_numeric_strings(self):
        self.assertEqual(build_db.anne_user_id("9910"), 9910)
        self.assertEqual(build_db.anne_user_id(9910), 9910)
        self.assertIsNone(build_db.anne_user_id(None))
        self.assertIsNone(build_db.anne_user_id("not-an-id"))

    def test_relay_members_keep_authoritative_anne_ids(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        persons = build_db.PersonRegistry()
        team = {
            "teamMembers": [
                {"userId": "9910", "firstName": "Jara", "lastName": "Leonhardt",
                 "yearOfBirth": 2014, "nationality": "AUT", "leg": 1,
                 "classification": "classified", "overall": {"time": 600}},
                {"userId": "9787", "firstName": "Mira", "lastName": "Veitsberger",
                 "yearOfBirth": 2013, "nationality": "AUT", "leg": 2,
                 "classification": "classified", "overall": {"time": 1100}},
            ],
            "teamName": "Leonhardt/Veitsberger",
            "clubName": "OC Fürstenfeld",
            "categoryTitle": "D14",
            "rank": 1,
            "classification": "classified",
        }

        inserted = build_db.insert_anne_relay(con.cursor(), persons, 1, "D14", team)

        self.assertEqual(inserted, 2)
        self.assertEqual(set(persons.by_id), {9910, 9787})
        self.assertEqual(
            con.execute("SELECT person_id, time_s FROM result ORDER BY person_id").fetchall(),
            [(9787, 500), (9910, 600)],
        )
        self.assertEqual(persons.by_id[9910][2:], (2014, "AUT", None))


if __name__ == "__main__":
    unittest.main()
