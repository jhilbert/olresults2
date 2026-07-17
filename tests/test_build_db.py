import importlib.util
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_db", ROOT / "build" / "build_db.py")
build_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_db)


class AnneIdentityTests(unittest.TestCase):
    def test_synthetic_ids_are_deterministic_and_identity_scoped(self):
        first = build_db.PersonRegistry().from_legacy("Anna Beispiel", 1988)
        second = build_db.PersonRegistry().from_legacy("Anna Beispiel", 1988)
        different_yob = build_db.PersonRegistry().from_legacy("Anna Beispiel", 1989)
        self.assertLess(first, 0)
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_yob)

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
        self.assertEqual(
            con.execute(
                "SELECT observed_user_id, identity_basis, identity_confidence "
                "FROM result ORDER BY person_id").fetchall(),
            [("9787", "anne-user-id", 1.0), ("9910", "anne-user-id", 1.0)],
        )

    def test_verified_members_survive_a_crossed_anne_name_and_user_id(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        persons = build_db.PersonRegistry()

        persons.from_anne(1644, "Christian Arbter", None, None, None)
        persons.record(1644, "Christian Arbter", authoritative=True)
        persons.from_anne(3682, "Christian Arbter", None, None, None)
        persons.record(3682, "Christian Arbter", authoritative=True)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=3682, category="H21", status="ok",
            source="anne-api", observed_name="Christian Arbter",
            observed_user_id="3682", identity_basis="anne-user-id",
            identity_confidence=1.0,
        )

        synthetic_sabine = persons.from_legacy("Sabine Jandl", None)
        persons.record(synthetic_sabine, "Sabine Jandl")
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=synthetic_sabine, category="D45",
            status="ok", source="anne-api", observed_name="Sabine Jandl",
            identity_basis="legacy-name", identity_confidence=0.55,
        )
        persons.from_anne(3682, "Sabine Jandl", None, None, None)
        persons.record(3682, "Sabine Jandl", authoritative=True)

        members = [
            {"ofol_id": 1644, "name": "Christian Arbter",
             "name_key": build_db.name_key("Christian Arbter"), "yob": 1990},
            {"ofol_id": 3682, "name": "Sabine Jandl",
             "name_key": build_db.name_key("Sabine Jandl"), "yob": 1969},
        ]
        corrections = build_db.prepare_verified_member_identities(
            con.cursor(), persons, members)
        merges = build_db.duplicate_identity_merge_edges(
            persons, (m["ofol_id"] for m in members))

        self.assertEqual(persons.by_id[1644][:3],
                         ("Christian Arbter", "arbter christian", 1990))
        self.assertEqual(persons.by_id[3682][:3],
                         ("Sabine Jandl", "jandl sabine", 1969))
        self.assertEqual(merges.get(synthetic_sabine), 3682)
        self.assertNotIn(3682, merges)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(
            con.execute(
                "SELECT person_id, observed_user_id, identity_basis "
                "FROM result WHERE observed_user_id = '3682'").fetchone(),
            (1644, "3682", "club-book-of-record"),
        )

    def test_verified_member_id_wins_over_a_duplicate_anne_account(self):
        persons = build_db.PersonRegistry()
        persons.from_anne(3000, "Ada Beispiel", 1985, None, None)
        persons.from_anne(5000, "Ada Beispiel", 1985, None, None)

        merges = build_db.duplicate_identity_merge_edges(persons, {5000})

        self.assertEqual(merges, {3000: 5000})

    def test_disjoint_roster_name_repairs_only_the_crossed_source_row(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        persons = build_db.PersonRegistry()
        persons.from_anne(8520, "Kathrin Kollndorfer", 1981, None, None)
        persons.record(8520, "Kathrin Kollndorfer", authoritative=True)
        persons.from_anne(10344, "Kathrin Kollndorfer", 1981, None, None)
        persons.record(10344, "Kathrin Kollndorfer", authoritative=True)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=10344, category="D40", status="ok",
            source="anne-api", observed_name="Kathrin Kollndorfer",
            observed_user_id="10344", identity_basis="anne-user-id",
            identity_confidence=1.0,
        )
        members = [{
            "ofol_id": 10344, "name": "Jean-Baptiste Le Blanc",
            "name_key": build_db.name_key("Jean-Baptiste Le Blanc"), "yob": 1981,
        }]

        corrections = build_db.prepare_verified_member_identities(
            con.cursor(), persons, members)
        merges = build_db.duplicate_identity_merge_edges(persons, {10344})

        self.assertEqual(con.execute("SELECT person_id FROM result").fetchone(), (8520,))
        self.assertEqual(persons.by_id[10344][0], "Jean-Baptiste Le Blanc")
        self.assertNotIn(8520, merges)
        self.assertNotIn(10344, merges)
        self.assertEqual(corrections[0]["assigned_person_id"], 8520)


if __name__ == "__main__":
    unittest.main()
