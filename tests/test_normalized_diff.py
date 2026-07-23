import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "normalized_diff", ROOT / "build" / "normalized_diff.py")
normalized_diff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalized_diff)


class NormalizedDiffTests(unittest.TestCase):
    def test_metrics_track_parser_fidelity_dimensions(self):
        document = {"categories": [
            {"name": "D21", "results": [
                {"name": "Anna", "rank": 1, "timeS": 100, "status": "ok"},
                {"name": "Berta", "status": "unknown", "outOfCompetition": True},
            ]},
            {"name": "H21", "results": []},
        ]}
        self.assertEqual(normalized_diff.metrics(document), {
            "categories": 2, "rows": 2, "ranked": 1, "timed": 1,
            "unknown": 1, "ooc": 1,
        })

    def test_render_makes_count_regressions_visible(self):
        report = {"changed_documents": 1, "changes": [{
            "path": "data/normalized/1-0.json",
            "before": {"rows": 10}, "after": {"rows": 8},
            "delta": {"rows": -2},
        }]}
        self.assertIn("rows -2", normalized_diff.render(report))

    def test_changed_paths_include_new_untracked_normalized_files(self):
        responses = [
            subprocess.CompletedProcess([], 0, "data/normalized/1-0.json\n", ""),
            subprocess.CompletedProcess([], 0, "data/normalized/2-0.json\n", ""),
        ]
        with patch.object(normalized_diff, "git", side_effect=responses):
            self.assertEqual(normalized_diff.changed_paths(), [
                Path("data/normalized/1-0.json"),
                Path("data/normalized/2-0.json"),
            ])


if __name__ == "__main__":
    unittest.main()
