import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "docs" / "rules" / "README.md"
AUDITS = ROOT / "docs" / "rules" / "audit-catalog.json"


class RuleCatalogTests(unittest.TestCase):
    def test_every_production_audit_code_is_documented(self):
        source = (ROOT / "build" / "build_db.py").read_text()
        codes = set(re.findall(
            r'add_audit_issue\(\s*\n?\s*cur,\s*[^,]+,\s*"([a-z_]+)"', source))
        catalog = json.loads(AUDITS.read_text())
        self.assertTrue(codes)
        self.assertEqual(codes - set(catalog), set())

    def test_every_audit_points_to_an_existing_rule(self):
        rule_text = RULES.read_text()
        catalog = json.loads(AUDITS.read_text())
        missing = {item["rule"] for item in catalog.values()
                   if f"`{item['rule']}`" not in rule_text}
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
