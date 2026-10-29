"""Run frontend business-rule regressions as part of the existing CI suite."""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class FrontendRegressionTests(unittest.TestCase):
    def test_node_frontend_rules_and_youth_titles(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for frontend regression tests")
        subprocess.run(
            [node, "--test", "tests/test_frontend_rules.js", "tests/test_youth_titles.js"],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )


if __name__ == "__main__":
    unittest.main()
