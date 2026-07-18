import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from sportsoftware_common import (
    is_junk_name, parse_champion_annotation, parse_flow_row, parse_status, parse_time,
)

HTML_SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_html", ROOT / "ingest" / "parse_sportsoftware_html.py")
html_parser = importlib.util.module_from_spec(HTML_SPEC)
HTML_SPEC.loader.exec_module(html_parser)

PDF_SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_pdf", ROOT / "ingest" / "parse_sportsoftware_pdf.py")
pdf_parser = importlib.util.module_from_spec(PDF_SPEC)
PDF_SPEC.loader.exec_module(pdf_parser)

TEXT_SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_text", ROOT / "ingest" / "parse_sportsoftware_text.py")
text_parser = importlib.util.module_from_spec(TEXT_SPEC)
TEXT_SPEC.loader.exec_module(text_parser)

BUILD_SPEC = importlib.util.spec_from_file_location(
    "build_db_relay", ROOT / "build" / "build_db.py")
build_db = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build_db)


class RelayStructureTests(unittest.TestCase):
    def test_elapsed_times_over_99_minutes_are_valid(self):
        self.assertEqual(parse_time("114:08"), 6848)
        self.assertEqual(parse_time("136:54"), 8214)
        self.assertEqual(parse_time("1:54:08"), 6848)

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

    def test_structural_staffel_header_overrides_anonymous_filename(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2713-1.html"
        self.require_source_fixture(source)
        text = html_parser.decode(source.read_bytes())
        self.assertEqual(html_parser.detect_list_type("erg020619.html", text), "relay")
        categories = html_parser.parse_relay_document(text)
        masters = next(c for c in categories if c["name"].startswith("H 150-"))
        self.assertEqual(masters["declaredStarters"], 17)
        self.assertEqual(len({r["teamNumber"] for r in masters["results"]}), 17)

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
        self.assertFalse(html_parser.is_ooc_status("Slovakia"))
        self.assertTrue(html_parser.is_ooc_status("A K"))
        self.assertTrue(html_parser.is_ooc_status("nc"))
        source = ROOT / "data" / "raw" / "anne" / "files" / "3474-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_bracket_html(html_parser.decode(source.read_bytes()))
        men_50 = next(c for c in categories if c["name"] == "Men 50+")
        milan = next(r for r in men_50["results"] if r["name"] == "Milan Beles")
        self.assertEqual((milan["timeText"], milan["status"]), ("DNS", "dns"))

    def test_legacy_ak_prefix_and_omt_are_normalized(self):
        parsed = parse_flow_row(
            "AK 1 Kerschbaumer Gernot vereinslos 14:22", {"vereinslos": "vereinslos"})
        self.assertEqual(parsed["names"], ["Kerschbaumer Gernot"])
        self.assertIsNone(parsed["rank"])
        self.assertTrue(parsed["outOfCompetition"])
        self.assertEqual(parse_status("OMT"), "dns")

        mp = parse_flow_row(
            "AK 725 Belzik Karl vereinslos Fehlst", {"vereinslos": "vereinslos"})
        [mp_result] = pdf_parser.flow_results(mp)
        self.assertTrue(mp_result["outOfCompetition"])
        self.assertEqual(mp_result["status"], "mp")

    def test_fixed_width_ak_is_orthogonal_to_status(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "842-0.html"
        self.require_source_fixture(source)
        text = text_parser.extract_pre_blocks(text_parser.decode(source.read_bytes()))
        categories = text_parser.parse_text(text)
        h14 = next(c for c in categories if c["name"] == "Herren -14")
        allwinger = next(r for r in h14["results"] if r["name"] == "Allwinger Herwig jun.")
        self.assertTrue(allwinger["outOfCompetition"])
        self.assertEqual(allwinger["status"], "ok")

    def test_ampersand_champion_annotation_carries_rank_to_html_result(self):
        self.assertEqual(
            parse_champion_annotation("2 & österreichischer Meister"),
            (2, "ÖM"),
        )
        source = ROOT / "data" / "raw" / "anne" / "files" / "1829-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        h14 = next(c for c in categories if c["name"] == "Herren -14")
        deubel = next(r for r in h14["results"] if r["name"] == "Deubel Jonas")
        self.assertEqual((deubel["rank"], deubel["championship"]), (2, "ÖM"))

    def test_split_champion_marker_repairs_shifted_fixed_width_row(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4304-0.txt"
        self.require_source_fixture(source)
        categories = text_parser.parse_text(text_parser.decode(source.read_bytes()))

        m14 = next(c for c in categories if c["name"] == "M -14")
        borsitzky = next(r for r in m14["results"] if r["name"] == "Borsitzky Felix")
        self.assertEqual((borsitzky["rank"], borsitzky["championship"]), (4, "ÖM"))
        self.assertEqual(borsitzky["club"], "HSV OL Wiener Neustad")

        w14 = next(c for c in categories if c["name"] == "W-14")
        sandrisser = next(r for r in w14["results"] if r["name"] == "Sandrisser Hannah")
        self.assertEqual((sandrisser["rank"], sandrisser["championship"]), (2, "ÖM"))
        self.assertEqual(sandrisser["club"], "Naturfreunde Villach")

    def test_pdf_page_header_fragments_are_not_runners(self):
        self.assertTrue(is_junk_name("Orientierungslauf-Club"))
        self.assertTrue(is_junk_name("Austria Cup"))
        self.assertTrue(is_junk_name("Mittel MS"))
        self.assertTrue(is_junk_name("NOLV Schulcup Ternitz Wed"))

    def test_abbreviated_zwischenzeit_attachment_is_skipped(self):
        self.assertEqual(
            html_parser.detect_list_type("event_896_3SchulC13ZwZeit.html", ""),
            "overall",
        )

    def test_bracket_layout_consumes_ak_placement_cell(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2633-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_bracket_html(html_parser.decode(source.read_bytes()))
        women = next(c for c in categories if c["name"] == "Damen A")
        hedi = next(r for r in women["results"] if r["name"] == "Hedi Berger")
        self.assertEqual(hedi["club"], "Orienteering Klosterneuburg")
        self.assertEqual((hedi["timeText"], hedi["status"]), ("24:22", "ok"))
        self.assertTrue(hedi["outOfCompetition"])

    def test_trailing_disqu_overrides_recorded_time(self):
        self.assertEqual(parse_status("Disqu"), "dsq")
        source = ROOT / "data" / "raw" / "anne" / "files" / "2254-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        men = next(c for c in categories if c["name"] == "Herren 2.Klasse")
        holzer = next(r for r in men["results"] if r["name"] == "Holzer Patrick")
        self.assertEqual((holzer["timeS"], holzer["status"]), (1908, "dsq"))

    def test_alphanumeric_bib_is_not_part_of_pdf_runner_name(self):
        parsed = parse_flow_row(
            "2 AUT59 Erik Simkovics OLC Wienerwald 17:39",
            {"olc wienerwald": "OLC Wienerwald"},
        )
        self.assertEqual(parsed["rank"], 2)
        self.assertEqual(parsed["names"], ["Erik Simkovics"])

    def test_flowing_pdf_carries_separate_champion_rank(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4011-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        h45 = next(c for c in categories if c["name"] == "H 45-")
        schmid = next(r for r in h45["results"] if r["name"] == "SCHMID Michael")
        self.assertEqual(schmid["rank"], 1)

    def test_exact_time_ties_inherit_the_printed_shared_rank(self):
        con = sqlite3.connect(":memory:")
        con.execute("""CREATE TABLE result (
            id INTEGER PRIMARY KEY, result_list_id TEXT, rank INTEGER,
            status TEXT, time_s INTEGER, out_of_competition INTEGER,
            result_kind TEXT, observed_rank TEXT)""")
        con.executemany("INSERT INTO result VALUES (?,?,?,?,?,?,?,?)", [
            (1, "list", 9, "ok", 1186, 0, "individual", "9"),
            (2, "list", None, "ok", 1186, 0, "individual", None),
            (3, "list", None, "ok", 1186, 0, "individual", None),
            (4, "list", None, "ok", 1200, 0, "individual", None),
        ])
        self.assertEqual(build_db.normalize_tied_individual_ranks(con.cursor()), 2)
        rows = con.execute("SELECT rank, observed_rank FROM result ORDER BY id").fetchall()
        self.assertEqual(rows, [(9, "9"), (9, None), (9, None), (None, None)])

    def test_score_ties_inherit_rank_even_with_different_times(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "5371-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        results = categories[0]["results"]
        daniel = next(r for r in results if r["name"] == "Daniel Bichl")
        anna = next(r for r in results if r["name"] == "Anna Skern")
        bela = next(r for r in results if r["name"] == "Bela Kiss")
        self.assertEqual((daniel["rank"], daniel["scoreText"]), (1, "71 Posten"))
        self.assertEqual((anna["rank"], anna["scoreText"]), (40, "19 Posten"))
        self.assertEqual((bela["rank"], bela["scoreText"]), (24, "30 Posten"))

    def test_bracketed_ak_time_wins_over_the_later_behind_value(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4220-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_bracket_html(html_parser.decode(source.read_bytes()))
        h65 = next(c for c in categories if c["name"] == "H65-")
        tim = next(r for r in h65["results"] if r["name"] == "Tim Skern")
        self.assertEqual((tim["timeText"], tim["timeS"], tim.get("rank")), ("(39:58)", 2398, None))
        self.assertTrue(tim["outOfCompetition"])

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
