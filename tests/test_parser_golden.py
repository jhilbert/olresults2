import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "parser"
sys.path.insert(0, str(ROOT / "ingest"))

SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_html_golden", ROOT / "ingest" / "parse_sportsoftware_html.py")
html_parser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(html_parser)

PDF_SPEC = importlib.util.spec_from_file_location(
    "parse_sportsoftware_pdf_golden", ROOT / "ingest" / "parse_sportsoftware_pdf.py")
pdf_parser = importlib.util.module_from_spec(PDF_SPEC)
PDF_SPEC.loader.exec_module(pdf_parser)

BUILD_SPEC = importlib.util.spec_from_file_location(
    "build_db_golden", ROOT / "build" / "build_db.py")
build_db = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build_db)


def matches(row, selector):
    return all(row.get(key) == value for key, value in selector.items())


class ParserGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((FIXTURES / "golden.json").read_text())

    def test_committed_source_fixtures(self):
        for case in self.manifest["cases"]:
            with self.subTest(case=case["id"], rules=case["rules"]):
                source = (FIXTURES / case["source"]).read_text()
                if case["parser"] == "sportsoftware-html":
                    categories = html_parser.parse_document(source)
                elif case["parser"] == "sportsoftware-html-simple-global":
                    categories = html_parser.parse_simple_global_results(source)
                else:
                    self.fail(f"unknown golden parser: {case['parser']}")
                category = next(item for item in categories
                                if item["name"] == case["category"])
                self.assertEqual(category.get("declaredStarters"),
                                 case["declared_starters"])
                self.assertEqual(
                    category.get("sourceUnitCount") or
                    build_db.normalized_source_unit_count(category["results"]),
                    case["source_units"])
                if "result_rows" in case:
                    self.assertEqual(len(category["results"]), case["result_rows"])
                for assertion in case["rows"]:
                    row = next(row for row in category["results"]
                               if matches(row, assertion["match"]))
                    for key, value in assertion["expect"].items():
                        self.assertEqual(row.get(key), value, (case["id"], key, row))
                names = {row.get("name") for row in category["results"]}
                self.assertTrue(names.isdisjoint(case.get("forbidden_names", [])))

    def test_wien_sprint_word_row_preserves_name_club_rank_and_status(self):
        winner = pdf_parser.parse_wien_sprint_result_line(
            "Anika Gassner Naturfreunde Wien 16:57", inferred_rank=1)
        self.assertEqual(
            winner,
            {
                "name": "Anika Gassner",
                "club": "Naturfreunde Wien",
                "timeText": "16:57",
                "status": "ok",
                "timeS": 1017,
                "rank": 1,
            },
        )
        mp = pdf_parser.parse_wien_sprint_result_line(
            "Andreas Slateff OLC Wienerwald MP")
        self.assertEqual(mp["name"], "Andreas Slateff")
        self.assertEqual(mp["club"], "OLC Wienerwald")
        self.assertEqual(mp["status"], "mp")
        self.assertNotIn("rank", mp)

    def test_academic_table_row_repairs_pdf_surname_spacing(self):
        row = pdf_parser.academic_championship_table_result(
            ["1.", "Ka ltenbacher", "Pierre", "00:38:25",
             "HSV OL Wiener Neustadt"])
        self.assertEqual(row["name"], "Kaltenbacher Pierre")
        self.assertEqual(row["club"], "HSV OL Wiener Neustadt")
        self.assertEqual(row["rank"], 1)
        self.assertEqual(row["timeS"], 2305)

    def test_noe_school_excel_team_rows_are_grouped_once(self):
        source = """
        <table>
          <tr><td>Unterstufe männlich</td></tr>
          <tr><td>Rang</td><td>Schule</td><td>Name</td><td>Vorname</td>
              <td>Zeit</td><td>Mannschaft</td></tr>
          <tr><td>1</td><td>NMS Edlitz</td><td>LENGL</td><td>Alexander</td>
              <td>29:22:00</td><td>94:50:00</td></tr>
          <tr><td>NMS Edlitz</td><td>RINGHOFER</td><td>Michael</td>
              <td>32:32:00</td></tr>
          <tr><td>NMS Edlitz</td><td>WIESER</td><td>Lukas</td>
              <td>32:56:00</td></tr>
          <tr><td>Qualifikation für Bundesmeisterschaft - Wien</td></tr>
          <tr><td>Unterstufe männlich</td></tr>
          <tr><td>1</td><td>NMS Edlitz</td><td>LENGL</td><td>Alexander</td>
              <td>29:22:00</td><td>94:50:00</td></tr>
        </table>
        """
        categories = html_parser.parse_noe_school_team_html(source)
        self.assertEqual(len(categories), 1)
        category = categories[0]
        self.assertEqual((category["sourceUnitCount"], len(category["results"])), (1, 3))
        self.assertEqual(category["results"][0]["timeS"], 29 * 60 + 22)
        self.assertEqual(category["results"][0]["teamTimeS"], 94 * 60 + 50)

    def test_tirol_school_team_lines_keep_ooc_and_individual_status(self):
        categories = pdf_parser.parse_tirol_school_team_lines([
            "5./6. männlich",
            "1 NMS Kitzbühel 01:00:47",
            "13 Taferner Julian 00:20:04",
            "14 Obermoser Anton 00:19:59",
            "15 Pothoven Abel Fehlstempel",
            "16 Hanser Lukas 00:20:44",
            "x BRG Imst ohne Wertung",
            "5 Felix Moosmann 00:23:35",
            "6 Vincent Schneider Disqu",
            "7 Xaver Pupeter 00:22:47",
            "8 Vakant N. An.",
        ])
        category = categories[0]
        self.assertEqual((category["sourceUnitCount"], len(category["results"])), (2, 7))
        mp = next(row for row in category["results"]
                  if row["name"] == "Pothoven Abel")
        self.assertEqual((mp["status"], mp["individualStatus"]), ("ok", "mp"))
        ooc = next(row for row in category["results"]
                   if row["name"] == "Felix Moosmann")
        self.assertTrue(ooc["outOfCompetition"])
        self.assertNotIn("rank", ooc)

    def test_noe_school_pdf_name_repair_only_splits_glued_columns(self):
        self.assertEqual(
            pdf_parser._repair_glued_school_name("ErtlschweigerTheo"),
            "Ertlschweiger Theo",
        )
        self.assertEqual(
            pdf_parser._repair_glued_school_name("TAKACS Leon"),
            "TAKACS Leon",
        )

    def test_os2003_pre_relay_groups_team_members_and_ak(self):
        team = lambda rank, number, name, value: (
            f"{rank:>6}{number:>7}{name:<37}{value}\n")
        member = lambda name, value: f"{'':13}{name:<29}{'':5}{value}\n"
        source = (
            "<pre><b>Bundesländerstaffel  (2)</b>\n"
            + team("1", "1", "Wien 1", "1:00:00")
            + member("Alpha, Anna", "20:00")
            + member("Beta, Berta", "40:00")
            + team("AK", "2", "Niederösterreich 1", "Fehlst")
            + member("Gamma, Gabi", "25:00")
            + member("Delta, Dora", "Fehlst")
            + "</pre>"
        )
        categories = html_parser.parse_os2003_relay_pre_html(source)
        category = categories[0]
        self.assertEqual((category["sourceUnitCount"], len(category["results"])), (2, 4))
        winner = category["results"][0]
        self.assertEqual(
            (winner["rank"], winner["teamNumber"], winner["leg"], winner["status"]),
            (1, "1", 1, "ok"),
        )
        ak = next(row for row in category["results"]
                  if row["name"] == "Gamma, Gabi")
        self.assertTrue(ak["outOfCompetition"])
        self.assertEqual((ak["status"], ak["individualStatus"]), ("mp", "ok"))

    def test_vienna_relay_keeps_repeated_legs_and_memberless_dns_team(self):
        source = """
        <table>
          <tr><td></td><td>Mixed Open Relay</td><td></td><td>Time</td></tr>
          <tr><td></td><td>1.</td><td>Team A</td><td>24:00</td></tr>
          <tr><td></td><td>1.</td><td>Anna Alpha</td><td>6:00</td><td>6:00</td></tr>
          <tr><td></td><td>2.</td><td>Bob Beta</td><td>6:00</td><td>12:00</td></tr>
          <tr><td></td><td>3.</td><td>Anna Alpha</td><td>6:00</td><td>18:00</td></tr>
          <tr><td></td><td>4.</td><td>Bob Beta</td><td>6:00</td><td>24:00</td></tr>
          <tr><td></td><td>Team 2</td><td>DNS</td></tr>
        </table>
        """
        categories = html_parser.parse_vienna_sprint_relay_html(source)
        category = categories[0]
        self.assertEqual((category["sourceUnitCount"], len(category["results"])), (2, 5))
        anna = [row for row in category["results"] if row["name"] == "Anna Alpha"]
        self.assertEqual([row["leg"] for row in anna], [1, 3])
        dns = next(row for row in category["results"] if row.get("memberlessTeam"))
        self.assertEqual((dns["teamName"], dns["status"]), ("Team 2", "dns"))


if __name__ == "__main__":
    unittest.main()
