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

    def test_mannschaft_text_switches_to_compact_individual_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2236-0.txt"
        self.require_source_fixture(source)
        categories = text_parser.parse_text(text_parser.decode(source.read_bytes()))

        long = next(c for c in categories if c["name"] == "Einzel Lang")
        self.assertEqual(len(long["results"]), 7)
        lauri = next(r for r in long["results"] if r["name"] == "Lauri Pekka")
        self.assertEqual((lauri["club"], lauri["timeS"], lauri["status"]),
                         ("Keravan Urheiljat (FIN)", 4437, "ok"))

        family = next(c for c in categories if c["name"] == "Family")
        self.assertEqual(len(family["results"]), 7)
        self.assertEqual({r.get("resultKind") for r in family["results"]}, {"family"})
        self.assertEqual(next(r for r in family["results"]
                             if r["name"] == "Urbanek Annina")["status"], "dns")

    def test_english_html_columns_keep_times_and_unranked_statuses(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1524-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        women = next(c for c in categories if c["name"] == "W21E")
        self.assertEqual(len(women["results"]), women["declaredStarters"])
        marina = next(r for r in women["results"] if r["name"] == "Reiner Marina")
        primus = next(r for r in women["results"] if r["name"] == "Primus Eva")
        self.assertEqual((marina["timeS"], marina["status"]), (5682, "ok"))
        self.assertEqual(primus["status"], "dnf")

        classic = ROOT / "data" / "raw" / "anne" / "files" / "992-0.html"
        self.require_source_fixture(classic)
        classic_categories = text_parser.parse_text(text_parser.extract_pre_blocks(
            html_parser.decode(classic.read_bytes())))
        short = next(c for c in classic_categories if c["name"] == "Open Kurz")
        self.assertEqual((short["declaredStarters"], len(short["results"])), (17, 17))

    def test_score_category_with_clock_duration_starts_a_new_class(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1971-0.html"
        self.require_source_fixture(source)
        text = text_parser.extract_pre_blocks(text_parser.decode(source.read_bytes()))
        categories = text_parser.parse_text(text)
        plain_d = next(c for c in categories if c["name"] == "D")
        school_d = next(c for c in categories if c["name"] == "E Schüler Dame")
        self.assertEqual((len(plain_d["results"]), len(school_d["results"])), (1, 6))

    def test_pair_unit_count_uses_roster_for_same_club_statuses(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1067-1.html"
        self.require_source_fixture(source)
        text = text_parser.extract_pre_blocks(html_parser.decode(source.read_bytes()))
        categories = text_parser.parse_text(text)
        women = next(c for c in categories if c["name"] == "Ew")
        self.assertEqual(build_db.normalized_source_unit_count(women["results"]),
                         women["declaredStarters"])

    def test_clubless_bracket_dns_stays_visible_but_outside_start_count(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4915-1.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_bracket_html(html_parser.decode(source.read_bytes()))
        basic = next(c for c in categories if c["name"] == "Lyceum basic")
        self.assertEqual(len(basic["results"]), 19)
        self.assertEqual(build_db.normalized_source_unit_count(basic["results"]), 8)
        dns = [r for r in basic["results"] if r["status"] == "dns"]
        self.assertEqual(len(dns), 11)
        self.assertTrue(all(r.get("excludedFromDeclaredCount") for r in dns))

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
        self.assertTrue(is_junk_name("Etappe 2 Rosegg/St.Lambrecht"))

    def test_compact_not_started_status_is_a_real_result_value(self):
        dns = parse_flow_row(
            "Josef Polster HSV Spittal n.ang.",
            {"hsv spittal": "HSV Spittal"},
        )
        self.assertTrue(pdf_parser.valid_flow(dns))
        self.assertEqual(parse_status(dns["statusText"]), "dns")
        self.assertEqual(parse_status("teilg."), "ok")

    def test_abbreviated_zwischenzeit_attachment_is_skipped(self):
        self.assertEqual(
            html_parser.detect_list_type("event_896_3SchulC13ZwZeit.html", ""),
            "overall",
        )
        self.assertEqual(
            html_parser.detect_list_type("mixedrelay-seestadt-ergbahnen.html", ""),
            "overall",
        )
        self.assertEqual(
            html_parser.detect_list_type(
                "event-4428-erg210924si.pdf",
                "Strallegg Weekend\nZwischenzeiten Ergebnis - 8. AC & ÖM Lang",
                True,
            ),
            "overall",
        )
        self.assertEqual(
            html_parser.detect_list_type(
                "MCUP2014-OVERALL-2.html",
                "MTBO Hungarian Cup - Overall results",
                False,
            ),
            "overall",
        )
        self.assertEqual(
            html_parser.detect_list_type(
                "austria-cup-wertung.html",
                "Waldviertel Festival - Gesamt-Ergebnis",
                False,
            ),
            "overall",
        )

    def test_fixed_width_time_before_region_column_keeps_ranked_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1203-0.html"
        self.require_source_fixture(source)
        text = text_parser.extract_pre_blocks(html_parser.decode(source.read_bytes()))
        categories = text_parser.parse_text(text)
        men = next(c for c in categories if c["name"] == "Herren 19-")
        self.assertEqual((men["declaredStarters"], len(men["results"])), (29, 29))
        groell = next(r for r in men["results"] if r["name"] == "Gröll Matthias")
        self.assertEqual((groell["rank"], groell["timeS"], groell["club"]),
                         (1, 2348, "OLC Graz"))

    def test_fixed_width_score_results_keep_integer_minute_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1745-0.html"
        self.require_source_fixture(source)
        text = text_parser.extract_pre_blocks(html_parser.decode(source.read_bytes()))
        categories = text_parser.parse_text(text)
        men = next(c for c in categories if c["name"] == "Herren 1")
        self.assertEqual((men["declaredStarters"], len(men["results"])), (97, 97))
        women = next(c for c in categories if c["name"] == "Damen 1")
        self.assertEqual((women["declaredStarters"], len(women["results"])), (50, 50))
        upper = next(c for c in categories if c["name"].startswith("Herren Oberstu"))
        self.assertEqual((upper["declaredStarters"], len(upper["results"])), (None, 9))
        benesch = next(r for r in men["results"] if r["name"] == "Benesch, Julian")
        self.assertEqual(
            (benesch["rank"], benesch["status"], benesch["timeText"],
             benesch["scoreText"], benesch["club"]),
            (1, "ok", "40", "490", "BIL"),
        )

    def test_truncated_pre_category_count_does_not_leak_into_previous_class(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1585-0.html"
        self.require_source_fixture(source)
        text = text_parser.extract_pre_blocks(html_parser.decode(source.read_bytes()))
        categories = text_parser.parse_text(text)
        women_b = next(c for c in categories if c["name"] == "Damen B")
        women_adult = next(c for c in categories if c["name"] == "Damen C Erwachsen")
        women_school = next(c for c in categories if c["name"] == "Damen C Schüler")
        men_b = next(c for c in categories if c["name"] == "Herren B")
        self.assertEqual((women_b["declaredStarters"], len(women_b["results"])), (5, 5))
        self.assertEqual((women_adult["declaredStarters"], len(women_adult["results"])),
                         (None, 8))
        self.assertEqual((women_school["declaredStarters"], len(women_school["results"])),
                         (1, 1))
        self.assertEqual((men_b["declaredStarters"], len(men_b["results"])), (14, 14))

    def test_meos_duration_is_not_the_declared_starter_count(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2500-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_meos_individual_pdf(source)
        women = next(c for c in categories if c["name"] == "D OL-15")
        self.assertEqual(
            (women["declaredStarters"],
             pdf_parser.category_competitor_unit_count(women)),
            (6, 6),
        )
        pair = [r for r in women["results"] if r.get("resultKind") == "pair"]
        self.assertEqual({r["name"] for r in pair}, {"Diana", "Ronja"})

    def test_mannschaft_html_keeps_embedded_individual_and_family_classes(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2612-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))

        short = next(c for c in categories if c["name"] == "Einzel Kurz")
        self.assertEqual((short["declaredStarters"], len(short["results"])), (40, 40))
        self.assertEqual({r["resultKind"] for r in short["results"]}, {"individual"})

        family = next(c for c in categories if c["name"] == "Family")
        self.assertEqual((family["declaredStarters"], len(family["results"])), (10, 10))
        self.assertEqual({r["resultKind"] for r in family["results"]}, {"family"})
        self.assertIn(
            "Rass Julia + Rass Magdalena + Rass Elisabeth",
            {r["name"] for r in family["results"]},
        )

        elite = next(c for c in categories if c["name"] == "Herren 19- Elite")
        self.assertEqual(pdf_parser.category_competitor_unit_count(elite), 11)

    def test_excel_web_pdf_recovers_glued_bib_year_and_championship_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1909-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_excel_web_pdf(source)

        men = next(c for c in categories if c["name"] == "Herren Elite")
        tobias = next(r for r in men["results"] if r["name"] == "Breitschädl Tobias")
        self.assertEqual(
            (tobias["rank"], tobias["timeS"], tobias["club"],
             tobias["championship"]),
            (1, 3274, "Askö Henndorf", "ÖSTM"),
        )
        self.assertEqual(len(men["results"]), 13)

        women = next(c for c in categories if c["name"] == "Damen Elite")
        self.assertEqual((women["declaredStarters"], len(women["results"])), (12, 12))
        marina = next(r for r in women["results"] if r["name"] == "Reiner Marina")
        self.assertEqual((marina["rank"], marina["championship"]), (4, "ÖSTM"))

    def test_school_schnupper_people_count_becomes_pair_start_count(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "5174-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        beginner = next(c for c in categories if c["name"] == "Schnupperklasse")
        self.assertEqual(
            (beginner["declaredStarters"],
             pdf_parser.category_competitor_unit_count(beginner)),
            (14, 14),
        )
        self.assertEqual(
            {r["name"] for r in beginner["results"]
             if r.get("teamNumber") == "school-pair-1"},
            {"Mahdi", "Charlotte"},
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
        self.assertEqual(
            build_db.canonicalize_official_club(
                "OK gittis Klosterneuburg", build_db.OFFICIAL_CLUBS),
            "Orienteering Klosterneuburg",
        )
        self.assertEqual(
            build_db.canonicalize_official_club(
                "Orienteering Kloste", build_db.OFFICIAL_CLUBS),
            "Orienteering Klosterneuburg",
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

    def test_legacy_relay_keeps_rankless_teams_members_and_title(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "922-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_relay_pdf(source)
        elite = next(c for c in categories if c["name"] == "Herren 19- Elite")

        team_15 = [r for r in elite["results"] if r["teamNumber"] == "15"]
        self.assertEqual({r["name"] for r in team_15}, {
            "Lukas Scharnagl", "Christian Wartbichler", "Robert Merl",
        })
        self.assertEqual({r["teamName"] for r in team_15}, {"ASKÖ Henndorf"})
        self.assertEqual({r["championship"] for r in team_15}, {"ÖSTM"})

        team_14 = [r for r in elite["results"] if r["teamNumber"] == "14"]
        self.assertEqual({r["name"] for r in team_14}, {
            "Florian Schiel", "Erich Göschl", "Vito Satrapa",
        })
        self.assertEqual({r["individualStatus"] for r in team_14}, {"ok", "mp"})

        team_7 = [r for r in elite["results"] if r["teamNumber"] == "7"]
        self.assertEqual({r["name"] for r in team_7}, {
            "Thomas Polster", "Christian Gotthardt", "Josef Polster",
        })
        self.assertEqual({r["teamName"] for r in team_7}, {"HSV Spittal / Drau"})
        self.assertEqual({r["status"] for r in team_7}, {"dnf"})
        self.assertEqual(
            next(r for r in team_7 if r["name"] == "Josef Polster")["individualStatus"],
            "dns",
        )

    def test_ski_pdf_drops_header_and_recovers_time_glued_to_club(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4346-4.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        masters = next(c for c in categories if c["name"] == "Herren ab 55")
        self.assertFalse(any(r["name"] == "Abtenau" for r in masters["results"]))
        roland = next(r for r in masters["results"] if r["name"] == "Roland Reisenberger")
        self.assertEqual(roland["club"], "Orienteering Klosterneuburg")
        self.assertEqual((roland["timeText"], roland["timeS"], roland["status"]),
                         ("45:35", 2735, "ok"))

        score_source = ROOT / "data" / "raw" / "anne" / "files" / "2794-0.pdf"
        self.require_source_fixture(score_source)
        score_categories, _ = pdf_parser.parse_pdf(score_source)
        women = next(c for c in score_categories if c["name"] == "Damen B")
        self.assertFalse(any(
            r["name"] == "Donauinsel-Kaisermühlen" for r in women["results"]))

    def test_pair_pdf_keeps_primary_name_column_and_optional_partners(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4153-1.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        guests = next(c for c in categories if c["name"] == "Herren Gäste")

        units = {
            (r.get("rank"), r.get("status"), r.get("timeS"), r.get("club"))
            if r.get("resultKind") == "pair" else ("row", index)
            for index, r in enumerate(guests["results"])
        }
        self.assertEqual(len(units), guests["declaredStarters"])
        self.assertIn("Benjamin ALTMANN", {r["name"] for r in guests["results"]})
        pair = [r for r in guests["results"] if r.get("resultKind") == "pair"]
        self.assertEqual({r["name"] for r in pair}, {"Fabian SAMEC", "DRAESNER Felix"})
        peter = next(r for r in guests["results"] if r["name"] == "Peter ILLIG")
        self.assertTrue(peter["outOfCompetition"])

    def test_flowing_pdf_keeps_parenthesized_foreign_place(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4474-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        elite = next(c for c in categories if c["name"] == "H 21E")
        vitek = next(r for r in elite["results"] if r["name"] == "Pospisil Vitek")
        self.assertEqual((vitek["rank"], vitek["timeS"]), (2, 5472))
        self.assertTrue(vitek["outOfCompetition"])
        self.assertEqual(len(elite["results"]), elite["declaredStarters"])

    def test_school_pdf_uses_full_school_boundary_and_does_not_split_names(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4779-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        women = next(c for c in categories if c["name"] == "Oberstufe Weibl.")
        self.assertEqual(len(women["results"]), women["declaredStarters"])
        alexia = next(r for r in women["results"] if r["name"] == "Lazar Alexia")
        self.assertEqual((alexia["club"], alexia["status"]),
                         ("BRG Solar City Linz", "dsq"))

        beginners = next(c for c in categories if c["name"] == "Schnupperkateg.")
        self.assertEqual(len(beginners["results"]), 6)
        self.assertEqual({r.get("resultKind", "individual") for r in beginners["results"]},
                         {"individual"})
        self.assertIn("Andexlinger Lisa", {r["name"] for r in beginners["results"]})

    def test_broad_pdf_layout_regressions_keep_declared_competitor_units(self):
        def units(category):
            keys = []
            for index, result in enumerate(category["results"]):
                kind = result.get("resultKind") or "individual"
                if kind == "pair":
                    key = (kind, result.get("teamNumber") or (
                        result.get("rank"), result.get("status"),
                        result.get("timeS"), result.get("club")))
                elif kind in ("relay", "team"):
                    key = (kind, result.get("teamNumber") or result.get("teamName"))
                else:
                    key = ("row", index)
                keys.append(key)
            return len(set(keys))

        fixed_sources = ("4346-1", "1710-0", "4769-1", "1511-0", "2477-0")
        for source_id in fixed_sources:
            with self.subTest(source=source_id):
                source = ROOT / "data" / "raw" / "anne" / "files" / f"{source_id}.pdf"
                self.require_source_fixture(source)
                categories, _ = pdf_parser.parse_pdf(source)
                self.assertTrue(categories)
                self.assertFalse([
                    (c["name"], c["declaredStarters"], units(c)) for c in categories
                    if c["declaredStarters"] != units(c)
                ])

        school = ROOT / "data" / "raw" / "anne" / "files" / "2477-0.pdf"
        school_categories, _ = pdf_parser.parse_pdf(school)
        beginners = next(c for c in school_categories if c["name"] == "E Schnupperklasse")
        self.assertEqual(
            {r["name"] for r in beginners["results"] if r.get("teamNumber") == "school-pair-1"},
            {"Alzubaidi Ibrahim", "Kana Hamza"},
        )

    def test_night_pairs_and_clipped_results_preserve_every_person(self):
        night_source = ROOT / "data" / "raw" / "anne" / "files" / "2798-0.pdf"
        self.require_source_fixture(night_source)
        night = pdf_parser.parse_flowing_pdf(night_source)
        boys_12 = next(c for c in night if c["name"] == "H-12")
        self.assertEqual(boys_12["declaredStarters"], 5)
        self.assertEqual(
            {r["name"] for r in boys_12["results"] if r.get("resultKind") == "pair"},
            {"Ochenbauer Niklas", "Ochenbauer Jonas", "Hofer Lukas",
             "Klingenberger Felix", "Degen Paul", "Friedl Eva",
             "Dobler Linus", "Stockert Alwin"},
        )

        clipped_source = ROOT / "data" / "raw" / "anne" / "files" / "3986-0.pdf"
        self.require_source_fixture(clipped_source)
        clipped, _ = pdf_parser.parse_pdf(clipped_source)
        elite = next(c for c in clipped if c["name"] == "Herren ab 21 Elite")
        self.assertEqual(len(elite["results"]), 23)
        self.assertEqual(
            {r["name"] for r in elite["results"] if r.get("rank") is None},
            {"Mueller Gian Andri", "Mayer Johannes"},
        )

    def test_headerless_relay_and_legacy_mannschaft_are_grouped(self):
        relay_source = ROOT / "data" / "raw" / "anne" / "files" / "4580-0.pdf"
        self.require_source_fixture(relay_source)
        relay = pdf_parser.parse_relay_pdf(relay_source)
        open_class = next(c for c in relay if c["name"] == "Offen")
        self.assertEqual(len({r["teamNumber"] for r in open_class["results"]}), 5)

        team_source = ROOT / "data" / "raw" / "anne" / "files" / "851-0.pdf"
        self.require_source_fixture(team_source)
        teams = pdf_parser.parse_relay_pdf(team_source, team_mode=True)
        women_18 = next(c for c in teams if c["name"] == "Damen -18")
        self.assertEqual(len({r["teamNumber"] for r in women_18["results"]}), 8)
        self.assertEqual({r["resultKind"] for r in women_18["results"]}, {"team"})
        self.assertTrue(all("leg" not in r for r in women_18["results"]))


if __name__ == "__main__":
    unittest.main()
