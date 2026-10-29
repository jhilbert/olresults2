"""Verify the browser's Vienna medal threshold against actual SQLite data."""
import sqlite3
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ViennaStarterMinimumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.count_query = subprocess.check_output(
            ["node", "-p", "require('./site/domain_rules.js').regionalStarterCountsSql()"],
            cwd=ROOT, text=True,
        )

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE championship_entry
              (id TEXT PRIMARY KEY, championship_instance_id TEXT, eligibility_state TEXT);
            CREATE TABLE championship_entry_result
              (championship_entry_id TEXT, result_id INTEGER);
            CREATE TABLE result
              (id INTEGER PRIMARY KEY, status TEXT, out_of_competition INTEGER);
        """)
        self.next_id = 1

    def add_entry(self, instance, statuses, eligibility="eligible", ooc=0):
        entry = f"entry-{self.next_id}"
        self.db.execute("INSERT INTO championship_entry VALUES (?,?,?)",
                        (entry, instance, eligibility))
        for status in statuses:
            result_id = self.next_id
            self.next_id += 1
            self.db.execute("INSERT INTO result VALUES (?,?,?)", (result_id, status, ooc))
            self.db.execute("INSERT INTO championship_entry_result VALUES (?,?)", (entry, result_id))

    def counts(self):
        return dict(self.db.execute(self.count_query))

    def test_two_finishers_and_one_dnf_or_mp_meet_minimum(self):
        for instance, other_status in (("dnf", "dnf"), ("mp", "mp"), ("dsq", "dsq")):
            self.add_entry(instance, ["ok"])
            self.add_entry(instance, ["ok"])
            self.add_entry(instance, [other_status])
        self.assertEqual(self.counts(), {"dnf": 3, "mp": 3, "dsq": 3})

    def test_one_finisher_and_two_dnf_or_mp_meet_minimum(self):
        for other_status in ("dnf", "mp"):
            self.add_entry(other_status, ["ok"])
            self.add_entry(other_status, [other_status])
            self.add_entry(other_status, [other_status])
        self.add_entry("mixed", ["ok"])
        self.add_entry("mixed", ["dnf"])
        self.add_entry("mixed", ["mp"])
        self.assertEqual(self.counts(), {"dnf": 3, "mp": 3, "mixed": 3})

    def test_dns_unknown_ak_and_ineligible_do_not_establish_eligible_start(self):
        self.add_entry("one", ["ok"])
        self.add_entry("one", ["dns"])
        self.add_entry("one", ["unknown"])
        self.add_entry("one", ["ok"], ooc=1)
        self.add_entry("one", ["ok"], eligibility="ineligible")
        self.assertEqual(self.counts(), {"one": 1})

    def test_two_people_in_pair_count_once_and_teams_with_mp_count(self):
        self.add_entry("pairs", ["ok", "ok"])
        self.add_entry("pairs", ["ok", "ok"])
        self.add_entry("pairs", ["mp", "mp"])
        self.assertEqual(self.counts(), {"pairs": 3})

    def test_one_or_two_starters_do_not_meet_minimum(self):
        self.add_entry("beck", ["ok"])
        self.add_entry("two", ["ok"])
        self.add_entry("two", ["dnf"])
        self.assertEqual(self.counts(), {"beck": 1, "two": 2})


if __name__ == "__main__":
    unittest.main()
