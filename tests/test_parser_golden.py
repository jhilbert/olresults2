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
                self.assertEqual(case["parser"], "sportsoftware-html")
                categories = html_parser.parse_document(
                    (FIXTURES / case["source"]).read_text())
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


if __name__ == "__main__":
    unittest.main()
