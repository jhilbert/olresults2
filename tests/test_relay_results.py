import html
import importlib.util
import inspect
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from sportsoftware_common import (
    detect_list_type, is_junk_name, parse_champion_annotation, parse_flow_row,
    parse_status, parse_time,
    repair_official_club_status_overflow,
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

CLUB_TABLE_SPEC = importlib.util.spec_from_file_location(
    "parse_club_table", ROOT / "ingest" / "parse_club_table.py")
club_table_parser = importlib.util.module_from_spec(CLUB_TABLE_SPEC)
CLUB_TABLE_SPEC.loader.exec_module(club_table_parser)

BUILD_SPEC = importlib.util.spec_from_file_location(
    "build_db_relay", ROOT / "build" / "build_db.py")
build_db = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build_db)


class RelayStructureTests(unittest.TestCase):
    def setUp(self):
        """Skip cache-backed integration cases in a clean Git checkout.

        The ANNE source cache is intentionally gitignored. Unit and golden
        fixture tests still run in CI; tests that explicitly reference that
        local cache run whenever it is available, including the full local QA
        loop.
        """
        raw_cache = ROOT / "data" / "raw" / "anne" / "files"
        if raw_cache.exists():
            return
        method_source = inspect.getsource(
            getattr(type(self), self._testMethodName))
        if '"raw"' in method_source or "'raw'" in method_source:
            self.skipTest("raw ANNE cache is intentionally not committed")

    def test_staggered_linz_cup_headings_still_use_two_column_parser(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3711-0.pdf"

        categories, _ = pdf_parser.parse_pdf(source)

        self.assertEqual(
            [category["name"] for category in categories],
            ["HA", "HB-Lang", "HB-Kurz", "DB-Lang", "DB-Kurz", "C"],
        )
        db_lang = next(category for category in categories
                       if category["name"] == "DB-Lang")
        self.assertEqual(
            [(row["name"], row["club"]) for row in db_lang["results"][:2]],
            [
                ("DULOVCOVA Marie", "Sokol Kremze"),
                ("ESCHLBÖCK Gudrun", "NF Linz"),
            ],
        )
        self.assertEqual(
            (db_lang["declaredStarters"], len(db_lang["results"])),
            (4, 4),
        )

    def test_status_prefix_boundary_repair_preserves_semantics(self):
        cases = [
            ({"club": "BG/BRG Zehnergasse", "timeText": "Wr. N N Ang"},
             ("BG/BRG Zehnergasse Wr. N", "N Ang")),
            ({"club": "Orienteering Klosterneubur", "timeText": "g Fehlst"},
             ("Orienteering Klosterneuburg", "Fehlst")),
            ({"club": "BG/BRG St. Martin", "timeText": "4C Fehlst"},
             ("BG/BRG St. Martin", "Fehlst")),
            ({"club": "NMS Kirchberg", "timeText": "am Wechsel Fehlst"},
             ("NMS Kirchberg", "am Wechsel Fehlst")),
        ]
        for result, expected in cases:
            with self.subTest(result=result):
                repair_official_club_status_overflow(result)
                self.assertEqual((result["club"], result["timeText"]), expected)

    def test_classic_oe2003_text1_after_time_is_not_a_team_member(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1057-0.pdf"

        categories, _ = pdf_parser.parse_pdf(source)

        self.assertEqual(len(categories), 4)
        self.assertEqual(sum(len(category["results"]) for category in categories), 109)
        first = categories[0]["results"][0]
        self.assertEqual(first["name"], "Knauder Viktoria")
        self.assertEqual(first["club"], "BG/BRG Kirchengasse")
        self.assertEqual(first["timeText"], "25:12")
        self.assertEqual(first["sourceNat"], "St")
        rows = [row for category in categories for row in category["results"]]
        long_name = next(row for row in rows if row["name"] == "Aus der Schmitten Helena")
        self.assertEqual(long_name["club"], "Wimmer Gymnasium, Oberschützen")
        self.assertEqual(long_name["sourceNat"], "B")
        overlapped_state = next(row for row in rows if row["name"] == "Auer Merlin")
        self.assertEqual(overlapped_state["club"],
                         "BG/BRG Zehnergasse Wiener Neus")
        self.assertEqual(overlapped_state["sourceNat"], "NÖ")

    def test_meos_repairs_given_name_shifted_into_club(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1905-0.pdf"

        categories = pdf_parser.parse_meos_individual_pdf(source)
        rows = [row for category in categories for row in category["results"]]
        kilian = next(row for row in rows if row["name"] == "Kilian Degen")
        pupils = next(category for category in categories
                      if category["name"] == "Herren E (Schüler)")
        lia = pupils["results"][0]

        self.assertEqual(kilian["club"], "HSV Pinkafeld")
        self.assertEqual(kilian["rank"], 1)
        self.assertEqual(kilian["timeText"], "21:04")
        self.assertEqual((pupils["declaredStarters"], len(pupils["results"])), (2, 2))
        self.assertEqual(
            (lia["name"], lia["club"], lia["rank"], lia["timeText"]),
            ("Lia, Valentin Gattringer Elisa", "HSV Ried", 1, "39:21"),
        )

    def test_ooc_qualifier_is_a_result_not_a_synthetic_category(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1372-1.pdf"

        categories, _ = pdf_parser.parse_pdf(source)
        h_b = next(category for category in categories
                   if category["name"] == "Herren B")
        vladimir = next(row for row in h_b["results"]
                        if row["name"] == "Vladimir Kolmogorov")

        self.assertEqual(
            (h_b["declaredStarters"], len(h_b["results"]),
             vladimir["status"], vladimir["outOfCompetition"]),
            (20, 20, "mp", True),
        )

    def test_corrupt_hungarian_status_row_uses_verified_source_repair(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2765-2.pdf"

        categories, _ = pdf_parser.parse_pdf(source)
        nyk = next(category for category in categories if category["name"] == "NYK")
        hites = next(row for row in nyk["results"]
                     if row["name"] == "Hites Gergõ")

        self.assertEqual((nyk["declaredStarters"], len(nyk["results"])), (15, 15))
        self.assertEqual(
            (hites["club"], hites["timeText"], hites["status"]),
            ("VBT Veszprémi Bridzs és Táj. SE", "Fehlst", "mp"),
        )

    def test_classic_oe2003_repairs_club_acronym_glued_to_given_name(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1124-0.pdf"

        categories, _ = pdf_parser.parse_pdf(source)
        rows = [row for category in categories for row in category["results"]]
        jasmin = next(row for row in rows
                      if row["name"] == "Rotheneder-Stocker Jasmin-Liv")
        david = next(row for row in rows
                     if row["name"] == "Farkas-Schandl David-Nicolas")

        self.assertEqual(jasmin["club"], "BG Zehnergasse")
        self.assertEqual(david["club"], "BG Zehnergasse")
        self.assertEqual((jasmin["status"], david["status"]), ("mp", "mp"))

    def test_school_schnupper_slash_pairs_remain_individually_addressable(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "937-0.pdf"

        categories, _ = pdf_parser.parse_pdf(source)
        schnupper = next(category for category in categories
                         if category["name"] == "Schnupper B")
        frank = next(row for row in schnupper["results"]
                     if row["name"] == "Frank Dominik")
        walter = next(row for row in schnupper["results"]
                      if row["name"] == "Walter Benjamin")

        self.assertEqual(frank["club"], "BRG Eisenstadt")
        self.assertEqual(frank["teamNumber"], walter["teamNumber"])
        self.assertEqual((frank["rank"], walter["rank"]), (12, 12))
        self.assertEqual((frank["resultKind"], walter["resultKind"]),
                         ("pair", "pair"))

    def test_school_nat_columns_keep_complete_names_clubs_and_state(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1238-1.pdf"

        categories, _ = pdf_parser.parse_pdf(source)
        rows = [row for category in categories for row in category["results"]]
        lisa = next(row for row in rows if row.get("sourceBib") == "127")
        viktoria = next(row for row in rows if row.get("sourceBib") == "216")

        self.assertEqual((lisa["name"], lisa["club"], lisa["sourceNat"]),
                         ("Ennemoser Lisa", "BRG Imst", "T"))
        self.assertEqual(
            (viktoria["name"], viktoria["club"], viktoria["sourceNat"]),
            ("Knauder Viktoria", "BG/BRG Graz, Kirchengasse", "St"),
        )

    def test_pdf_qualitative_participation_is_unranked(self):
        categories = [{"results": [
            {"name": "Heindl Clemens", "timeText": "gut", "status": "ok", "rank": 1},
            {"name": "Lang Maximilian", "timeText": "Erfolgreich teilgenommen",
             "status": "ok", "rank": 1},
        ]}]

        pdf_parser.normalize_qualitative_result_ranks(categories)

        self.assertNotIn("rank", categories[0]["results"][0])
        self.assertNotIn("rank", categories[0]["results"][1])

    def test_krems_school_pairs_are_expanded_without_changing_start_count(self):
        categories = [{"name": "D 14-15", "declaredStarters": 1, "results": [{
            "name": "Studeregger Sophie Fischer Ann", "club": "NMS FURTH",
            "rank": 1, "timeText": "31:42", "timeS": 1902, "status": "ok",
        }]}]

        text_parser.repair_krems_2014_school_pairs(categories)

        self.assertEqual({row["name"] for row in categories[0]["results"]},
                         {"Studeregger Sophie", "Fischer Anna"})
        self.assertEqual({row["teamNumber"] for row in categories[0]["results"]},
                         {"school-pair-1"})

    def test_elapsed_times_over_99_minutes_are_valid(self):
        self.assertEqual(parse_time("114:08"), 6848)
        self.assertEqual(parse_time("136:54"), 8214)
        self.assertEqual(parse_time("1:54:08"), 6848)
        self.assertEqual(parse_status("Ang"), "dns")
        self.assertEqual(parse_status("Missing Punch"), "mp")
        self.assertEqual(parse_status("NOK"), "mp")
        self.assertEqual(parse_status("2 Posten fehlen"), "mp")
        self.assertEqual(parse_status("Posten 10 falsch"), "mp")
        self.assertEqual(parse_status("Not Finish"), "dnf")
        self.assertEqual(parse_status("dis."), "dsq")
        self.assertEqual(parse_status("techn. Fehler"), "dnf")

    def test_pair_name_splitting_handles_shared_and_missing_surnames(self):
        from sportsoftware_common import split_pair_names
        self.assertEqual(split_pair_names("Leo + Max Maurer"),
                         ["Leo Maurer", "Max Maurer"])
        self.assertEqual(split_pair_names("Anna + Selina Skern"),
                         ["Anna Skern", "Selina Skern"])
        self.assertEqual(split_pair_names("Paul + Petra"), ["Paul", "Petra"])
        self.assertEqual(split_pair_names("Hnilica Hannes/Sonja"),
                         ["Hnilica Hannes", "Hnilica Sonja"])

    def test_interleaved_club_suffix_recovers_time_and_status(self):
        repair = pdf_parser.repair_interleaved_club_value
        self.assertEqual(
            repair("ORIENTEERING", "INNSBRUCK 3IM8:S2T8"),
            ("Orienteering Innsbruck Imst", "38:28"))
        self.assertEqual(
            repair("ORIENTEERING", "INNSBRUCKF IeMhSlsTt"),
            ("Orienteering Innsbruck Imst", "Fehlst"))
        self.assertEqual(
            repair("Naturfreunde Villach", "- Orie1n3te:37,05"),
            ("Naturfreunde Villach - Orienteering", "13:37"))
        self.assertEqual(
            repair("BG/BRG Zehnergasse", "Wie3n7e:4r0 Neus140"),
            ("BG/BRG Zehnergasse Wiener Neustadt", "37:40"))
        self.assertEqual(
            repair("Naturfreunde Bad", "Vöslau, 2R9e:j3o9i,00"),
            ("Naturfreunde Bad Vöslau", "29:39"))
        repair_value = pdf_parser.repair_result_club_and_value
        self.assertEqual(
            repair_value("HSV OL Wiener", "Neustad Gut"),
            ("HSV OL Wiener Neustadt", "Gut"),
        )

        self.assertEqual(
            repair_value("SK Zabrovesky", "Brno Gut"),
            ("SK Zabrovesky Brno", "Gut"),
        )
        self.assertEqual(
            repair("SV MÖLTEN RAIFFEISEN", "AM1A:3T0E:5U9RSP"),
            ("SV Mölten Raiffeisen ASV", "1:30:59"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "SKV OLG Deutsch Kaltenbr1u:n0n", "2:33"),
            ("SKV OLG Deutsch Kaltenbrunn", "1:02:33"))

        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "Orienteering KlosterneuburgN", "Ang"),
            ("Orienteering Klosterneuburg", "N Ang"))
        self.assertEqual(
            pdf_parser.repair_interleaved_club_value(
                "MOM Hegyvidék SE-MOMH", "TUáNjfutó1 S:4z2a:5ko3", True),
            ("MOM Hegyvidék SE-MOM Tájfutó Szako", "1:42:53"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "VBT Veszprémi Bridzs és HTáUjNékozódAáufg", ""),
            ("VBT Veszprémi Bridzs és HTáUjNékozód", "Aufg"))
        self.assertEqual(
            repair("BG/BRG Zehnergasse", "Wr. NFeueshtlastd"),
            ("BG/BRG Zehnergasse Wiener Neustadt", "Fehlst"))
        self.assertEqual(
            repair("KNC OOB TJ Sokol", "Kostelec Fn.e Èhl.sl.t"),
            ("KNC OOB TJ Sokol Kostelec", "Fehlst"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "Leibnitzer AC OrientierungslaFuehlst", ""),
            ("Leibnitzer AC - Orienteering", "Fehlst"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "HSV Spittal/Drau 1 :", "01:07"),
            ("HSV Spittal/Drau", "1:01:07"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "Naturfreunde Villach - Oriente 5", "1:16"),
            ("Naturfreunde Villach - Orienteering", "51:16"))

        self.assertEqual(
            pdf_parser.repair_result_club_and_value("OLC Graz", "N Ang"),
            ("OLC Graz", "N Ang"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value("OLG Ströck Wien", "N Ang"),
            ("OLG Ströck Wien", "N Ang"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "HSV Spittal/Drau", "1 :01:07"),
            ("HSV Spittal/Drau", "1:01:07"))
        self.assertEqual(
            pdf_parser.repair_result_club_and_value(
                "Naturfreunde Villach - Orienteering", "5 1:16"),
            ("Naturfreunde Villach - Orienteering", "51:16"))
        self.assertEqual(parse_status("vzdal"), "dnf")
        self.assertEqual(parse_status("APng"), "dns")
        self.assertTrue(is_junk_name("Bib. Name"))
        self.assertTrue(is_junk_name("mit weniger"))

    def test_given_name_glued_to_known_club_is_restored(self):
        fixtures = [
            ({"name": "Luttenberger", "club": "JohannHSV Feldbach",
              "timeText": "0:42:05", "timeS": 2525, "status": "ok"},
             {"name": "Luttenberger Johann", "club": "HSV Feldbach",
              "timeText": "0:42:05", "timeS": 2525, "status": "ok"}),
            ({"name": "Luttenberger", "club": "Marie LHuSisVe Feldbach",
              "timeText": "Aufg.", "rank": 12, "status": "dnf"},
             {"name": "Luttenberger Marie Luise", "club": "HSV Feldbach",
              "timeText": "Aufg.", "rank": 12, "status": "dnf"}),
            ({"name": "Kathrin Kollndorfer HSV", "club": "Grossmittel",
              "timeText": "17:55", "status": "ok"},
             {"name": "Kathrin Kollndorfer", "club": "HSV Großmittel",
              "timeText": "17:55", "status": "ok"}),
            ({"name": "Wachmann Elias BG/BRG", "club": "Fürstenfeld St",
              "timeText": "15:18", "status": "ok"},
             {"name": "Wachmann Elias", "club": "BG/BRG Fürstenfeld",
              "sourceNat": "St", "timeText": "15:18", "status": "ok"}),
            ({"name": "Buchberger/Leutgeb Eva/JuliaBRG", "club": "Eisenstadt",
              "timeText": "34:00", "status": "ok"},
             {"name": "Buchberger/Leutgeb Eva/Julia", "club": "BRG Eisenstadt",
              "timeText": "34:00", "status": "ok"}),
            ({"name": "Frédéric Genevois Naturfreunde Villach",
              "club": "Orienteering", "timeText": "16:46", "status": "ok"},
             {"name": "Frédéric Genevois",
              "club": "Naturfreunde Villach - Orienteering",
              "timeText": "16:46", "status": "ok"}),
        ]
        for row, expected in fixtures:
            with self.subTest(club=row["club"]):
                self.assertEqual(pdf_parser.repair_shifted_name_club_time(row),
                                 expected)
        untouched = [
            {"name": "Pock/Schweinzer", "club": "NMS Fürstenfeld",
             "timeText": "18:31", "status": "ok"},
            {"name": "Haselsberger",
             "club": "Naturfreunde Villach Orienteering",
             "timeText": "1:08:39", "status": "ok"},
        ]
        for row in untouched:
            with self.subTest(untouched=row["club"]):
                self.assertEqual(pdf_parser.repair_shifted_name_club_time(
                    dict(row)), row)

    def test_given_name_shifted_into_club_column_is_restored(self):
        result = {
            "name": "Kaltenbacher",
            "club": "Pierre HSV OL Wiener Neustadt",
            "timeText": "59:18",
            "status": "ok",
        }
        pdf_parser.repair_shifted_name_club_time(result)
        self.assertEqual(result["name"], "Kaltenbacher Pierre")
        self.assertEqual(result["club"], "HSV OL Wiener Neustadt")

    def test_pdf_overflow_repairs_real_rows_and_drops_stage_footers(self):
        fixtures = {
            "1692-1.pdf": ("Maximilian Egger", "dnf", None,
                           "Laufklub Kompass Innsbruck Imst"),
            "3999-0.pdf": ("Reiner Matthias", "ok", 817,
                           "Naturfreunde Villach - Orienteering"),
            "4626-4.pdf": ("Malea Fritsch", "ok", 2308,
                           "Orienteering Innsbruck Imst"),
        }
        for file_name, expected in fixtures.items():
            with self.subTest(source=file_name):
                source = ROOT / "data" / "raw" / "anne" / "files" / file_name
                self.require_source_fixture(source)
                categories, _ = pdf_parser.parse_pdf(source)
                result = next(
                    row for category in categories for row in category["results"]
                    if row["name"] == expected[0])
                self.assertEqual(
                    (result["status"], result.get("timeS"), result["club"]),
                    expected[1:])

        footer_source = ROOT / "data" / "raw" / "anne" / "files" / "2075-0.pdf"
        self.require_source_fixture(footer_source)
        categories, _ = pdf_parser.parse_pdf(footer_source)
        self.assertFalse(any(
            row["name"].startswith("Results (stage")
            for category in categories for row in category["results"]))

    def test_championship_overall_and_national_rank_prefix_keeps_runner(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1677-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)

        women = next(category for category in categories
                     if category["name"] == "Damen 18-20")
        magdalena = next(row for row in women["results"]
                         if row["name"] == "Van de Voorde Magdalena")
        self.assertEqual((magdalena["rank"], magdalena["timeS"], magdalena["club"]),
                         (3, 1847, "SU Klagenfurt"))
        self.assertEqual(pdf_parser.category_competitor_unit_count(women), 5)

        men = next(category for category in categories
                   if category["name"] == "Herren18-20")
        bernhard = next(row for row in men["results"]
                        if row["name"] == "Lerchegger Bernhard")
        self.assertEqual((bernhard["rank"], bernhard["timeS"]), (2, 1654))
        self.assertEqual(pdf_parser.category_competitor_unit_count(men), 2)

    def test_historic_zell_club_is_a_result_row_boundary(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2675-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)

        girls = next(category for category in categories
                     if category["name"] == "Damen-14")
        antonia = next(row for row in girls["results"]
                       if row["name"] == "Seitlinger Antonia")
        self.assertEqual((antonia["club"], antonia["status"]),
                         ("OL Sektion TV Zell am See", "dns"))
        self.assertEqual(pdf_parser.category_competitor_unit_count(girls), 23)

        men = next(category for category in categories
                   if category["name"] == "Herren45-")
        gabriel = next(row for row in men["results"]
                       if row["name"] == "Seitlinger Gabriel")
        self.assertEqual((gabriel["club"], gabriel["status"]),
                         ("OL Sektion TV Zell am See", "dns"))
        self.assertEqual(pdf_parser.category_competitor_unit_count(men), 18)

        family = next(category for category in categories
                      if category["name"] == "Familiy")
        self.assertEqual(family.get("sourceUnitCount"), 9)
        self.assertEqual(family["declaredStarters"], 9)

    def test_clipped_villach_club_keeps_complete_result_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2598-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        by_name = {category["name"]: category for category in categories}
        expected = {
            "D 65-": "Prommer Martha",
            "D-Hobby": "Rapotz Brigitte",
            "H 16E": "Rapotz David",
            "H 65-": "Prommer Günther",
        }
        for category_name, runner_name in expected.items():
            with self.subTest(category=category_name):
                category = by_name[category_name]
                runner = next(row for row in category["results"]
                              if row["name"] == runner_name)
                self.assertEqual(runner["club"],
                                 "Naturfreunde Villach - Orienteering")
                self.assertEqual(
                    pdf_parser.category_competitor_unit_count(category),
                    category["declaredStarters"])

    def test_oribos_relay_uses_team_and_leg_columns_not_repeated_header(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4645-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_oribos_relay_pdf(source)
        self.assertFalse(any(
            row["name"] == "Bib. Name"
            for category in categories for row in category["results"]))

        d12 = next(category for category in categories if category["name"] == "D12")
        self.assertEqual(d12["declaredStarters"], 4)
        julia = next(row for row in d12["results"] if row["name"] == "Julia Tanner")
        self.assertEqual(
            (julia["rank"], julia["teamNumber"], julia["teamName"],
             julia["club"], julia["leg"], julia["legCount"],
             julia["status"], julia["timeS"], julia["teamTimeS"]),
            (1, "4", "Graubünden 54", "Graubünden", 1, 2,
             "ok", 1175, 2326))

        d14 = next(category for category in categories if category["name"] == "D14")
        sara = next(row for row in d14["results"] if row["name"] == "Sara Permann")
        lia = next(row for row in d14["results"] if row["name"] == "Lia Grassi")
        self.assertEqual((sara["status"], sara["individualStatus"]), ("dns", "ok"))
        self.assertEqual((lia["status"], lia["individualStatus"]), ("dns", "dns"))

        h10 = next(category for category in categories if category["name"] == "H10")
        enea = next(row for row in h10["results"] if row["name"] == "Enea Ruggiero")
        self.assertEqual((enea["rank"], enea["timeS"], enea["club"]),
                         (1, 1731, "Lombardia"))

    def test_rank_only_and_multistage_rows_are_classified(self):
        rank_only = ROOT / "data" / "raw" / "anne" / "files" / "3986-0.pdf"
        self.require_source_fixture(rank_only)
        categories, _ = pdf_parser.parse_pdf(rank_only)
        rows = [row for category in categories for row in category["results"]]
        tobias = next(row for row in rows if row["name"] == "Habenicht Tobias")
        mayer = next(row for row in rows if row["name"] == "Mayer Johannes")
        mueller = next(row for row in rows if row["name"] == "Mueller Gian Andri")
        self.assertEqual(
            (tobias["rank"], tobias["status"], tobias.get("timeS")),
            (1, "ok", 1103))
        self.assertEqual((mayer.get("rank"), mayer["status"]), (None, "dnf"))
        self.assertEqual(mueller["status"], "mp")

        multistage = ROOT / "data" / "raw" / "anne" / "files" / "2605-0.pdf"
        self.require_source_fixture(multistage)
        categories, _ = pdf_parser.parse_pdf(multistage)
        rows = [row for category in categories for row in category["results"]]
        sabrina = next(row for row in rows if row["name"] == "Sabrina Perktold")
        vanessa = next(row for row in rows if row["name"] == "Vanessa Mark")
        self.assertEqual(
            (sabrina["rank"], sabrina["timeS"], sabrina["status"],
             sabrina["rankingBasis"]),
            (2, 2190, "ok", "other"))
        self.assertEqual(vanessa["status"], "mp")

    def test_estimated_time_championship_keeps_score_ranking(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2865-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        self.assertEqual(len(categories), 1)
        category = categories[0]
        self.assertEqual(
            (category["name"], category["declaredStarters"], len(category["results"])),
            ("Vereinsmeisterschaft", 22, 22))
        by_name = {row["name"]: row for row in category["results"]}
        self.assertEqual(
            (by_name["Markus Wolf"]["rank"], by_name["Markus Wolf"]["scoreText"]),
            (1, "Abweichung 0,62"))
        self.assertEqual(by_name["Eduard Böhm"]["status"], "dsq")
        self.assertEqual(by_name["Bernhard Klingseisen"]["status"], "dns")
        self.assertEqual(by_name["Slávka Cahlová"]["status"], "unknown")
        self.assertEqual(by_name["Slávka Cahlová"]["timeText"], "???")

    def test_plain_course_and_age_headings_split_shared_pdf_table(self):
        course_source = ROOT / "data" / "raw" / "anne" / "files" / "1941-0.pdf"
        self.require_source_fixture(course_source)
        courses, _ = pdf_parser.parse_pdf(course_source)
        self.assertEqual(
            [(category["name"], len(category["results"])) for category in courses],
            [("Bahn A", 33), ("Bahn B", 29), ("Bahn C", 11), ("Bahn D", 5)])

        age_source = ROOT / "data" / "raw" / "anne" / "files" / "3852-2.pdf"
        self.require_source_fixture(age_source)
        ages, _ = pdf_parser.parse_pdf(age_source)
        self.assertIn("Herren bis 18 Elite", {category["name"] for category in ages})
        self.assertIn("Damen ab 75", {category["name"] for category in ages})
        self.assertNotIn("Ergebnis", {category["name"] for category in ages})

        numbered_source = ROOT / "data" / "raw" / "anne" / "files" / "2860-0.pdf"
        self.require_source_fixture(numbered_source)
        numbered, _ = pdf_parser.parse_pdf(numbered_source)
        self.assertEqual(
            [(category["name"], len(category["results"])) for category in numbered],
            [("Bahn 1", 35), ("Bahn 2", 22), ("Bahn 3", 3),
             ("Bahn 4", 16), ("Bahn 5", 5), ("Bahn 6", 19)])

    def test_pdf_shifted_club_time_uses_finish_not_gap(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3739-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        women = next(category for category in categories if category["name"] == "Damen A")
        kiara = next(row for row in women["results"] if row["name"] == "Kiara Piskorz")
        self.assertEqual((kiara["club"], kiara["timeText"], kiara["timeS"]),
                         ("WAT-OL", "20:43", 1243))
        marina = next(row for row in women["results"] if row["name"] == "Marina Skern")
        self.assertEqual((marina["club"], marina["timeText"]),
                         ("Naturfreunde Wien", "21:48"))

    def test_regional_championship_pdf_splits_real_categories(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3652-2.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        self.assertEqual(
            [(category["name"], len(category["results"])) for category in categories],
            [("Damen -13", 5), ("Herren -13", 6), ("Damen -18", 3),
             ("Herren -35", 5), ("Damen -45", 5), ("Herren -45", 4),
             ("Damen -55", 3), ("Herren -65", 8)])
        women_45 = next(category for category in categories
                        if category["name"] == "Damen -45")
        daniela = next(row for row in women_45["results"]
                       if row["name"] == "Daniela Fink")
        self.assertEqual(
            (daniela["club"], daniela["status"], daniela["yearOfBirth"]),
            ("ASKÖ Henndorf Orienteering", "dnf", 1976))

    def test_joint_eastern_state_championship_preserves_nat_column(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4963-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        women_14 = next(category for category in categories
                        if category["name"] == "D-14")
        self.assertEqual(
            [(row["rank"], row["sourceNat"]) for row in women_14["results"]],
            [(1, "B"), (1, "B"), (2, "St"), (2, "St"),
             (3, "NÖ"), (3, "NÖ"), (4, "St"), (4, "St"),
             (5, "NÖ"), (5, "NÖ")])
        self.assertEqual(women_14["results"][-1]["club"],
                         "HSV OL Wiener Neustadt")
        self.assertEqual(
            [(row["name"], row["resultKind"], row["teamNumber"])
             for row in women_14["results"][-2:]],
            [("Stockmayer Lina", "pair", "8"),
             ("Stockmayer Emma", "pair", "8")])
        women_45 = next(category for category in categories
                        if category["name"] == "D40-(B),D45-(NÖ,St,W)")
        self.assertEqual(
            [(row["club"], row["sourceNat"]) for row in women_45["results"]
             if row["name"] in {"Kogelmann Silke", "Tezarek Helga"}],
            [("SKV OLG Deutsch Kaltenbrunn", "B"),
             ("Orienteering Klosterneuburg", "NÖ")])
        men_19 = next(category for category in categories
                      if category["name"] == "H19-")
        self.assertEqual(
            [row["name"] for row in men_19["results"] if "Schmitten" in row["name"]],
            ["Aus der Schmitten Paul", "Aus der Schmitten Jakob"])

    def test_shifted_final_pdf_page_recovers_all_declared_rows_from_flow(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3642-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        women = next(category for category in categories
                     if category["name"] == "OÖ Mittel D 50-")
        men = next(category for category in categories
                   if category["name"] == "OÖ Mittel H 60-")
        self.assertEqual((len(women["results"]), len(men["results"])), (6, 9))
        self.assertEqual(
            [(row["rank"], row["name"], row["timeS"]) for row in women["results"]],
            [(1, "Zöbl Maria", 2512), (2, "Wagner Birgit", 2909),
             (3, "Eschlböck Gudrun", 3350), (4, "Haider Anna", 3567),
             (5, "Roder Ulrike", 3787), (6, "Wagner Elfi", 4367)])
        self.assertEqual(men["results"][0]["rank"], 1)
        self.assertEqual(men["results"][0]["name"], "Gittmaier Georg")

    def test_exact_flow_crosscheck_adds_clean_missing_status_rows(self):
        fixtures = [
            ("821-0.pdf", "Da45-", "Scherr Hildegard", "HSV Villach", "dns"),
            ("1036-0.pdf", "Damen 19-", "Andrea Matitz", "HSV Villach", "dnf"),
            ("1578-0.pdf", "D3", "Wen Jin Lin", "Hit2", "mp"),
        ]
        for file_name, category_name, name, club, status in fixtures:
            with self.subTest(source=file_name, category=category_name):
                source = ROOT / "data" / "raw" / "anne" / "files" / file_name
                self.require_source_fixture(source)
                categories, _ = pdf_parser.parse_pdf(source)
                category = next(row for row in categories
                                if row["name"] == category_name)
                self.assertEqual(len(category["results"]), category["declaredStarters"])
                recovered = next(row for row in category["results"]
                                 if row["name"] == name)
                self.assertEqual((recovered["club"], recovered["status"]),
                                 (club, status))

    def test_detached_time_prefix_and_score_basis_survive_complete_parse(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1121-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        family = next(category for category in categories
                      if category["name"] == "FAMILY")
        zoe = next(row for row in family["results"]
                   if row["name"] == "Kohlbacher Zoe")
        men = next(category for category in categories
                   if category["name"] == "H 19-")
        daniel = next(row for row in men["results"]
                      if row["name"] == "Gotthardt Daniel")
        self.assertEqual((zoe["club"], zoe["timeS"]),
                         ("Naturfreunde Villach - Orienteering", 3076))
        self.assertEqual((daniel["club"], daniel["timeS"]),
                         ("HSV Spittal/Drau", 3667))

        festival_source = ROOT / "data" / "raw" / "anne" / "files" / "4626-6.pdf"
        self.require_source_fixture(festival_source)
        festival, _ = pdf_parser.parse_pdf(festival_source)
        girls_18 = next(category for category in festival
                        if category["name"] == "D18")
        matilda = next(row for row in girls_18["results"]
                       if row["name"] == "Matilda Buschek")
        cleo = next(row for row in girls_18["results"]
                    if row["name"] == "Cleo Machold")
        self.assertEqual((matilda["club"], matilda["timeS"]),
                         ("Naturfreunde Wien", 4211))
        self.assertEqual((cleo["club"], cleo["timeS"]),
                         ("Naturfreunde Wien", 4802))

        score_source = ROOT / "data" / "raw" / "anne" / "files" / "991-0.pdf"
        self.require_source_fixture(score_source)
        score_categories, _ = pdf_parser.parse_pdf(score_source)
        self.assertTrue(all(
            row.get("rankingBasis") == "score"
            for category in score_categories for row in category["results"]))

        friendship_source = ROOT / "data" / "raw" / "anne" / "files" / "2765-2.pdf"
        self.require_source_fixture(friendship_source)
        friendship, _ = pdf_parser.parse_pdf(friendship_source)
        men_50 = next(category for category in friendship
                      if category["name"] == "F50")
        scherr = next(row for row in men_50["results"]
                      if row["name"] == "Scherr Bruno")
        women_21 = next(category for category in friendship
                        if category["name"] == "N21A")
        szabo = next(row for row in women_21["results"]
                     if row["name"] == "Szabó Emese")
        self.assertEqual((scherr["club"], scherr["timeS"]),
                         ("XNRE Naturfreunde OLG", 2525))
        self.assertEqual((szabo["club"], szabo["timeS"]),
                         ("ZTC Zalaegerszegi Fut", 5362))

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

    def test_structural_team_leg_headers_parse_sprint_relay_as_teams(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1523-1.html"
        self.require_source_fixture(source)
        text = html_parser.decode(source.read_bytes())
        self.assertEqual(html_parser.detect_list_type(source.name, text), "relay")
        categories = html_parser.parse_relay_document(text)
        senior = next(c for c in categories if c["name"] == "Senior 33-80")
        self.assertEqual(senior["declaredStarters"], 16)
        self.assertEqual(len(senior["results"]), 64)
        self.assertEqual(len({r["teamNumber"] for r in senior["results"]}), 16)
        self.assertEqual({r["leg"] for r in senior["results"]}, {1, 2, 3, 4})

    def test_pdf_page_furniture_signatures_are_rejected(self):
        self.assertTrue(pdf_parser.PDF_PAGE_CHROME_RE.search("Page 3 of 4"))
        self.assertTrue(is_junk_name("MTBO World Cup"))

    def test_inline_font_score_html_preserves_ak_results(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1367-0.html"
        self.require_source_fixture(source)
        text = html_parser.decode(source.read_bytes())
        import html as html_mod
        import re
        fixed_text = html_mod.unescape(re.sub(r"<[^>]+>", "", text))
        categories = text_parser.parse_text(fixed_text)
        men = next(c for c in categories if c["name"] == "Herren B")
        ooc = {r["name"] for r in men["results"] if r.get("outOfCompetition")}
        self.assertEqual(ooc, {"Biel Axel", "Jörgen Deubel"})

    def test_duplicate_rankless_relay_start_number_prefers_ranked_team(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1455-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_relay_pdf(source)
        pairs = next(c for c in categories if c["name"] == "Offen 2er")
        self.assertEqual(pairs["declaredStarters"], 11)
        self.assertEqual(len({r["teamNumber"] for r in pairs["results"]}), 11)
        team_93 = [r for r in pairs["results"] if r["teamNumber"] == "93"]
        self.assertEqual({r["name"] for r in team_93}, {"Cart Andreas", "Cart Johanna"})
        self.assertEqual({r["rank"] for r in team_93}, {4})
        self.assertEqual({r["status"] for r in team_93}, {"ok"})

    def test_course_class_pdf_recovers_rank_full_name_and_club(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "874-1.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        course = next(c for c in categories if c["name"] == "Bahn 4")
        self.assertEqual(pdf_parser.category_competitor_unit_count(course), 10)
        winner = next(r for r in course["results"] if r["name"] == "Hoffmann Hannah")
        self.assertEqual((winner["rank"], winner["club"], winner["timeS"]),
                         (1, "LZ Omaha", 1757))
        samec = next(r for r in course["results"] if r["name"] == "Samec Fabian")
        self.assertTrue(samec["outOfCompetition"])

    def test_multi_round_pdf_uses_total_and_preserves_every_rank(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "952-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        results = categories[0]["results"]
        self.assertEqual([r["rank"] for r in results], list(range(1, 22)))
        ditz = next(r for r in results if r["name"] == "Ditz Robert")
        self.assertEqual((ditz["club"], ditz["timeText"], ditz["timeS"]),
                         ("Naturfreunde Wien", "21:02", 1262))

    def test_score_pdf_is_split_into_independently_ranked_courses(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1837-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        self.assertEqual({c["name"] for c in categories},
                         {"Bahn A-50MIN", "Bahn B-40MIN", "Bahn C-30MIN"})
        course_a = next(c for c in categories if c["name"] == "Bahn A-50MIN")
        self.assertEqual(len(course_a["results"]), 14)
        winner = course_a["results"][0]
        self.assertEqual((winner["rank"], winner["name"], winner["club"],
                          winner["timeS"], winner["scoreText"]),
                         (1, "Kubelka Stefan", "Leibnitzer AC", 3185, "490"))

    def test_cellless_html_champion_row_restores_winner_rank(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2579-1.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        men_60 = next(c for c in categories if c["name"] == "Herren 60-")
        winner = next(r for r in men_60["results"] if r["name"] == "Zapletal Josef")
        self.assertEqual(winner["rank"], 1)
        self.assertEqual(winner["championship"], "ÖM")
        self.assertFalse([
            r["name"] for c in categories for r in c["results"]
            if r.get("status") == "ok" and r.get("timeS") is not None
            and r.get("rank") is None and not r.get("outOfCompetition")
        ])

    def test_html_champion_row_with_omitted_place_cell_keeps_winner(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3828-1.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        women_75 = next(c for c in categories if c["name"] == "Damen ab 75")
        self.assertEqual(len(women_75["results"]), 3)
        winner = women_75["results"][0]
        self.assertEqual(
            (winner["rank"], winner["name"], winner["club"], winner["timeS"],
             winner["yearOfBirth"], winner["championship"]),
            (1, "Roder Ulrike", "HSV Ried", 4121, 1940, "ÖM"),
        )

    def test_secondary_championship_rank_column_does_not_hide_overall_rank(self):
        self.assertEqual(
            parse_champion_annotation(
                "3 1 und Wr. ASKÖ-Meisterin 2023 der Kategorie D 13-14"),
            (3, None),
        )
        self.assertEqual(
            parse_champion_annotation(
                "2 1 2 und Wr. Senioren-Meister 2021 der Kategorie H 45-"),
            (2, None),
        )
        source = ROOT / "data" / "raw" / "anne" / "files" / "3977-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        women = next(c for c in categories if c["name"] == "Damen 13-14")
        merryn = next(r for r in women["results"] if r["name"] == "MILLARD Merryn")
        self.assertEqual(merryn["rank"], 3)

    def test_exact_time_tie_inherits_preceding_competition_rank(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2764-2.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        men = next(c for c in categories if c["name"] == "Herren 55-")
        tied = {r["name"]: r.get("rank") for r in men["results"]
                if r["timeText"] == "00:51:22"}
        self.assertEqual(tied, {"BIEL Axel": 1, "ZAPLETAL Josef": 1})

    def test_word_processor_championship_list_uses_flowing_structure(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3055-2.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        self.assertGreaterEqual(len(categories), 20)
        women = next(c for c in categories if c["name"] == "Damen 55-")
        winner = next(r for r in women["results"] if r["name"] == "GOLLMANN Birgit")
        self.assertEqual((winner["rank"], winner["timeS"]), (1, 2573))
        finder = next(r for r in women["results"] if r["name"] == "FINDER Gaby")
        self.assertTrue(finder["outOfCompetition"])
        dns = next(c for c in categories if c["name"] == "Damen 65-")["results"][0]
        self.assertEqual(dns["status"], "dns")
        self.assertEqual(
            html_parser.detect_list_type(
                "event_3055_ergebnis-wienerwertung.pdf", "", False),
            "race",
        )

    def test_status_note_parentheses_do_not_create_phantom_category(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1967-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        self.assertNotIn("Hittisau 2 23:56 Fehlst 7",
                         {c["name"] for c in categories})
        self.assertIn("Damen 3", {c["name"] for c in categories})
        women = next(c for c in categories if c["name"] == "Damen 3")
        self.assertEqual([r["rank"] for r in women["results"]], list(range(1, 13)))
        women_2 = next(c for c in categories if c["name"] == "Damen 2")
        lahr = next(r for r in women_2["results"] if r["name"] == "Leoni Lahr")
        self.assertEqual((lahr["status"], lahr["timeS"]), ("mp", 1436))

    def test_status_after_time_preserves_time_and_ooc_semantics(self):
        from sportsoftware_common import parse_flow_row
        flow = parse_flow_row(
            "173 Wieser Niklas M17 HSV Pinkafeld 75:19 nc", pdf_parser.CLUBS)
        nc = pdf_parser.flow_results(flow)[0]
        self.assertEqual((nc["status"], nc["timeS"]), ("ok", 4519))
        self.assertTrue(nc["outOfCompetition"])

    def test_single_rank_gap_between_neighbors_is_recovered(self):
        categories = [{"results": [
            {"name": "A", "rank": 5, "status": "ok", "timeS": 10},
            {"name": "B", "status": "ok", "timeS": 11},
            {"name": "C", "rank": 7, "status": "ok", "timeS": 12},
        ]}]
        pdf_parser.normalize_exact_time_ties(categories)
        self.assertEqual(categories[0]["results"][1]["rank"], 6)

    def test_split_tens_minute_digit_is_recovered_by_rank_order(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1516-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        women = next(c for c in categories if c["name"] == "D15-54 Stmk/D15- B")
        expected = {
            "Bauer Julia": ("S KV OLG Deutsch Kaltenbrunn", "16:51", 1011),
            "Hafner Andrea": ("S KV OLG Deutsch Kaltenbrunn", "23:27", 1407),
            "Kogelmann Silke": ("S KV OLG Deutsch Kaltenbrunn", "25:29", 1529),
        }
        for name, wanted in expected.items():
            row = next(r for r in women["results"] if r["name"] == name)
            self.assertEqual((row["club"], row["timeText"], row["timeS"]), wanted)

    def test_interleaved_championship_places_become_overall_ranks(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "686-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        boys = next(c for c in categories if c["name"] == "Herren -16 Elite")
        selected = {r["name"]: r["rank"] for r in boys["results"][:5]}
        self.assertEqual(selected, {
            "Mathias Peter": 1,
            "Edvin Smedberg": 2,
            "Florian Kurz": 3,
            "Ágoston Fekete": 4,
            "Stefan Falk": 5,
        })

        long_source = ROOT / "data" / "raw" / "anne" / "files" / "1109-0.pdf"
        self.require_source_fixture(long_source)
        long_categories, _ = pdf_parser.parse_pdf(long_source)
        elite = next(c for c in long_categories if c["name"] == "Herren 21- Elite")
        winner = next(r for r in elite["results"] if r["name"] == "Haselsberger Kevin")
        self.assertEqual((winner["rank"], winner.get("championship")), (1, "ÖSTM"))

    def test_digits_in_real_club_codes_are_not_moved_into_time(self):
        mtbo = ROOT / "data" / "raw" / "anne" / "files" / "4477-0.pdf"
        university = ROOT / "data" / "raw" / "anne" / "files" / "1440-0.pdf"
        self.require_source_fixture(mtbo)
        self.require_source_fixture(university)
        mtbo_categories, _ = pdf_parser.parse_pdf(mtbo)
        short = next(c for c in mtbo_categories if c["name"] == "WB/Short")
        dora = next(r for r in short["results"] if r["name"] == "Dora Trabert")
        self.assertEqual((dora["club"], dora["timeText"], dora["timeS"]),
                         ("Hungary X2S Team", "52:25", 3145))
        long_stage = ROOT / "data" / "raw" / "anne" / "files" / "4477-3.pdf"
        self.require_source_fixture(long_stage)
        long_categories, _ = pdf_parser.parse_pdf(long_stage)
        long_m50 = next(c for c in long_categories if c["name"] == "M50")
        libor = next(r for r in long_m50["results"] if r["name"] == "Libor Filip")
        self.assertEqual(
            (libor["club"], libor["timeText"], libor["timeS"]),
            ("SRK SOOB Spartak Rychnov n.Kn.", "1:19:24", 4764),
        )
        university_categories, _ = pdf_parser.parse_pdf(university)
        tu = next(c for c in university_categories if c["name"] == "TU-Wertung")
        bosina = next(r for r in tu["results"] if r["name"] == "Bosina Joachim")
        self.assertEqual((bosina["club"], bosina["timeText"], bosina["timeS"]),
                         ("E141 Atominstitut", "5:52", 352))

    def test_stage_rank_is_not_used_as_total_time_hour(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3038-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        elite = next(c for c in categories if c["name"] == "Damen 21- Elite")
        lena = next(r for r in elite["results"] if r["name"] == "Lena Ennemoser")
        lisa = next(r for r in elite["results"] if r["name"] == "Lisa Ennemoser")
        self.assertEqual((lena["timeText"], lena["timeS"]), ("35:27", 2127))
        self.assertEqual((lisa["timeText"], lisa["timeS"]), ("37:29", 2249))

    def test_plain_report_sections_do_not_merge_into_prior_category(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3852-2.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        by_name = {c["name"]: c for c in categories}
        self.assertIn("Damen ab 21 Kurz", by_name)
        self.assertIn("Neulinge", by_name)
        self.assertIn("Familie", by_name)
        self.assertEqual([r["name"] for r in by_name["Damen ab 21 Kurz"]["results"]],
                         ["Oswald Katharina"])
        self.assertEqual([r["name"] for r in by_name["Neulinge"]["results"]],
                         ["Danklmaier Vera"])

        vienna = ROOT / "data" / "raw" / "anne" / "files" / "2040-0.pdf"
        self.require_source_fixture(vienna)
        vienna_categories, _ = pdf_parser.parse_pdf(vienna)
        women = next(c for c in vienna_categories if c["name"] == "Damen 55- Wien")
        self.assertNotIn("SIEGERT Reinhard", {r["name"] for r in women["results"]})

    def test_leading_hour_and_club_suffix_digit_are_rank_repaired(self):
        long_source = ROOT / "data" / "raw" / "anne" / "files" / "4948-0.pdf"
        club_source = ROOT / "data" / "raw" / "anne" / "files" / "1433-0.pdf"
        self.require_source_fixture(long_source)
        self.require_source_fixture(club_source)
        long_categories, _ = pdf_parser.parse_pdf(long_source)
        women = next(c for c in long_categories if c["name"] == "D 19 -")
        unegg = next(r for r in women["results"] if r["name"] == "Unegg Marlene")
        self.assertEqual((unegg["club"], unegg["timeText"], unegg["timeS"]),
                         ("SU Klagenfurt", "1:22:25", 4945))
        club_categories, _ = pdf_parser.parse_pdf(club_source)
        short = next(c for c in club_categories if c["name"] == "Offen Kurz")
        petra = next(r for r in short["results"] if r["name"] == "Schinnerer Petra")
        self.assertEqual((petra["club"], petra["timeText"], petra["timeS"]),
                         ("Wr. Gehörlosen Sportclub 1901", "47:59", 2879))

    def test_consecutive_woven_hour_markers_use_outer_rank_bounds(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1109-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        women = next(c for c in categories if c["name"] == "Damen 21- Elite")
        reiner = next(r for r in women["results"] if r["name"] == "Reiner Marina")
        pirker = next(r for r in women["results"] if r["name"] == "Pirker Lisa")
        self.assertEqual((reiner["timeText"], reiner["timeS"]), ("2:02:07", 7327))
        self.assertEqual((pirker["timeText"], pirker["timeS"]), ("2:10:08", 7808))

        mtbo = ROOT / "data" / "raw" / "anne" / "files" / "2765-0.pdf"
        self.require_source_fixture(mtbo)
        mtbo_categories, _ = pdf_parser.parse_pdf(mtbo)
        youth = next(c for c in mtbo_categories if c["name"] == "NYK")
        livia = next(r for r in youth["results"] if r["name"] == "Fuchs Lívia")
        self.assertEqual((livia["timeText"], livia["timeS"]), ("1:28:52", 5332))

    def test_champion_wording_in_rank_cell_keeps_first_place(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3325-1.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        girls = next(c for c in categories if c["name"] == "Damen -12 NOE")
        self.assertEqual(girls["results"][0]["rank"], 1)

    def test_detached_multi_rank_line_is_carried_to_runner(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3325-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        seniors = next(c for c in categories
                       if c["name"] == "ASKÖ MS Herren 65- + Herren 75-")
        b = next(r for r in seniors["results"] if r["name"] == "BONEK Ernst")
        self.assertEqual(b["rank"], 6)

    def test_ak_marker_with_surname_in_rank_cell_keeps_full_name(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2040-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        men = next(c for c in categories if c["name"] == "Herren 50- Wien")
        pekka = next(r for r in men["results"] if r["name"] == "PEKKA Lauri")
        self.assertTrue(pekka["outOfCompetition"])

    def test_points_cup_categories_and_excel_duration_are_recovered(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2205-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        self.assertEqual([c["name"] for c in categories],
                         ["Kategorie A", "Kategorie B", "Kategorie C", "Kategorie D"])
        holper = categories[0]["results"][0]
        self.assertEqual((holper["rank"], holper["timeText"], holper["timeS"]),
                         (1, "48:16", 2896))

    def test_multi_attachment_cup_final_standings_are_not_a_race(self):
        self.assertEqual(
            html_parser.detect_list_type(
                "event_3184_wolv-cup-2020-endergebnis-4-l-int.pdf",
                "WOLV-Cup 2020 Endergebnis nach dem 4. Lauf", False),
            "overall",
        )

    def test_html_mannschaft_champion_rank_propagates_to_all_members(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3831-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(
            html_parser.decode(source.read_bytes()))
        men = next(c for c in categories if c["name"] == "Herren ab 19")
        first = [r for r in men["results"] if r["teamName"] == "HSV Pinkafeld 1"]
        self.assertEqual({r.get("rank") for r in first}, {1})

    def test_text_mannschaft_champion_rank_propagates_to_all_members(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2236-0.txt"
        self.require_source_fixture(source)
        categories = text_parser.parse_text(source.read_text(encoding="cp1252"))
        men = next(c for c in categories if c["name"] == "Herren 19-")
        first = [r for r in men["results"] if r["teamName"] == "OLC Graz 1"]
        self.assertEqual({r.get("rank") for r in first}, {1})

    def test_relay_annotation_in_second_cell_keeps_team_rank(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1738-1.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_relay_document(
            html_parser.decode(source.read_bytes()))
        men = next(c for c in categories if c["name"] == "Herren 17- NÖ")
        first = [r for r in men["results"] if r["teamNumber"] == "117"]
        self.assertEqual({r.get("rank") for r in first}, {1})

    def test_relay_first_finisher_recovers_rank_when_nested_annotation_drops(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2713-1.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_relay_document(
            html_parser.decode(source.read_bytes()))
        masters = next(c for c in categories if c["name"].startswith("H 150-"))
        first = [r for r in masters["results"] if r["teamNumber"] == "53"]
        self.assertEqual({r.get("rank") for r in first}, {1})

    def test_pdf_relay_dotted_ranks_keep_team_number_and_name(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1455-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_relay_pdf(source)
        men = next(c for c in categories if c["name"] == "Herren -14 / WIEN")
        by_number = {}
        for result in men["results"]:
            by_number.setdefault(result["teamNumber"], result)
        self.assertEqual(
            [(number, by_number[number]["rank"], by_number[number]["teamName"])
             for number in ("36", "39", "37", "38")],
            [("36", 1, "Naturfreunde Wien 2"),
             ("39", 2, "OLT Transdanubien 1"),
             ("37", 3, "Naturfreunde Wien 1"),
             ("38", 4, "OLC Wienerwald 1")],
        )

    def test_glued_pl_name_and_year_club_columns_are_recovered(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "5419-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_glued_header_pdf(source)
        girls = next(c for c in categories if c["name"] == "Damen bis 12")
        self.assertEqual(len(girls["results"]), 2)
        self.assertEqual(
            (girls["results"][0]["name"], girls["results"][0]["rank"],
             girls["results"][0]["club"], girls["results"][0]["yearOfBirth"]),
            ("Pötsch Alma", 1, "OLC Graz", 2014),
        )
        self.assertEqual(girls["results"][1]["status"], "dnf")
        newcomers = next(c for c in categories if c["name"] == "Neulinge")
        wolfgang = next(r for r in newcomers["results"] if r["name"] == "Neuhold Wolfgang")
        self.assertEqual(wolfgang["rank"], 2)
        women_45 = next(c for c in categories if c["name"] == "Damen ab 45")
        champion = women_45["results"][0]
        self.assertEqual((champion["name"], champion["rank"], champion["championship"]),
                         ("Walther Katja", 1, "ÖM"))

    def test_reports_and_si_protocols_are_not_race_results(self):
        self.assertEqual(html_parser.detect_list_type(
            "event_5060_bericht-schachol-wien-2025.pdf", "", False), "overall")
        self.assertEqual(html_parser.detect_list_type(
            "event_1915_soc2017-3-si-a.pdf", "", False), "overall")
        self.assertEqual(
            html_parser.detect_list_type(
                "event_4408_wolv-cup-2024-endergebnis-nach-5-laufen-int.pdf",
                "WOLV-Cup 2024", False),
            "overall",
        )

    def test_school_score_pdf_keeps_true_placements(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1943-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_school_score_pdf(source)
        men = next(c for c in categories if c["name"] == "Oberstufe – männlich")
        self.assertEqual(
            [(r["rank"], r["name"]) for r in men["results"][:3]],
            [(1, "Zippusch Patrick"), (2, "Prets Johannes"), (3, "Shevlin John")],
        )
        lower = pdf_parser.parse_school_score_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "1943-0.pdf")
        boys = next(c for c in lower if c["name"] == "Unterstufe – männlich")
        self.assertEqual((boys["results"][0]["rank"], boys["results"][0]["name"]),
                         (1, "WIESER Lukas"))

    def test_school_final_pdf_splits_classes_and_glued_columns(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1204-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_school_final_pdf(source)
        self.assertGreaterEqual(len(categories), 6)
        boys = next(c for c in categories if c["name"] == "Unterstufe männlich")
        self.assertEqual(
            (boys["results"][0]["rank"], boys["results"][0]["name"],
             boys["results"][0]["club"], boys["results"][0]["timeS"]),
            (1, "Ritter Jan", "FF Fürstenfeld", 986),
        )

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

    def test_scored_team_keeps_member_mp_without_invalidating_team(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            """CREATE TABLE result (
                 id INTEGER PRIMARY KEY, stage_id INTEGER,
                 result_list_id TEXT, category TEXT, result_kind TEXT,
                 rank INTEGER, status TEXT, individual_status TEXT,
                 team_status TEXT, team_number TEXT, team_name TEXT,
                 leg_number INTEGER, leg_count INTEGER, club TEXT, note TEXT
               )""")
        for rid, individual_status in ((1, "ok"), (2, "mp"), (3, "ok")):
            connection.execute(
                """INSERT INTO result VALUES (
                     ?, 1, 'list', 'School', 'team', 2, 'ok', ?, 'ok',
                     '2', 'HS Egg 2', ?, 3, 'HS Egg', '')""",
                (rid, individual_status, rid))
        build_db.normalize_team_results(connection.cursor())
        rows = connection.execute(
            "SELECT status, individual_status FROM result ORDER BY id").fetchall()
        self.assertEqual(rows, [("ok", "ok"), ("ok", "mp"), ("ok", "ok")])

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
        kids = next(c for c in categories if c["name"] == "Kids")
        dora = next(r for r in kids["results"] if r["name"] == "Dóra Bereczky")
        self.assertEqual(dora["status"], "ok")
        open_class = next(c for c in categories if c["name"] == "Men Open")
        tobias = next(r for r in open_class["results"] if r["name"] == "Tobias Bartok")
        self.assertEqual(
            (tobias["club"], tobias["timeS"], tobias["status"]),
            ("", 1207, "ok"))
        shifted = ROOT / "data" / "raw" / "anne" / "files" / "5038-0.html"
        self.require_source_fixture(shifted)
        shifted_categories = html_parser.parse_bracket_html(
            html_parser.decode(shifted.read_bytes()))
        men_a = next(c for c in shifted_categories if c["name"] == "Herren A")
        griff = next(r for r in men_a["results"] if r["name"] == "Griff Daniel")
        self.assertEqual((griff["club"], griff["timeText"], griff["timeS"]),
                         ("", "19:46", 1186))

    def test_legacy_ak_prefix_and_omt_are_normalized(self):
        parsed = parse_flow_row(
            "AK 1 Kerschbaumer Gernot vereinslos 14:22", {"vereinslos": "vereinslos"})
        self.assertEqual(parsed["names"], ["Kerschbaumer Gernot"])
        self.assertIsNone(parsed["rank"])
        self.assertTrue(parsed["outOfCompetition"])
        self.assertEqual(parse_status("OMT"), "dns")

    def test_swiss_status_phrases_preserve_every_source_entry(self):
        for text, status in {
            "1 Po fehlt": "mp",
            "1 Po falsch": "mp",
            "Posten fehlen": "mp",
            "aufgegeben": "dnf",
            "Maximalzeit": "dsq",
            "keine Zielzeit": "dnf",
            "keine e-Card": "dns",
        }.items():
            self.assertEqual(parse_status(text), status)
        source = ROOT / "data" / "raw" / "anne" / "files" / "2474-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_flowing_pdf(source)
        self.assertTrue(categories)
        for category in categories:
            starter_units = []
            for index, result in enumerate(category["results"]):
                if result.get("resultKind") == "pair":
                    starter_units.append((
                        "pair", result.get("rank"), result.get("status"),
                        result.get("timeS"), result.get("club")))
                else:
                    starter_units.append(("row", index))
            self.assertEqual(len(set(starter_units)), category["declaredStarters"],
                             category["name"])

        mp = parse_flow_row(
            "AK 725 Belzik Karl vereinslos Fehlst", {"vereinslos": "vereinslos"})
        [mp_result] = pdf_parser.flow_results(mp)
        self.assertTrue(mp_result["outOfCompetition"])
        self.assertEqual(mp_result["status"], "mp")

    def test_labyrinth_matrix_uses_total_and_separate_gender_categories(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1372-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_labyrinth_challenge_pdf(source)
        self.assertEqual([category["name"] for category in categories],
                         ["Damen", "Herren"])
        self.assertEqual([category["declaredStarters"] for category in categories],
                         [21, 30])
        women = categories[0]["results"]
        maya = next(result for result in women if result["name"] == "Kastner Maya")
        self.assertEqual((maya["rank"], maya["timeS"], maya["timeText"]),
                         (1, 71, "1:11"))
        lisa = next(result for result in women if result["name"] == "Kirchberger Lisa")
        self.assertEqual((lisa.get("rank"), lisa["status"]), (None, "dnf"))
        harald = next(result for result in categories[1]["results"]
                      if result["name"] == "Lipphart-Kirchmeir Harald")
        self.assertEqual((harald.get("rank"), harald["status"]), (None, "mp"))

    def test_wintertour_uses_penalized_total_and_keeps_late_categories(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3924-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_wintertour_pdf(source)
        self.assertEqual(
            [category["name"] for category in categories[-4:]],
            ["Kurz Anspruchsvoll Damen", "Kurz Anspruchsvoll Herren",
             "Lang Anspruchsvoll Damen", "Lang Anspruchsvoll Herren"])
        women = next(category for category in categories
                     if category["name"] == "Kurz Anspruchsvoll Damen")
        corinna = next(result for result in women["results"]
                       if result["name"] == "Biel Corinna")
        self.assertEqual((corinna["rank"], corinna["timeText"], corinna["timeS"]),
                         (6, "00:28:18", 1698))
        long_men = next(category for category in categories
                        if category["name"] == "Lang Anspruchsvoll Herren")
        self.assertEqual(len(long_men["results"]), 22)
        plohn = next(result for result in long_men["results"]
                     if result["name"] == "Plohn Markus")
        self.assertEqual((plohn["rank"], plohn["timeText"]), (2, "00:24:54"))

    def test_meos_column_rounding_does_not_shift_club_or_finish_time(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3739-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_meos_individual_pdf(source)
        self.assertTrue(categories)
        for category in categories:
            units = {
                ("pair", result.get("teamNumber"))
                if result.get("resultKind") == "pair" else ("row", index)
                for index, result in enumerate(category["results"])
            }
            self.assertEqual(len(units), category["declaredStarters"], category["name"])
        women_a = next(category for category in categories
                       if category["name"] == "Damen A")
        kiara = next(result for result in women_a["results"]
                     if result["name"] == "Kiara Piskorz")
        self.assertEqual((kiara["club"], kiara["timeText"], kiara["timeS"]),
                         ("WAT-OL", "20:43", 1243))
        women_c = next(category for category in categories
                       if category["name"] == "Damen C")
        self.assertEqual(
            {result["name"] for result in women_c["results"]
             if result.get("resultKind") == "pair"},
            {"Emilia Hohenecker", "Lea Hohnold", "Golsamin Harandi", "Emma Huszar"})

    def test_school_text_columns_are_separate_team_members(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "5175-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        self.assertTrue(categories)
        for category in categories:
            units = {
                ("team", result.get("teamNumber"))
                if result.get("resultKind") in {"pair", "team"}
                else ("row", index)
                for index, result in enumerate(category["results"])
            }
            self.assertEqual(len(units), category["declaredStarters"], category["name"])
        men_2 = next(category for category in categories
                     if category["name"] == "Herren 2")
        timo = next(result for result in men_2["results"]
                    if result["name"] == "MORAWEC Timo")
        self.assertEqual((timo["club"], timo["timeText"], timo["timeS"]),
                         ("GRG 19 Billrothstr", "17:32", 1052))
        self.assertIn("HONECK Konstantin", {result["name"] for result in men_2["results"]})
        beginners = next(category for category in categories
                         if category["name"] == "Damen Neulinge")
        first_team = {result["name"] for result in beginners["results"]
                      if result.get("rank") == 1}
        self.assertEqual(first_team, {"MIRTH Mariella", "STOHL Annika"})

    def test_relay_source_count_includes_dns_teams_without_member_names(self):
        html_source = ROOT / "data" / "raw" / "anne" / "files" / "1618-1.html"
        self.require_source_fixture(html_source)
        html_categories = html_parser.parse_relay_document(
            html_parser.decode(html_source.read_bytes()))
        for category in html_categories:
            self.assertEqual(category.get("sourceUnitCount"),
                             category["declaredStarters"], category["name"])
        html_memberless = [row for category in html_categories
                           for row in category["results"]
                           if row.get("memberlessTeam")]
        self.assertTrue(html_memberless)
        self.assertTrue(all(row["status"] == "dns" for row in html_memberless))

        pdf_source = ROOT / "data" / "raw" / "anne" / "files" / "3633-1.pdf"
        self.require_source_fixture(pdf_source)
        pdf_categories = pdf_parser.parse_relay_pdf(pdf_source)
        for category in pdf_categories:
            self.assertEqual(category.get("sourceUnitCount"),
                             category["declaredStarters"], category["name"])
        pdf_memberless = [row for category in pdf_categories
                          for row in category["results"]
                          if row.get("memberlessTeam")]
        self.assertTrue(pdf_memberless)
        self.assertTrue(all(row["status"] == "dns" for row in pdf_memberless))

        current_source = ROOT / "data" / "raw" / "anne" / "files" / "5133-1.html"
        self.require_source_fixture(current_source)
        current_categories = html_parser.parse_relay_document(
            html_parser.decode(current_source.read_bytes()))
        youth = next(category for category in current_categories
                     if category["name"] == "Mixed Staffel bis 16")
        memberless_by_number = {
            row.get("teamNumber"): row for row in youth["results"]
            if row.get("memberlessTeam")
        }
        self.assertEqual(set(memberless_by_number), {"74", "86"})
        self.assertEqual(
            {row["teamName"] for row in memberless_by_number.values()},
            {"HSV OL Wiener Neustadt 3", "Orienteering Innsbruck Imst 2"})

        duplicated_label_source = (ROOT / "data" / "raw" / "anne" / "files" /
                                   "3824-3.pdf")
        self.require_source_fixture(duplicated_label_source)
        duplicated_categories = pdf_parser.parse_relay_pdf(duplicated_label_source)
        women_120 = next(category for category in duplicated_categories
                         if category["name"] == "Damen 120")
        self.assertEqual(women_120.get("sourceUnitCount"),
                         women_120["declaredStarters"])
        self.assertEqual(
            {result["name"] for result in women_120["results"]
             if result.get("teamName") == "HSV Ried HSV Ried"},
            {"Karoline Fischerleitner", "Doris Gittmaier", "Ingrid Gattringer"})
        adult_open = next(category for category in duplicated_categories
                          if category["name"] == "Offen Erwachsen")
        self.assertEqual(adult_open.get("sourceUnitCount"),
                         adult_open["declaredStarters"])
        self.assertEqual(
            {result["name"] for result in adult_open["results"]
             if result.get("teamName") == "HSV Großmittel"},
            {"Jakob Pauser", "Kathrin Kollendorfer", "Dominik Lapornik"})

    def test_rankless_status_team_with_club_label_is_not_a_member(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3825-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_relay_pdf(source)
        by_name = {category["name"]: category for category in categories}
        for category_name in ("Mixed Staffel bis 16", "Mixed Staffel ab 17", "Offen"):
            category = by_name[category_name]
            self.assertEqual(category.get("sourceUnitCount"),
                             category["declaredStarters"], category_name)

        youth = by_name["Mixed Staffel bis 16"]
        kitzbuehel = [row for row in youth["results"]
                      if row.get("teamName") == "Naturfreunde Kitzbühel 1"]
        self.assertEqual(len(kitzbuehel), 1)
        self.assertTrue(kitzbuehel[0]["memberlessTeam"])
        self.assertEqual(kitzbuehel[0]["status"], "dns")
        hsv_ried = [row for row in youth["results"]
                    if row.get("teamName") == "HSV Ried HSV Ried"]
        self.assertEqual(
            {row["name"] for row in hsv_ried},
            {"Clemens Fischerleitner", "Anna Gruber", "Lorenz Fischerleitner"})
        self.assertEqual({row["status"] for row in hsv_ried}, {"dnf"})

        adult = by_name["Mixed Staffel ab 17"]
        suso = [row for row in adult["results"]
                if row.get("teamName") == "SU Schöckl Orienteering SUSO"]
        self.assertEqual(len(suso), 4)
        self.assertEqual({row["status"] for row in suso}, {"dsq"})

        ski_source = ROOT / "data" / "raw" / "anne" / "files" / "1890-1.pdf"
        self.require_source_fixture(ski_source)
        ski_categories = pdf_parser.parse_relay_pdf(ski_source)
        masters = next(category for category in ski_categories
                       if category["name"] == "Herren 50-")
        self.assertEqual(masters.get("sourceUnitCount"),
                         masters["declaredStarters"])
        self.assertEqual(
            {row["name"] for row in masters["results"]
             if row.get("teamName") == "GO Harzberg GO"},
            {"Klaus Kramer", "Peter Illig"})
        youth = next(category for category in ski_categories
                     if category["name"] == "Herren -18")
        self.assertEqual(youth.get("sourceUnitCount"), youth["declaredStarters"])
        self.assertNotIn("N 03 N", {row.get("teamName") for row in youth["results"]})

    def test_meos_source_count_includes_memberless_dns_team(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3504-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_meos_relay_pdf(source)
        open_class = next(category for category in categories
                          if category["name"] == "Mix Offen")
        self.assertEqual(open_class.get("sourceUnitCount"), 10)
        self.assertEqual(open_class["declaredStarters"], 10)
        team_73 = [row for row in open_class["results"]
                   if row.get("teamName") == "Team 73"]
        self.assertEqual(len(team_73), 1)
        self.assertTrue(team_73[0]["memberlessTeam"])
        self.assertEqual(team_73[0]["status"], "dns")

    def test_mannschaft_placeholder_members_keep_personless_team_result(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2612-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(
            html_parser.decode(source.read_bytes()))
        open_class = next(category for category in categories
                          if category["name"] == "Offen")
        team = [row for row in open_class["results"]
                if row.get("teamNumber") == "127"]
        self.assertEqual(len(team), 1)
        self.assertTrue(team[0]["memberlessTeam"])
        self.assertEqual(team[0]["teamName"], "GOs Harzberg 1")
        self.assertEqual((team[0]["rank"], team[0]["teamTimeS"]), (10, 5175))

    def test_inline_mannschaft_pdf_keeps_teams_members_and_statuses(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3334-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_inline_mannschaft_pdf(source)

        self.assertEqual(len(categories), 12)
        self.assertTrue(all(
            category["declaredStarters"] == category["sourceUnitCount"]
            for category in categories))
        men = next(category for category in categories
                   if category["name"] == "Herren ab 19")
        winner = [row for row in men["results"]
                  if row.get("teamName") ==
                     "ASKÖ Henndorf Orienteering AHDO 1"]
        self.assertEqual({row["name"] for row in winner},
                         {"Ebster Leon", "Merl Robert", "Wartbichler Christi"})
        self.assertEqual({row["rank"] for row in winner}, {1})
        self.assertEqual({row["teamTimeS"] for row in winner}, {4398})

        ooc = [row for row in men["results"]
               if row.get("teamName") == "OLC Graz 2"]
        self.assertEqual({row["name"] for row in ooc}, {"Glatz Ewald"})
        self.assertTrue(all(row["outOfCompetition"] for row in ooc))
        self.assertEqual({row["teamStatus"] for row in ooc}, {"mp"})

        absent = [row for row in men["results"]
                  if row.get("teamName") == "OC Fürstenfeld OCFF2"]
        self.assertEqual(len(absent), 1)
        self.assertTrue(absent[0]["memberlessTeam"])
        self.assertEqual(absent[0]["status"], "dns")

    def test_team_time_interleaved_with_name_keeps_its_team(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4149-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_relay_pdf(source, team_mode=True)
        masters = next(category for category in categories
                       if category["name"] == "S Herren 55-")
        self.assertEqual(masters.get("sourceUnitCount"),
                         masters["declaredStarters"])
        team = [row for row in masters["results"]
                if row.get("teamNumber") == "47"]
        self.assertEqual({row["name"] for row in team},
                         {"Eva Breitschädel", "Christian Breitschädel"})
        self.assertEqual({row["rank"] for row in team}, {3})
        self.assertEqual({row["teamTimeS"] for row in team}, {7498})

    def test_interleaved_hungarian_hours_and_statuses_survive(self):
        first_source = ROOT / "data" / "raw" / "anne" / "files" / "2765-0.pdf"
        dnf_source = ROOT / "data" / "raw" / "anne" / "files" / "2765-6.pdf"
        self.require_source_fixture(first_source)
        self.require_source_fixture(dnf_source)
        first_categories, _ = pdf_parser.parse_pdf(first_source)
        nyt = next(category for category in first_categories if category["name"] == "NYT")
        abigel = next(result for result in nyt["results"]
                      if result["name"] == "Soltész Abigél")
        self.assertEqual((abigel["rank"], abigel["timeText"], abigel["timeS"]),
                         (5, "1:42:53", 6173))

        dnf_categories, _ = pdf_parser.parse_pdf(dnf_source)
        nyk = next(category for category in dnf_categories if category["name"] == "NYK")
        self.assertEqual(len(nyk["results"]), nyk["declaredStarters"])
        self.assertEqual(
            {result["name"]: result["status"] for result in nyk["results"]
             if result["name"] in {"Fuchs Lívia", "Hites Gergõ"}},
            {"Fuchs Lívia": "dnf", "Hites Gergõ": "dnf"})

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

    def test_dns_is_not_excluded_when_declared_count_includes_it(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "3474-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_bracket_html(html_parser.decode(source.read_bytes()))
        men = next(c for c in categories if c["name"] == "Men Open")
        team = next(c for c in categories if c["name"] == "Team Challenge")
        self.assertEqual(build_db.normalized_source_unit_count(men["results"]), 28)
        self.assertEqual(build_db.normalized_source_unit_count(team["results"]), 4)

    def test_corrupted_zeitueberschreitung_status_is_dsq(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1716-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        h20 = next(c for c in categories if c["name"] == "Herren 18-20")
        flachberger = next(r for r in h20["results"] if r["name"] == "Flachberger Jakob")
        self.assertEqual((flachberger["timeText"], flachberger["status"]),
                         ("Zeitï¿½b", "dsq"))

    def test_incomplete_member_of_three_person_source_row_is_preserved(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "856-0.html"
        self.require_source_fixture(source)
        fixed_text = html.unescape(re.sub(
            r"<[^>]+>", "",
            html_parser.decode(source.read_bytes()),
        ))
        categories = text_parser.parse_text(fixed_text)
        women = next(c for c in categories if c["name"] == "Damen E")
        fifth = [r for r in women["results"] if r.get("rank") == 5]
        self.assertEqual(
            [(r["name"], bool(r.get("identityExcluded"))) for r in fifth],
            [("Laura", True), ("Maria Reil", False),
             ("Christina Hell", False)],
        )
        self.assertTrue(all(r["resultKind"] == "pair" for r in fifth))

    def test_score_club_overflow_before_status_is_not_a_time(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4106-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        women = next(c for c in categories if c["name"] == "Damen E")
        cabala = next(r for r in women["results"] if r["name"] == "Cabala Laura")
        self.assertEqual(cabala["status"], "dnf")
        self.assertEqual(cabala["timeText"], "DNF")
        self.assertEqual(cabala["club"], "BG/BRG Zehnergasse Wiener Neus")

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

    def test_broken_relay_member_header_is_not_an_extra_leg(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1123-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_relay_pdf(source)
        h16 = next(c for c in categories if c["name"] == "SBG H 16 -")
        team_65 = [r for r in h16["results"] if r.get("teamNumber") == "65"]
        self.assertEqual([r.get("leg") for r in team_65], [1, 2, 3, 4])
        h15 = next(c for c in categories if c["name"] == "SBG H -15")
        team_97 = [r for r in h15["results"] if r.get("teamNumber") == "97"]
        self.assertEqual([r.get("leg") for r in team_97], [1, 2])
        self.assertFalse(any(
            r.get("name") == "Name Jg Z eit"
            for category in categories for r in category["results"]))

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
        self.assertEqual(
            html_parser.detect_list_type(
                "ergebnis-nach-kategorie-gesamt.pdf",
                "NÖ MS Ultralang 2024\nPl Name Verein Zeit",
                False,
            ),
            "race",
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

        open_class = next(c for c in categories if c["name"] == "Offen")
        self.assertEqual(open_class.get("sourceUnitCount"), 18)
        self.assertEqual(open_class["declaredStarters"], 18)
        self.assertNotIn("Ben und andere",
                         {result["name"] for result in open_class["results"]})

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

    def test_shifted_status_column_keeps_unranked_html_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2206-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        youth = next(c for c in categories if c["name"] == "Hamen 15-")
        self.assertEqual((youth["declaredStarters"], len(youth["results"])), (23, 23))
        by_name = {result["name"]: result for result in youth["results"]}
        self.assertEqual(by_name["Michael Auer"]["status"], "dns")
        self.assertEqual(by_name["Leo Pauser"]["status"], "dns")

        source = ROOT / "data" / "raw" / "anne" / "files" / "3925-0.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        men = next(c for c in categories if c["name"] == "Kurz Einfach Herren")
        self.assertEqual((men["declaredStarters"], len(men["results"])), (7, 7))
        siegl = next(result for result in men["results"] if result["name"] == "Siegl Niklas")
        self.assertEqual((siegl["timeText"], siegl["status"]), ("Disqu", "dsq"))

    def test_html_clipped_official_club_suffix_is_removed_from_status(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "115-0.html"
        self.require_source_fixture(source)
        categories = text_parser.parse_text(text_parser.extract_pre_blocks(
            html_parser.decode(source.read_bytes())))
        men = next(c for c in categories if c["name"] == "Herren 45-")
        wallas = next(result for result in men["results"] if result["name"] == "Klaus Wallas")
        self.assertEqual(
            (wallas["club"], wallas["timeText"], wallas["status"]),
            ("Naturfreunde Villach - Orienteering", "N Ang", "dns"),
        )

    def test_text_clipped_official_club_suffix_is_removed_from_status(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4120-0.txt"
        self.require_source_fixture(source)
        categories = text_parser.parse_text(text_parser.decode(source.read_bytes()))
        women = next(c for c in categories if c["name"] == "Da 19-")
        sandrisser = next(
            result for result in women["results"] if result["name"] == "Sandrisser Lisi")
        self.assertEqual(
            (sandrisser["club"], sandrisser["timeText"], sandrisser["status"]),
            ("Naturfreunde Villach - Orienteering", "N Ang", "dns"),
        )

    def test_nolv_school_pdf_uses_explicit_school_and_result_columns(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2262-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_nolv_school_result_pdf(source)
        by_name = {category["name"]: category for category in categories}
        self.assertGreaterEqual(len(categories), 5)
        women = by_name["Damen E"]
        auer = next(result for result in women["results"] if result["name"] == "Auer Nina")
        self.assertEqual(
            (auer["club"], auer["sourceCategory"], auer["timeText"], auer["status"]),
            ("NMS Gloggnitz", "DE", "dis.", "dsq"),
        )
        men = by_name["Herren E"]
        kayal = next(result for result in men["results"] if result["name"] == "Kayal Riad")
        self.assertEqual((kayal["club"], kayal["timeText"], kayal["status"]),
                         ("NMS Gloggnitz", "dis.", "dsq"))

    def test_partial_club_tail_restores_given_name_from_club_column(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1710-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        rows = [row for category in categories for row in category["results"]]
        christ = next(row for row in rows if row["name"] == "Ziegerhofer M. Christ")
        self.assertEqual(
            (christ["club"], christ["timeText"], christ["status"]),
            ("HSV OL Wiener Neustadt", "N Ang", "dns"),
        )
        reisenberger = next(
            row for row in rows if row["name"] == "Reisenberger Roland")
        self.assertEqual(reisenberger["club"], "Orienteering Klosterneuburg")

    def test_named_family_row_with_blank_result_is_preserved(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2375-4.html"
        self.require_source_fixture(source)
        categories = html_parser.parse_document(html_parser.decode(source.read_bytes()))
        family = next(c for c in categories if c["name"] == "Family")
        self.assertEqual((family["declaredStarters"], len(family["results"])), (12, 12))
        boehm = next(result for result in family["results"] if result["name"] == "Böhm Niklas")
        self.assertEqual((boehm["resultKind"], boehm["status"]), ("family", "unknown"))

    def test_bracket_html_does_not_treat_ok_club_as_result_status(self):
        source = """
        <table>
          <tr><td></td><td>Men 50+</td><td>(1 / 1)</td><td>Time</td></tr>
          <tr><td></td><td>1.</td><td>Milan Venhoda</td>
              <td>OK Jihlava</td><td>Czech Republic</td><td>18:55</td></tr>
        </table>
        """
        categories = html_parser.parse_bracket_html(source)
        self.assertEqual(len(categories), 1)
        result = categories[0]["results"][0]
        self.assertEqual(
            (result["club"], result["timeText"], result["timeS"], result["status"]),
            ("OK Jihlava", "18:55", 1135, "ok"),
        )

    def test_meos_pairs_keep_partial_and_memberless_source_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4087-1.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_meos_individual_pdf(source)
        boys = next(category for category in categories if category["name"] == "H-12")
        self.assertEqual(pdf_parser.category_competitor_unit_count(boys), 3)
        self.assertEqual(
            {result["name"] for result in boys["results"]},
            {"Marc Maier", "Nicolas Schall", "Jonas Kofler", "Corinna Kofler",
             "Benjamin Reiner"},
        )
        benjamin = next(result for result in boys["results"]
                        if result["name"] == "Benjamin Reiner")
        self.assertIn("Begleitung", benjamin["note"])

        source = ROOT / "data" / "raw" / "anne" / "files" / "4496-0.pdf"
        self.require_source_fixture(source)
        _generic, head_text = pdf_parser.parse_pdf(source)
        self.assertIsNotNone(pdf_parser.MEOS_CLASS_HEADER_RE.search(head_text))
        categories = pdf_parser.parse_meos_individual_pdf(source)
        men = next(category for category in categories if category["name"] == "Herren E")
        self.assertEqual(pdf_parser.category_competitor_unit_count(men), 5)
        self.assertIn("Leo Maurer", {result["name"] for result in men["results"]})
        nameless = next(result for result in men["results"]
                        if result.get("memberlessTeam"))
        self.assertEqual(
            (nameless["rank"], nameless["club"], nameless["timeS"],
             nameless["resultKind"]),
            (4, "WAT-OL", 9000, "pair"),
        )

        source = ROOT / "data" / "raw" / "anne" / "files" / "3503-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_meos_individual_pdf(source)
        accompanied = next(category for category in categories
                           if category["name"] == "Kind m. Begl.")
        self.assertEqual(pdf_parser.category_competitor_unit_count(accompanied), 10)
        self.assertTrue({"Paul", "Petra"}.issubset(
            {result["name"] for result in accompanied["results"]}))

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
        self.require_source_fixture(school)
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

    def test_skio_mixed_relay_keeps_combined_clubs_and_repeated_legs(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "5204-0.pdf"
        self.require_source_fixture(source)
        categories = pdf_parser.parse_relay_pdf(source)
        self.assertEqual(
            {c["name"]: (c["declaredStarters"], c.get("sourceUnitCount"))
             for c in categories},
            {"Mixed Staffel bis 17": (9, 9), "Mixed Staffel ab 18": (10, 10),
             "Mixed Staffel ab 45": (12, 12), "Offen": (7, 7)},
        )

        youth = next(c for c in categories if c["name"] == "Mixed Staffel bis 17")
        champions = [r for r in youth["results"]
                     if r["teamName"] == "NF Kitzb./HSV Wr. Neust."]
        self.assertEqual([(r["leg"], r["name"], r["timeText"]) for r in champions], [
            (1, "Lisa Hauser", "8:03"), (2, "David Kaltenbacher", "6:39"),
            (3, "Lisa Hauser", "8:18"), (4, "David Kaltenbacher", "6:00"),
        ])
        self.assertTrue(all(r.get("preserveRepeatedRelayLeg") for r in champions))
        self.assertIn("AHDO / HSV Wr. Neust.", {r["teamName"] for r in youth["results"]})
        self.assertNotIn("Pia Aspalter er", {r["name"] for r in youth["results"]})
        pia = [r for r in youth["results"] if r["name"] == "Pia Aspalter"]
        self.assertEqual([(r["leg"], r["timeText"]) for r in pia],
                         [(1, "10:36"), (3, "er 11")])

        adults = next(c for c in categories if c["name"] == "Mixed Staffel ab 18")
        self.assertIn("OLC Graz / LZ OMAHA", {r["teamName"] for r in adults["results"]})
        self.assertIn("LZ OMAHA /OCFF", {r["teamName"] for r in adults["results"]})
        self.assertNotIn("Lisa Habenicht ht", {r["name"] for r in adults["results"]})
        wrapped_team_times = {
            r["teamName"]: r["teamTimeS"] for r in adults["results"]
            if r["leg"] == 1 and r.get("teamTimeS") is not None
        }
        self.assertEqual(wrapped_team_times["OLC Graz"], 3661)
        self.assertEqual(wrapped_team_times["AHDO / LK Innsbruck"], 3679)
        self.assertEqual(wrapped_team_times["HSV Großmittel"], 3802)

        masters = next(c for c in categories if c["name"] == "Mixed Staffel ab 45")
        self.assertIn("NF Wien / OLC Wienerw.",
                      {r["teamName"] for r in masters["results"]})
        self.assertIn("OLC Graz / OLC Wienerw.",
                      {r["teamName"] for r in masters["results"]})

    def test_ok_suffix_category_does_not_leak_into_previous_class(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1049-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)

        women_50 = next(c for c in categories if c["name"].replace(" ", "") == "Damen50-")
        beginners = next(c for c in categories if c["name"] == "Neulinge - OK")
        self.assertEqual(women_50["declaredStarters"], 7)
        self.assertEqual(len(women_50["results"]), 7)
        self.assertEqual(beginners["declaredStarters"], 3)
        self.assertEqual(len(beginners["results"]), 3)
        self.assertEqual(
            {r["name"] for r in beginners["results"]},
            {"Eckart Karl", "Kalinová Markéta", "Emde Sabine"},
        )
        elite = next(c for c in categories if c["name"] == "Damen 21E CZ")
        lamichova = next(r for r in elite["results"] if r["name"] == "Lamichová Martina")
        self.assertEqual(
            (lamichova["club"], lamichova["timeText"], lamichova["timeS"]),
            ("XHK GHOST RUBENA racing team", "1:13:22", 4402),
        )

    def test_fully_interleaved_school_club_and_time_are_recovered(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4089-1.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        girls = next(c for c in categories if c["name"] == "Unterstufe weiblich")
        expected = {
            "Oberneuwirther Isabel": ("Christian-Doppler-Gymnasium", "38:20", 2300),
            "Schimon Amelie": ("Christian-Doppler-Gymnasium", "46:19", 2779),
            "Ginzinger Emily": ("Christian-Doppler-Gymnasium", "53:04", 3184),
            "Kreuzer Hannah": ("Christian-Doppler-Gymnasium", "1:06:25", 3985),
        }
        for name, wanted in expected.items():
            row = next(r for r in girls["results"] if r["name"] == name)
            self.assertEqual((row["club"], row["timeText"], row["timeS"]), wanted)

    def test_interleaved_school_nat_column_is_removed_from_club(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "4089-3.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        rows = [row for category in categories for row in category["results"]]

        expected = {
            "Falkensammer Julia":
                ("BG/BRG Villach St. Martin", "K", "09"),
            "Huber Pia":
                ("BG/BRG/BORG Oberschützen", "B", "11"),
            "Aigmüller Camilla":
                ("BG/BRG Kirchengasse Graz", "St", "06"),
            "Oberneuwirther Isabel":
                ("Christian-Doppler-Gymnasium", "S", "11"),
        }
        for name, wanted in expected.items():
            row = next(result for result in rows if result["name"] == name)
            self.assertEqual(
                (row["club"], row["sourceNat"], row["sourceJg"]), wanted)

        girls = next(c for c in categories if c["name"] == "Unterstufe weiblich")
        raggl = next(r for r in girls["results"] if r["name"] == "Raggl Emily")
        kollenhofer = next(
            r for r in girls["results"] if r["name"] == "Kollenhofer Johanna")
        self.assertEqual((raggl["status"], raggl.get("rank")), ("mp", None))
        self.assertEqual((kollenhofer["status"], kollenhofer.get("rank")),
                         ("dns", None))

    def test_school_olympics_keeps_both_result_dates_as_stages(self):
        documents = [
            json.loads((ROOT / "data" / "normalized" / file_name).read_text())
            for file_name in ("4089-1.json", "4089-3.json")
        ]
        self.assertEqual({doc["listType"] for doc in documents}, {"race"})
        self.assertEqual({doc["docDate"] for doc in documents},
                         {"2023-05-24", "2023-05-25"})
        self.assertIn(4089, build_db.LEGACY_MULTIDAY_EVENT_OVERRIDES)

    def test_school_result_and_laufzeit_headers_are_elapsed_times(self):
        fixtures = {
            "917-0.pdf": ("D3", "Pfanner Anja", "00:29:05", 1745),
            "993-0.pdf": ("D3", "Lorenz Emilie", "00:11:59", 719),
            "3801-0.pdf": ("Damen 2", "Bischofberger Nina", "0:26:52", 1612),
        }
        for file_name, (category_name, name, time_text, time_s) in fixtures.items():
            source = ROOT / "data" / "raw" / "anne" / "files" / file_name
            self.require_source_fixture(source)
            categories, _ = pdf_parser.parse_pdf(source)
            category = next(c for c in categories if c["name"] == category_name)
            result = next(r for r in category["results"] if r["name"] == name)
            self.assertEqual((result["timeText"], result["timeS"]), (time_text, time_s))

        source = ROOT / "data" / "raw" / "anne" / "files" / "993-0.pdf"
        categories, _ = pdf_parser.parse_pdf(source)
        d2 = next(c for c in categories if c["name"] == "D2")
        hold = next(r for r in d2["results"] if r["name"] == "Hold Janine")
        self.assertEqual((hold["club"], hold["status"], hold.get("rank")),
                         ("Lauterach1", "mp", None))
        self.assertNotIn("Janine", {r["name"] for c in categories for r in c["results"]})

        source = ROOT / "data" / "raw" / "anne" / "files" / "3801-0.pdf"
        categories, _ = pdf_parser.parse_pdf(source)
        sample = next(c for c in categories if c["name"] == "Schnupperer")
        first_team = [r for r in sample["results"]
                      if r.get("teamNumber") == "school-group-59"]
        self.assertEqual({r["name"] for r in first_team},
                         {"Lampert Bernhard", "Neubacher Noah"})

        source = ROOT / "data" / "raw" / "anne" / "files" / "1200-0.pdf"
        categories, _ = pdf_parser.parse_pdf(source)
        sample = next(c for c in categories if c["name"] == "Schnupper")
        melissa = next(r for r in sample["results"] if r["name"] == "Melissa")
        self.assertEqual(
            (melissa["club"], melissa["rank"], melissa["status"],
             melissa["timeS"], melissa["note"]),
            ("Egg", 14, "mp", 2229, "Posten 6 (54) fehlt"),
        )

    def test_net_time_penalty_table_uses_gross_time_and_real_categories(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2188-1.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        self.assertEqual({c["name"] for c in categories}, {"D/H Kurz", "D/H Lang"})
        short = next(c for c in categories if c["name"] == "D/H Kurz")
        angus = next(r for r in short["results"] if r["name"] == "Mair Angus")
        self.assertEqual((angus["rank"], angus["timeText"], angus["timeS"]),
                         (6, "01:26:14", 5174))
        long = next(c for c in categories if c["name"] == "D/H Lang")
        isabel = next(r for r in long["results"] if r["name"] == "Hechl Isabel")
        self.assertEqual(isabel["status"], "dns")

    def test_known_embedded_pdf_rows_retain_their_visible_times(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1605-0.pdf"
        self.require_source_fixture(source)
        categories, _ = pdf_parser.parse_pdf(source)
        expected = {
            "Bednarik Martin": ("KOB Cingov Spisska Nova Ves", "33:58", 2038),
            "Bednarikova Tatiana": ("KOB Cingov Spisska Nova Ves", "47:11", 2831),
            "Ladics Thomas+Stephan": ("GRG Alterlaa", "27:01", 1621),
        }
        rows = {r["name"]: r for c in categories for r in c["results"]}
        for name, wanted in expected.items():
            row = rows[name]
            self.assertEqual((row["club"], row["timeText"], row["timeS"]), wanted)

    def test_legacy_pre_relay_counts_teams_and_propagates_status(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1103-0.html"
        text = html_parser.decode(source.read_bytes())
        categories = text_parser.parse_legacy_pre_text(
            text_parser.extract_pre_blocks(text))
        men = next(category for category in categories
                   if category["name"] == "Herren 15-")
        self.assertEqual((men["declaredStarters"], men["sourceUnitCount"]), (10, 10))
        mp_team = [row for row in men["results"]
                   if row.get("teamNumber") == "6"]
        self.assertEqual({row["status"] for row in mp_team}, {"mp"})
        self.assertEqual({row["leg"] for row in mp_team}, {1, 2})

    def test_headerless_pre_results_keep_ak_and_status_rows(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "1409-3.html"
        text = html_parser.decode(source.read_bytes())
        categories = text_parser.parse_legacy_pre_text(
            text_parser.extract_pre_blocks(text))
        self.assertEqual((len(categories), sum(len(c["results"]) for c in categories)),
                         (21, 100))
        h12 = next(category for category in categories
                   if category["name"] == "H 11-12")
        michael = next(row for row in h12["results"]
                       if row["name"] == "Zollner Michael")
        self.assertTrue(michael["outOfCompetition"])
        self.assertEqual((michael["club"], michael["timeText"]),
                         ("HSV Villach", "32:48"))

    def test_positioned_meos_html_reconstructs_regional_and_relay_columns(self):
        individual = ROOT / "data" / "raw" / "anne" / "files" / "1947-0.html"
        categories = html_parser.parse_positioned_document(
            html_parser.decode(individual.read_bytes()))
        self.assertEqual((len(categories), sum(len(c["results"]) for c in categories)),
                         (24, 245))
        self.assertEqual(categories[0]["name"], "Wien H14")
        noe_h45 = next(c for c in categories if c["name"] == "NO H45")
        schuller = next(r for r in noe_h45["results"]
                        if r["name"] == "Schuller Georg")
        self.assertEqual((schuller["status"], schuller["timeText"]),
                         ("unknown", "–"))

        relay = ROOT / "data" / "raw" / "anne" / "files" / "1989-0.html"
        categories = html_parser.parse_positioned_document(
            html_parser.decode(relay.read_bytes()), relay=True)
        first = categories[0]
        self.assertEqual((first["declaredStarters"], first["sourceUnitCount"]),
                         (54, 54))
        self.assertEqual({row["leg"] for row in first["results"][:3]}, {1, 2, 3})

    def test_mtbo_split_attachment_recovers_only_missing_dns_rows(self):
        source = (ROOT / "data" / "raw" / "anne" / "files" /
                  "1909-0.pdf")
        categories = pdf_parser.parse_mtbo_dns_supplement_pdf(source)
        rows = {
            category["name"]: [row["name"] for row in category["results"]]
            for category in categories
        }
        self.assertEqual(rows, {
            "Herren/Damen -14": ["Diesenreiter Ben"],
            "Herren Elite": ["Haselberger Kevin", "Fürnkranz Martin"],
            "Herren 60": [
                "Wendler Michael", "Pirchegger Günter", "Schanes Josef"],
            "Herren 70": ["Fruhwirth Friedrich"],
        })
        self.assertNotIn("Rochford Jan", rows["Herren 60"])
        self.assertTrue(all(
            row["status"] == "dns"
            for category in categories for row in category["results"]))

    def test_meos_single_table_relay_does_not_count_legs_as_teams(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "5156-1.html"
        categories = html_parser.parse_meos_relay_table(
            html_parser.decode(source.read_bytes()))
        for category in categories:
            self.assertEqual(category["sourceUnitCount"],
                             category["declaredStarters"])
        d100 = next(category for category in categories
                    if category["name"] == "D100 W")
        self.assertEqual((d100["sourceUnitCount"], len(d100["results"])), (5, 10))

    def test_custom_pdf_families_recover_complete_result_units(self):
        cases = [
            ("1064-0.pdf", pdf_parser.parse_origare_pdf, 122),
            ("1951-0.pdf", pdf_parser.parse_sime_pdf, 130),
            ("2051-0.pdf", pdf_parser.parse_apprentice_sport_pdf, 87),
            ("2476-0.pdf", pdf_parser.parse_nolv_freeform_school_pdf, 111),
        ]
        for file_name, parser, expected_rows in cases:
            with self.subTest(file_name=file_name):
                source = ROOT / "data" / "raw" / "anne" / "files" / file_name
                categories = parser(source)
                self.assertEqual(sum(len(c["results"]) for c in categories),
                                 expected_rows)

    def test_course_kat_parser_keeps_night_pairs_as_one_ranked_unit(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2102-0.pdf"
        categories = pdf_parser.parse_course_kat_pdf(source)
        h14 = next(category for category in categories
                   if category["sourceCategory"] == "H14")
        self.assertEqual((h14["declaredStarters"], h14["sourceUnitCount"]), (4, 4))
        self.assertEqual(len(h14["results"]), 8)
        self.assertEqual({row["rank"] for row in h14["results"]}, {1, 2, 3, 4})

    def test_hungarian_statuses_and_multiday_reports_are_not_unknown_races(self):
        self.assertEqual(parse_status("hiba"), "mp")
        self.assertEqual(parse_status("nfb"), "dnf")
        self.assertEqual(parse_status("n.i."), "dns")
        self.assertEqual(parse_status("Po.f."), "mp")

        split_source = ROOT / "data" / "raw" / "anne" / "files" / "2364-1.html"
        split_text = html_parser.decode(split_source.read_bytes())
        self.assertEqual(detect_list_type("ergebnisse-full-reszidos.html", split_text),
                         "overall")

        overall_source = ROOT / "data" / "raw" / "anne" / "files" / "2084-2.html"
        overall_text = html_parser.decode(overall_source.read_bytes())
        self.assertEqual(detect_list_type("bartsg-kupa-2017-1-3-tag.html", overall_text),
                         "overall")

    def test_nolv_name_first_pdf_retains_trailing_placement_column(self):
        source = ROOT / "data" / "raw" / "anne" / "files" / "2115-0.pdf"
        categories = pdf_parser.parse_nolv_name_first_pdf(source)
        self.assertEqual(sum(len(c["results"]) for c in categories), 72)
        men = next(c for c in categories if c["name"] == "Herren A")
        self.assertEqual((men["results"][0]["name"], men["results"][0]["rank"]),
                         ("Plohn Markus", 1))
        mp = next(r for r in men["results"] if r["name"] == "Krischan Klaus")
        self.assertEqual((mp["status"], mp.get("rank")), ("mp", None))

    def test_start_finish_notes_do_not_become_elapsed_times(self):
        categories = pdf_parser.parse_flowing_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "4331-0.pdf"
        )
        women_long = next(
            category for category in categories
            if category["name"] == "Damen lang"
        )
        missing_punch = next(
            row for row in women_long["results"]
            if row["name"] == "Magdalena Glasner"
        )
        self.assertEqual(
            (missing_punch["status"], missing_punch["timeText"]),
            ("mp", "Nr.10 fehlt"),
        )
        self.assertNotIn("timeS", missing_punch)

        women_short = next(
            category for category in categories
            if category["name"] == "Damen kurz"
        )
        qualitative = next(
            row for row in women_short["results"]
            if row["name"] == "Miriam Obermüller"
        )
        self.assertEqual(
            (qualitative["status"], qualitative["timeText"]),
            ("ok", "Super gelaufen!"),
        )
        self.assertNotIn("timeS", qualitative)

    def test_club_name_starting_with_ok_does_not_replace_finish_time(self):
        source = (
            ROOT / "data" / "raw" / "anne" / "files" / "870-club0.html"
        )
        categories = club_table_parser.parse_document(
            club_table_parser.decode(source.read_bytes())
        )
        men = next(category for category in categories
                   if category["name"] == "A Herren")
        runner = next(row for row in men["results"]
                      if row["name"] == "Dominik Grünberger")
        self.assertEqual(
            (runner["club"], runner["timeText"], runner["timeS"]),
            ("OK gittis Klosterneuburg", "47:56", 2876),
        )
        novices = next(category for category in categories
                       if category["name"] == "Neulinge")
        mp = next(row for row in novices["results"]
                  if row["name"] == "Phillip Lechthaler")
        self.assertEqual(
            (mp["club"], mp["timeText"], mp["status"]),
            ("OK gittis Klosterneuburg", "Fehlst.", "mp"),
        )

    def test_nolv_overprinted_and_rankless_rows_are_recovered(self):
        categories = pdf_parser.parse_nolv_freeform_school_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "2476-0.pdf"
        )
        hds = next(category for category in categories
                   if category["name"] == "HDS")
        self.assertEqual((hds["declaredStarters"], len(hds["results"])), (22, 22))
        by_name = {row["name"]: row for row in hds["results"]}
        self.assertEqual(by_name["Mayerhofer Alexander"]["rank"], 6)
        self.assertEqual(by_name["Stoier Florian"]["rank"], 13)
        self.assertEqual(by_name["Rabl Nils"]["rank"], 16)
        self.assertEqual(by_name["Degen Paul"]["rank"], 18)
        self.assertEqual(by_name["PISKULA Marc"]["rank"], 21)

    def test_nolv_single_decimal_digit_is_a_trailing_zero(self):
        categories = pdf_parser.parse_nolv_freeform_school_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "2839-0.pdf"
        )
        hds = next(category for category in categories
                   if category["name"] == "HDS")
        by_name = {row["name"]: row for row in hds["results"]}

        self.assertEqual(
            (by_name["Handler Paul"]["timeText"],
             by_name["Handler Paul"]["timeS"]),
            ("34:50", 2090),
        )
        self.assertEqual(
            (by_name["Fischer Alexander"]["timeText"],
             by_name["Fischer Alexander"]["timeS"]),
            ("51:30", 3090),
        )

    def test_family_adventure_numbers_are_bibs_not_placements(self):
        categories = pdf_parser.parse_family_adventure_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "2245-0.pdf"
        )
        men = next(category for category in categories
                   if category["name"] == "Elche männlich")
        schwab = next(row for row in men["results"]
                      if row["name"] == "Schwab Anton")
        self.assertEqual(
            (schwab["sourceBib"], schwab["timeText"], schwab["timeS"]),
            ("1", "59.8", 59),
        )
        self.assertTrue(all("rank" not in row for row in men["results"]))
        self.assertEqual(men["rankingBasis"], "other")

    def test_southeast_plain_categories_do_not_leak_into_each_other(self):
        categories = pdf_parser.parse_southeast_plain_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "1014-0.pdf"
        )
        self.assertEqual([category["name"] for category in categories],
                         ["A", "B", "C", "D"])
        a = categories[0]
        self.assertEqual(
            (a["results"][0]["name"], a["results"][0]["rank"],
             a["results"][0]["timeText"]),
            ("Robert Merl", 1, "22:05"),
        )
        tied = next(row for row in a["results"]
                    if row["name"] == "Florian Exler")
        self.assertEqual(tied["rank"], 9)
        self.assertFalse(any(row["name"] == "Peter Bonek"
                             for row in a["results"]))

    def test_primary_school_rankings_are_split_by_school_and_grade(self):
        categories = pdf_parser.parse_primary_school_team_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "1414-0.pdf"
        )
        self.assertEqual(len(categories), 10)
        self.assertTrue(all(
            category["rankingBasis"] == "score"
            and "Klasse" in category["name"]
            for category in categories
        ))
        bad_erlach = next(
            category for category in categories
            if category["name"]
            == "1.1 km / 12 Posten W · 2. Klasse · VS Bad Erlach"
        )
        self.assertEqual(
            [row["rank"] for row in bad_erlach["results"]
             if row["name"] in {"Reiterer Marlene", "Saufnauer Lena"}],
            [1, 7],
        )

    def test_second_course_without_rank_is_out_of_competition(self):
        categories = pdf_parser.parse_start_finish_class_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "4017-0.pdf"
        )
        b = next(category for category in categories
                 if category["name"] == "B")
        axel = next(row for row in b["results"]
                    if row["name"] == "Axel Rimnac")
        self.assertEqual(
            (axel.get("rank"), axel["timeText"],
             axel.get("outOfCompetition")),
            (None, "00:19:50", True),
        )

    def test_custom_result_grids_preserve_ranks_units_and_statuses(self):
        cases = [
            ("5017-0.pdf", pdf_parser.parse_wings_for_life_pdf, 4, 10),
            ("4330-0.pdf", pdf_parser.parse_funol_two_column_pdf, 3, 45),
            ("4271-0.pdf", pdf_parser.parse_tirol_school_individual_pdf, 6, 195),
            ("2245-0.pdf", pdf_parser.parse_family_adventure_pdf, 6, 34),
            ("3009-0.pdf", pdf_parser.parse_knockout_final_pdf, 1, 22),
        ]
        for file_name, parser, category_count, row_count in cases:
            with self.subTest(file_name=file_name):
                source = ROOT / "data" / "raw" / "anne" / "files" / file_name
                categories = parser(source)
                self.assertEqual((len(categories), sum(
                    len(c["results"]) for c in categories)),
                    (category_count, row_count))

        source = ROOT / "data" / "raw" / "anne" / "files" / "2445-0.pdf"
        categories = pdf_parser.parse_nolv_excel_result_pdf(source)
        self.assertEqual((len(categories), sum(
            c["sourceUnitCount"] for c in categories), sum(
            len(c["results"]) for c in categories)), (11, 147, 170))
        he = next(c for c in categories if c["name"] == "HE")
        self.assertEqual(
            [(r["name"], r["rank"]) for r in he["results"][:2]],
            [("Dan Darius", 1), ("Graf Valenti", 1)],
        )

        school_score = pdf_parser.parse_school_score_team_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "1600-0.pdf")
        self.assertEqual((len(school_score), sum(
            c["sourceUnitCount"] for c in school_score), sum(
            len(c["results"]) for c in school_score)), (8, 329, 541))

        vorarlberg = pdf_parser.parse_vorarlberg_school_team_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "1216-0.pdf")
        self.assertEqual((len(vorarlberg), sum(
            c["sourceUnitCount"] for c in vorarlberg), sum(
            len(c["results"]) for c in vorarlberg)), (4, 29, 116))
        loitz = next(r for c in vorarlberg for r in c["results"]
                     if r["name"] == "Loitz Alina")
        self.assertEqual((loitz["status"], loitz["individualStatus"], loitz["rank"]),
                         ("ok", "mp", 2))

        noe_2016_source = (
            ROOT / "data" / "raw" / "anne" / "files" / "1669-1.html")
        noe_2016 = html_parser.parse_noe_school_team_html(
            html_parser.decode(noe_2016_source.read_bytes()))
        self.assertEqual((len(noe_2016), sum(
            c["sourceUnitCount"] for c in noe_2016), sum(
            len(c["results"]) for c in noe_2016)), (3, 38, 114))

        tirol_2016_individual = (
            pdf_parser.parse_tirol_school_2016_individual_pdf(
                ROOT / "data" / "raw" / "anne" / "files" / "1682-0.pdf"))
        self.assertEqual(
            [(c["name"], c["declaredStarters"], c["sourceUnitCount"])
             for c in tirol_2016_individual],
            [
                ("5./6. männlich", 24, 24),
                ("5./6. weiblich", 16, 16),
                ("5.-8. männlich", 32, 32),
                ("5.-8. weiblich", 16, 16),
                ("9.-12. männlich", 12, 12),
                ("9.-12. weiblich", 1, 1),
            ],
        )

        tirol_2016 = pdf_parser.parse_tirol_school_team_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "1682-2.pdf")
        self.assertEqual((len(tirol_2016), sum(
            c["sourceUnitCount"] for c in tirol_2016), sum(
            len(c["results"]) for c in tirol_2016)), (6, 26, 97))

        noe_2022 = pdf_parser.parse_noe_school_team_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "3728-1.pdf")
        self.assertEqual((len(noe_2022), sum(
            c["sourceUnitCount"] for c in noe_2022), sum(
            len(c["results"]) for c in noe_2022)), (4, 26, 78))

        tirol_2020 = pdf_parser.parse_tirol_school_team_2020_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "3004-0.pdf")
        self.assertEqual((len(tirol_2020), sum(
            c["sourceUnitCount"] for c in tirol_2020), sum(
            len(c["results"]) for c in tirol_2020)), (4, 24, 96))

        tirol_2020_individual = pdf_parser.parse_tirol_school_2020_individual_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "3004-1.pdf")
        self.assertEqual(
            [(c["name"], c["declaredStarters"], c["sourceUnitCount"])
             for c in tirol_2020_individual],
            [
                ("5./6. weiblich", 13, 13),
                ("5./6. männlich", 20, 20),
                ("5./8. weiblich", 32, 32),
                ("5./8. männlich", 33, 33),
            ],
        )
        pfluger = next(
            r for c in tirol_2020_individual for r in c["results"]
            if r["name"] == "Pfluger Sophia"
        )
        self.assertEqual(
            (pfluger["club"], pfluger["timeText"], pfluger["timeS"]),
            ("NMS Langkampfen 1", "1:08:10", 4090),
        )

        tirol_2023 = pdf_parser.parse_tirol_school_team_2023_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "4271-1.pdf")
        self.assertEqual((len(tirol_2023), sum(
            c["sourceUnitCount"] for c in tirol_2023), sum(
            len(c["results"]) for c in tirol_2023)), (5, 49, 192))
        posthoorn = next(
            r for c in tirol_2023 for r in c["results"]
            if r["name"] == "Posthoorn Kilian"
        )
        self.assertEqual(
            (posthoorn["club"], posthoorn["timeText"],
             posthoorn["individualStatus"], posthoorn["status"]),
            ("MS Hopfgarten", "00:40:06", "ok", "dsq"),
        )

        surprise = pdf_parser.parse_surprise_three_person_team_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "952-1.pdf")
        self.assertEqual(
            (surprise[0]["sourceUnitCount"], len(surprise[0]["results"])),
            (7, 21),
        )

        arge_alp = pdf_parser.parse_arge_alp_relay_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "3551-0.pdf")
        self.assertEqual((len(arge_alp), sum(
            c["sourceUnitCount"] for c in arge_alp), sum(
            len(c["results"]) for c in arge_alp)), (15, 194, 548))
        missing_punch = next(r for c in arge_alp for r in c["results"]
                             if r["name"] == "Ruggiero Ines")
        self.assertEqual(
            (missing_punch["status"], missing_punch["individualStatus"],
             missing_punch["teamNumber"], missing_punch["leg"]),
            ("mp", "mp", "140", 1),
        )
        retired_team_member = next(
            r for c in arge_alp for r in c["results"]
            if r["name"] == "Badia Comas Núria")
        self.assertEqual(
            (retired_team_member["status"],
             retired_team_member["individualStatus"]),
            ("dnf", "ok"),
        )

    def test_multistage_overall_sources_split_into_physical_stages(self):
        uwg_2018 = pdf_parser.parse_uwg_multistage_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "2091-1.pdf")
        self.assertEqual(
            [(stage["stageNumber"], len(stage["categories"]),
              sum(len(c["results"]) for c in stage["categories"]))
             for stage in uwg_2018],
            [(3, 11, 141), (4, 22, 289)],
        )
        self.assertFalse(any(
            c["name"] == "M12"
            for c in uwg_2018[0]["categories"]
        ))
        ievstafiev = next(
            r for c in uwg_2018[0]["categories"] for r in c["results"]
            if r["name"] == "Ievstafiev Oleksandr"
        )
        self.assertEqual(
            (ievstafiev["rank"], ievstafiev["timeS"], ievstafiev["timeText"]),
            (10, 4709, "1:18:29"),
        )

        uwg_2019_source = (
            ROOT / "data" / "raw" / "anne" / "files" / "2430-3.html")
        uwg_2019 = html_parser.parse_oe_multistage_html(
            html_parser.decode(uwg_2019_source.read_bytes()),
            [
                {
                    "stageNumber": 3, "stageDate": "2019-06-22",
                    "stageTitle": "E2", "sourceColumn": "E2",
                    "timeIndex": 7, "rankIndex": 8,
                    "blankStageMeansDns": True,
                },
                {
                    "stageNumber": 4, "stageDate": "2019-06-23",
                    "stageTitle": "E3", "sourceColumn": "E3",
                    "timeIndex": 9, "rankIndex": 10,
                    "blankStageMeansDns": True,
                },
            ],
        )
        self.assertEqual(
            [(stage["stageNumber"], len(stage["categories"]),
              sum(len(c["results"]) for c in stage["categories"]))
             for stage in uwg_2019],
            [(3, 16, 176), (4, 24, 305)],
        )

        skio_source = (
            ROOT / "data" / "raw" / "anne" / "files" / "3681-1.html")
        skio = html_parser.parse_oe_multistage_html(
            html_parser.decode(skio_source.read_bytes()),
            [{
                "stageNumber": 2, "stageDate": "2022-02-27",
                "stageTitle": "8 AC Verfolgung", "sourceColumn": "E2",
                "timeIndex": 7, "overallTimeIndex": 8,
                "overallRankIndex": 0, "sourceLabel": "E2-Laufzeit",
                "unrankedStageIsOoc": True,
                "blankStageMeansDns": True,
            }],
        )
        habenicht = next(
            r for c in skio[0]["categories"] for r in c["results"]
            if r["name"] == "Tobias Habenicht"
        )
        self.assertEqual(
            (habenicht["rank"], habenicht["timeS"], habenicht["note"]),
            (1, 2417, "E2-Laufzeit: 23:40"),
        )
        hnilica = next(
            r for c in skio[0]["categories"] for r in c["results"]
            if r["name"] == "Sonja Hnilica"
        )
        self.assertEqual(
            (hnilica.get("rank"), hnilica["timeS"],
             hnilica.get("outOfCompetition")),
            (None, 1905, True),
        )
        h45 = next(
            c for c in skio[0]["categories"]
            if c["name"] == "Herren ab 45"
        )
        self.assertEqual(
            (h45["declaredStarters"], h45["sourceUnitCount"],
             len(h45["results"])),
            (8, 8, 8),
        )
        self.assertEqual(
            [r["name"] for r in h45["results"] if r["status"] == "dns"],
            ["Klaus Zweiker", "Hannes Brecka", "Gilbert Rass",
             "Peter Unterberger", "Bernhard Prokopetz"],
        )

        mtbo = pdf_parser.parse_mtbo_two_stage_overall_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "4835-1.pdf")
        self.assertEqual(
            [(stage["stageNumber"], len(stage["categories"]),
              sum(len(c["results"]) for c in stage["categories"]))
             for stage in mtbo],
            [(1, 28, 233), (2, 27, 220)],
        )
        berthaud = next(
            r for c in mtbo[1]["categories"] for r in c["results"]
            if r["name"] == "Armel Berthaud"
        )
        self.assertEqual((berthaud["rank"], berthaud["timeS"]), (2, 6742))

        online = pdf_parser.parse_orienteering_online_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "4071-1.pdf")
        self.assertEqual(
            (len(online), sum(len(c["results"]) for c in online)),
            (18, 100),
        )
        hnilica = next(r for c in online for r in c["results"]
                       if r["name"] == "Thomas Hnilica")
        self.assertEqual(
            (hnilica["rank"], hnilica["club"], hnilica["country"],
             hnilica["timeS"]),
            (1, "OLT Transdanubien", "AUT", 2021),
        )

        regional = pdf_parser.parse_plain_regional_championship_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "2533-3.pdf")
        self.assertEqual(
            (len(regional), sum(len(c["results"]) for c in regional)),
            (14, 65),
        )
        d14 = next(c for c in regional if c["name"] == "Damen -14")
        self.assertEqual(
            [(row["name"], row.get("rank")) for row in d14["results"]],
            [("Emily Adenstedt", 1), ("Tanja Klöckl", 2)],
        )

        mixed_relay = pdf_parser.parse_wien_mixed_sprint_relay_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "2793-1.html")
        self.assertEqual(
            (len(mixed_relay), sum(c["sourceUnitCount"] for c in mixed_relay),
             sum(len(c["results"]) for c in mixed_relay)),
            (5, 35, 105),
        )
        gassner = next(r for c in mixed_relay for r in c["results"]
                       if r["name"] == "Anika Gassner")
        self.assertEqual(
            (gassner["status"], gassner["individualStatus"],
             gassner["club"], gassner["leg"]),
            ("mp", "mp", "Naturfreunde Wien", 2),
        )

        sprint = club_table_parser.parse_document(
            (ROOT / "data" / "raw" / "anne" / "files" /
             "1974-0.html").read_text())
        self.assertEqual(
            (len(sprint), sum(len(c["results"]) for c in sprint)),
            (6, 79),
        )
        a_herren = next(c for c in sprint if c["name"] == "A Herren")
        self.assertEqual(
            (a_herren["declaredStarters"], a_herren["results"][-1]["name"],
             a_herren["results"][-1]["outOfCompetition"]),
            (31, "Satrapa Vito", True),
        )

        summer = club_table_parser.parse_document(
            (ROOT / "data" / "raw" / "anne" / "files" /
             "1430-0.html").read_text())
        self.assertEqual(
            (len(summer), sum(len(c["results"]) for c in summer)),
            (7, 146),
        )

        trailo = pdf_parser.parse_trailo_tempo_pdf(
            ROOT / "data" / "raw" / "anne" / "files" / "930-0.pdf")
        self.assertEqual(
            [(category["name"], len(category["results"]))
             for category in trailo],
            [("Elite", 31), ("A", 9), ("N", 3)],
        )
        winner = trailo[0]["results"][0]
        self.assertEqual(
            (winner["name"], winner["club"], winner["country"],
             winner["rank"], winner["timeS"]),
            ("Ivo Tišljar", "OK Orion", "CRO", 1, 720),
        )


if __name__ == "__main__":
    unittest.main()
