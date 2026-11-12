import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
import parse_archived_2015 as archive
import parse_sportsoftware_html as sportsoftware


class Archive2015Tests(unittest.TestCase):
    def test_archive_normalization_is_reproducible(self):
        for event, index, name, date, label, kind, url in archive.SOURCES:
            expected = json.loads((ROOT / "data/normalized" / f"{event}-{index}.json").read_text())
            self.assertEqual(archive.document(event, name, date, label, kind, url), expected)

    def test_relay_winners_keep_club_legs_and_championship(self):
        source = archive.SOURCES[1]
        doc = archive.document(source[0], *source[2:])
        women = next(c for c in doc["categories"] if c["name"] == "Damen 19- Elite")
        team = [r for r in women["results"] if r.get("teamName") == "Naturfreunde Wien 1"]
        self.assertEqual({r["name"] for r in team}, {"Anika Gassner", "Vera Arbter", "Anja Arbter"})
        self.assertEqual({r["rank"] for r in team}, {2})
        self.assertEqual({r["championship"] for r in team}, {"ÖSTM"})
        self.assertEqual({r["leg"] for r in team}, {1, 2, 3})
        youth = next(c for c in doc["categories"] if c["name"] == "Herren -12")
        max_pietsch = next(r for r in youth["results"] if r["name"] == "Max Pietsch")
        self.assertEqual(max_pietsch["yearOfBirth"], 2005)

    def test_national_classification_is_a_subset_of_the_full_cup(self):
        national, cup = (archive.document(source[0], *source[2:]) for source in archive.SOURCES[2:])
        overall = {c["name"]: c for c in cup["categories"]}
        self.assertEqual(len(national["categories"]), 27)
        self.assertEqual(sum(len(c["results"]) for c in national["categories"]), 369)
        for category in national["categories"]:
            self.assertEqual({r["name"] for r in category["results"]},
                             {r["name"] for r in overall[category["name"]]["results"]})

    def test_official_historical_winner_evidence_is_source_pinned(self):
        data = json.loads((ROOT / "data/review/official_historical_champions.json").read_text())
        winners = [entry for entry in data["titles"] if entry["event_id"] == 1424]
        self.assertEqual({entry["category"] for entry in winners},
                         {"Damen -14", "Damen -18", "Herren -12", "Herren -16", "Herren -18", "Damen 50-"})
        self.assertTrue(all(len(entry["person_ids"]) == 3 and entry["team_time_s"]
                            and entry["result_url"].startswith("https://web.archive.org/")
                            for entry in winners))

    def test_2016_team_results_reproduce_the_preserved_source(self):
        snapshot = ROOT / "data/review/archived_sources/2016/1812-0.html"
        normalized = json.loads((ROOT / "data/normalized/1812-0.json").read_text())
        self.assertEqual(sportsoftware.parse_team_document(sportsoftware.decode(snapshot.read_bytes())),
                         normalized["categories"])
        self.assertEqual(normalized["snapshotPath"], snapshot.relative_to(ROOT).as_posix())
        self.assertEqual(len(normalized["categories"]), 11)
        self.assertEqual(sum(c["sourceUnitCount"] for c in normalized["categories"]), 109)


if __name__ == "__main__":
    unittest.main()
