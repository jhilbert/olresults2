import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("wolv_annual", ROOT / "build" / "extract_wolv_annual.py")
wolv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wolv)


class WolvAnnualTests(unittest.TestCase):
    def test_extracts_complete_wrapped_relay_winner_not_other_places(self):
        result = wolv.extract_cell("Senioren 35-:\n1) Club: ERSTE Anna -\n- ZWEITE Max\n2) Andere")
        self.assertEqual(result["first_place_text"], "Club: ERSTE Anna - - ZWEITE Max")

    def test_no_winner_in_explanatory_count(self):
        self.assertIsNone(wolv.extract_cell("keine Wertung\n(1)")["first_place_text"])

    def test_snapshot_preserves_official_no_award_reason(self):
        data = json.loads((ROOT / "data" / "review" / "wolv_annual_medal_tables_2024_2026.json").read_text())
        self.assertEqual([d["year"] for d in data["documents"]], [2024, 2025, 2026])
        doc = data["documents"][2]
        cells = [c for c in doc["cells"] if c["page"] == 1 and c["column"] == 4
                 and c["row_heading"] == "D 19-"]
        self.assertEqual(len(cells), 1)
        self.assertIn("nur 3 Läuferinnen aus einem Verein", cells[0]["cell"])
        self.assertIsNone(cells[0]["first_place_text"])
        self.assertEqual(doc["extraction_status"], "unmatched_review_only")
