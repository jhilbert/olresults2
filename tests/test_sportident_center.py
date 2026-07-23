import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingest"))
SPEC = importlib.util.spec_from_file_location(
    "parse_sportident_center",
    ROOT / "ingest" / "parse_sportident_center.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SportidentCenterParserTest(unittest.TestCase):
    def test_normalizes_rank_seconds_mp_year_and_course(self):
        payload = {
            "date": "2021-06-19",
            "title": "1. KOLV Cup Mitteldistanz",
            "categories": [{
                "category": "H19-",
                "declared": 2,
                "course": "5.0 km / 275 m / 22 controls / 2 athletes",
                "rows": [
                    {"rank": "1", "name": "David Rapotz (2004)",
                     "club": "Naturfreunde Villach - Orienteering",
                     "time": "55:27", "behind": ""},
                    {"rank": "-", "name": "Daniel Gotthardt (1998)",
                     "club": "HSV Spittal / Drau",
                     "time": "MP", "behind": ""},
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text(json.dumps(payload))
            doc = MODULE.normalize_snapshot(
                3438, {"url": "https://center.sportident.com/results/x",
                       "fileName": "event-3438-sportident-center.json"},
                snapshot)

        category = doc["categories"][0]
        self.assertEqual(category["courseLengthM"], 5000)
        self.assertEqual(category["courseClimbM"], 275)
        self.assertEqual(category["courseControls"], 22)
        self.assertEqual(category["sourceUnitCount"], 2)
        self.assertEqual(category["results"][0]["name"], "David Rapotz")
        self.assertEqual(category["results"][0]["yearOfBirth"], 2004)
        self.assertEqual(category["results"][0]["timeS"], 3327)
        self.assertEqual(category["results"][1]["status"], "mp")
        self.assertIsNone(category["results"][1]["rank"])
        self.assertNotIn("timeS", category["results"][1])

    def test_committed_kolv_snapshot_is_complete(self):
        snapshot = (
            ROOT / "data" / "raw" / "anne" / "files" /
            "3438-0.sportident.json")
        doc = MODULE.normalize_snapshot(
            3438,
            {"url": "https://center.sportident.com/results/orienteering/"
                    "sportunion-klagenfurt-orientierungslauf/2021/"
                    "event-1-kolv-cup-mitteldistanz/overview/1",
             "fileName": "event-3438-sportident-center.json"},
            snapshot)

        self.assertEqual(len(doc["categories"]), 16)
        self.assertEqual(
            sum(len(category["results"]) for category in doc["categories"]),
            48)
        self.assertEqual(
            sum(result["status"] == "mp"
                for category in doc["categories"]
                for result in category["results"]),
            4)
        self.assertEqual(
            sum(result.get("rank") is not None
                for category in doc["categories"]
                for result in category["results"]),
            44)


if __name__ == "__main__":
    unittest.main()
