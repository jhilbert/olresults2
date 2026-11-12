"""Source-backed reconciliation of the 2021 Vienna/ASKÖ result PDF."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
import parse_vienna_2021


class Vienna2021Results(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document, cls.splits, cls.ranks = parse_vienna_2021.parse_document()

    def test_all_source_rows_preserved_once(self):
        self.assertEqual(len(self.document["categories"]), 15)
        self.assertEqual(sum(len(c["results"]) for c in self.document["categories"]), 96)
        self.assertNotIn("ASKÖ MS Damen 19-", self.splits)
        self.assertNotIn("ASKÖ MS Herren 19-", self.splits)

    def test_published_age_splits_and_ranks(self):
        self.assertEqual(self.ranks["D50-"]["BONEK Claudia"], 1)
        self.assertEqual(self.ranks["D45-"]["HINTERPLATTNER Herlinde"], 1)
        self.assertEqual(self.ranks["H50-"]["WALDHÄUSL Wolfgang"], 1)
        self.assertEqual(self.ranks["H45-"]["BALLIK Thomas"], 1)
        self.assertEqual(self.ranks["H70-"]["BONEK Ernst"], 1)
        self.assertEqual(self.ranks["H70-"]["KOCHMANN Günther"], 5)
        senior = next(c for c in self.document["categories"] if c["name"] == "ASKÖ MS Herren 65- + Herren 75-")
        self.assertEqual(next(r for r in senior["results"] if r["name"] == "CZEMBIREK Heinz")["status"], "dsq")
        self.assertNotIn("rank", next(r for r in senior["results"] if r["name"] == "CZEMBIREK Heinz"))
        self.assertIn("STÖCHER Jacopo", self.splits["ASKÖ MS Herren 15-18"]["H15-18"])
        self.assertIn("ZETTL Karin", self.splits["ASKÖ MS Damen 45-"]["__unassigned__"])

    def test_evidence_is_reproducible(self):
        result = subprocess.run([sys.executable, str(ROOT / "ingest/parse_vienna_2021.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        evidence = json.loads((ROOT / "data/review/regional_source_classes_3325.json").read_text())
        self.assertEqual(evidence["events"]["3325"]["jurisdictions"]["WIEN"]["result_document_url"],
                         self.document["sourceUrl"])


if __name__ == "__main__":
    unittest.main()
