import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))

HTML_SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_html", ROOT / "ingest" / "parse_sportsoftware_html.py")
html_parser = importlib.util.module_from_spec(HTML_SPEC)
HTML_SPEC.loader.exec_module(html_parser)

PDF_SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_pdf", ROOT / "ingest" / "parse_sportsoftware_pdf.py")
pdf_parser = importlib.util.module_from_spec(PDF_SPEC)
PDF_SPEC.loader.exec_module(pdf_parser)

BUILD_SPEC = importlib.util.spec_from_file_location(
    "build_db_relay", ROOT / "build" / "build_db.py")
build_db = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build_db)


class RelayStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.relay_source = ROOT / "data" / "raw" / "anne" / "files" / "4480-0.html"
        if not cls.relay_source.exists():
            cls.category = None
            return
        categories = html_parser.parse_relay_document(
            html_parser.decode(cls.relay_source.read_bytes()))
        cls.category = next(c for c in categories if c["name"] == "Mixed Staffel ab 35")

    @classmethod
    def require_source_fixture(cls, path):
        if not path.exists():
            raise unittest.SkipTest(
                f"source fixture not present locally: {path.name}; "
                "the raw ANNE cache is intentionally not committed")

    def test_source_starter_count_means_teams_not_member_status_groups(self):
        self.require_source_fixture(self.relay_source)
        self.assertEqual(self.category["declaredStarters"], 14)
        self.assertEqual(len(self.category["results"]), 42)
        self.assertEqual(len({r["teamNumber"] for r in self.category["results"]}), 14)

    def test_team_status_propagates_and_individual_cause_is_preserved(self):
        self.require_source_fixture(self.relay_source)
        by_number = {}
        for result in self.category["results"]:
            by_number.setdefault(result["teamNumber"], []).append(result)

        self.assertEqual({r["status"] for r in by_number["209"]}, {"mp"})
        self.assertEqual(
            {r["name"]: r["individualStatus"] for r in by_number["209"]},
            {"Hannes Kolar": "ok", "Natalia Machold": "mp", "Sandra Ujvari": "ok"},
        )
        self.assertEqual({r["status"] for r in by_number["214"]}, {"dsq"})
        self.assertEqual(
            {r["name"]: r["individualStatus"] for r in by_number["214"]},
            {"Uwe Sandrisser": "dsq", "Lisi Sandrisser": "ok", "Michael Hohenwarter": "mp"},
        )
        self.assertEqual({r["status"] for r in by_number["212"]}, {"dsq"})
        self.assertEqual([r["leg"] for r in by_number["212"]], [1, 2, 3])
        self.assertEqual({r["legCount"] for r in by_number["212"]}, {3})

    def test_audit_unit_key_ignores_member_status(self):
        rows = [
            (1, "relay", None, "ok", 998, "Naturfreunde Wien 2", "", "209", "Naturfreunde Wien 2"),
            (2, "relay", None, "mp", None, "Naturfreunde Wien 2", "", "209", "Naturfreunde Wien 2"),
            (3, "relay", None, "ok", 1415, "Naturfreunde Wien 2", "", "209", "Naturfreunde Wien 2"),
        ]
        self.assertEqual(len({build_db.competitor_unit_key(r) for r in rows}), 1)

    def test_short_ak_status_does_not_match_slovakia(self):
        self.assertIsNone(html_parser.parse_status("Slovakia"))
        source = ROOT / "data" / "raw" / "anne" / "files" / "3474-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_bracket_html(html_parser.decode(source.read_bytes()))
        men_50 = next(c for c in categories if c["name"] == "Men 50+")
        milan = next(r for r in men_50["results"] if r["name"] == "Milan Beles")
        self.assertEqual((milan["timeText"], milan["status"]), ("DNS", "dns"))

    def test_relay_ak_is_preserved_for_every_member(self):
        self.require_source_fixture(self.relay_source)
        categories = html_parser.parse_relay_document(
            html_parser.decode(self.relay_source.read_bytes()))
        category = next(c for c in categories if c["name"] == "Mixed Staffel bis 16")
        by_number = {}
        for result in category["results"]:
            by_number.setdefault(result["teamNumber"], []).append(result)

        for team_number in ("106", "119"):
            self.assertEqual(len(by_number[team_number]), 3)
            self.assertEqual(
                {r["outOfCompetition"] for r in by_number[team_number]}, {True})
            self.assertEqual({r["status"] for r in by_number[team_number]}, {"ok"})

    def test_relay_team_label_keeps_source_club_and_safe_official_mapping(self):
        source_club = build_db.source_club_for_team(
            "FUN-OL NÖ 2", "FUN-OL NÖ 2", "relay")
        self.assertEqual(source_club, "FUN-OL NÖ")
        self.assertEqual(
            build_db.canonicalize_official_club(source_club, build_db.OFFICIAL_CLUBS),
            "FUN.O NOe",
        )
        self.assertEqual(
            build_db.canonicalize_official_club("LZ Omaha", build_db.OFFICIAL_CLUBS),
            "LZ OMAHA",
        )
        # A retired ÖFOL club stays a distinct historical club; it must not
        # be remapped to Orienteering Imst Oberland or Laufklub Kompass.
        historic = "Orienteering Innsbruck Imst"
        self.assertEqual(
            build_db.canonicalize_official_club(historic, build_db.OFFICIAL_CLUBS),
            historic)

    def test_mannschaft_pdf_has_one_unit_per_team_without_page_chrome(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3507-1.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        category = next(c for c in categories if c["name"] == "Herren ab 19")
        results = category["results"]

        self.assertEqual(category["declaredStarters"], 13)
        self.assertEqual(len(results), 39)
        self.assertEqual(len({r["teamName"] for r in results}), 13)
        self.assertFalse(any(
            token in r["name"] for r in results
            for token in ("Seite", "SportSoftware", "Krämer")
        ))

        by_team = {}
        for result in results:
            by_team.setdefault(result["teamName"], []).append(result)
        self.assertEqual(set(by_team), {
            "SU Klagenfurt 1", "OLC Graz 1", "Naturfreunde Wien 1",
            "WAT-OL 1", "OC Fürstenfeld 1", "WAT-OL 2",
            "ASKÖ Henndorf Orientee 1", "SU Schöckl Orienteering 1",
            "OLT Transdanubien 1", "HSV Pinkafeld 1",
            "ASKÖ Henndorf Orientee 2", "HSV OL Wiener Neustad 1",
            "Naturfreunde Wien 2",
        })
        self.assertEqual(
            {r["name"] for r in by_team["SU Klagenfurt 1"]},
            {"Binder Martin", "Schgaguler Klaus", "Meizer Felix"},
        )
        self.assertEqual(
            {r["status"] for r in by_team["Naturfreunde Wien 1"]}, {"ok"})
        self.assertEqual(
            {r["status"] for r in by_team["Naturfreunde Wien 2"]}, {"dns"})
        self.assertEqual(
            {r["teamTimeS"] for r in by_team["WAT-OL 2"]}, {5952})
        self.assertTrue(all("leg" not in r for r in results))


if __name__ == "__main__":
    unittest.main()
