"""Regression checks against the preserved complete ÖM team result."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
import parse_archived_2017


class HistoricalTeamResults(unittest.TestCase):
    def test_original_pdf_team_units_and_members(self):
        categories = {c["name"]: c for c in parse_archived_2017.document()["categories"]}
        self.assertEqual(sum(c["sourceUnitCount"] for c in categories.values()), 84)
        elite = categories["D 19-"]["results"]
        winners = [r for r in elite if r.get("teamNumber") == "15"]
        self.assertEqual([r["name"] for r in winners], ["Anja Arbter", "Anika Gassner", "Barbara Kastner"])
        self.assertTrue(all(r["rank"] == 1 and r["championship"] == "ÖM" for r in winners))
        self.assertTrue(all(r["timeS"] == 4955 and "leg" not in r for r in winners))
        self.assertEqual(next(r for r in elite if r["name"] == "Ursula Kadan")["outOfCompetition"], True)
        junior = categories["H -14"]["results"]
        self.assertEqual(next(r for r in junior if r["name"] == "Jonas Deubel")["yearOfBirth"], 2003)
        maria = next(r for r in junior if r["name"] == "Maria Beck")
        self.assertEqual(maria["yearOfBirth"], 2004)
        self.assertTrue(maria["strictYearOfBirth"])
        self.assertEqual(categories["D -14"]["sourceUnitCount"], 1)
        self.assertFalse(any(r.get("championship") for r in categories["D -14"]["results"]))

    def test_reproducible_normalization(self):
        import json
        self.assertEqual(json.loads(parse_archived_2017.OUTPUT.read_text()), parse_archived_2017.document())


if __name__ == "__main__":
    unittest.main()
