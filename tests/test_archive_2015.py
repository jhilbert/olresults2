import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        source = next(item for item in archive.SOURCES if item[0] == 1424)
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
        sources = [item for item in archive.SOURCES if item[0] == 1425]
        national, cup = (archive.document(source[0], *source[2:]) for source in sources)
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

    def test_2015_wien_and_noe_mixed_relay_keeps_teams_and_leg_times(self):
        source = next(item for item in archive.SOURCES if item[0] == 1386)
        doc = archive.document(source[0], *source[2:])
        self.assertEqual(sum(c["sourceUnitCount"] for c in doc["categories"]), 72)
        self.assertEqual(sum(len(c["results"]) for c in doc["categories"]), 216)
        youth = next(c for c in doc["categories"] if c["name"] == "-16 W")
        winners = [r for r in youth["results"] if r.get("rank") == 1]
        self.assertEqual([r["name"] for r in winners], ["Jakob Wolfram", "Tina Tiefenböck", "Erik Bonek"])
        self.assertEqual([r["timeS"] for r in winners], [535, 575, 525])
        self.assertEqual([r["leg"] for r in winners], [1, 2, 3])
        self.assertEqual({r["teamTimeS"] for r in winners}, {1635})
        missing = next(r for c in doc["categories"] for r in c["results"] if r["name"] == "John Shelvin")
        self.assertEqual(missing["individualStatus"], "unknown")

    def test_2015_mixed_relay_rescan_preserves_reviewed_normalization(self):
        source = next(item for item in archive.SOURCES if item[0] == 1386)
        data = (archive.ARCHIVE / source[2]).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, files, out = root / "raw", root / "files", root / "normalized"
            raw.mkdir()
            (raw / "attachments.json").write_text(json.dumps({"1386": [{"url":source[6],
                "fileName":source[4], "mimeType":"text/html"}]}))
            with patch.object(sportsoftware, "RAW", raw), patch.object(sportsoftware, "FILES", files), \
                    patch.object(sportsoftware, "OUT", out), patch.object(sportsoftware, "fetch", return_value=data), \
                    patch.object(sys, "argv", ["parse", "--event-id", "1386"]):
                self.assertEqual(sportsoftware.main(), 0)
            self.assertEqual((out / "1386-0.json").read_bytes(),
                             (ROOT / "data/normalized/1386-0.json").read_bytes())

    def test_2015_wien_and_noe_junior_senior_results_complete(self):
        source = next(item for item in archive.SOURCES if item[0] == 1368)
        doc = archive.document(source[0], *source[2:])
        self.assertEqual(len(doc["categories"]), 33)
        self.assertEqual(sum(len(c["results"]) for c in doc["categories"]), 239)
        for category in doc["categories"]:
            self.assertEqual(len(category["results"]), category["declaredStarters"], category["name"])
        d14 = next(c for c in doc["categories"] if c["name"] == "D 13-14 Wien")
        self.assertEqual(d14["results"][0]["name"], "Tiefenböck Rita")
        self.assertEqual(d14["results"][0]["timeS"], 1880)


if __name__ == "__main__":
    unittest.main()
