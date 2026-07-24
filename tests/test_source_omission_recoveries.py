import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "build_source_omission_recoveries",
    ROOT / "ingest" / "build_source_omission_recoveries.py",
)
recoveries = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recoveries)


class SourceOmissionRecoveryTests(unittest.TestCase):
    def test_reviewed_entries_build_dns_only_supplements(self):
        payload = json.loads(recoveries.CONFIG.read_text())
        documents = [
            recoveries.build_document(event)
            for event in payload["events"]
        ]
        rows = [
            row
            for document in documents
            for category in document["categories"]
            for row in category["results"]
        ]
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["status"] == "dns" for row in rows))
        self.assertEqual(
            {document["eventId"] for document in documents},
            {633, 856, 4995},
        )
        self.assertIn(
            ("Luise Schöller", 6988),
            {(row["name"], row["userId"]) for row in rows},
        )
        self.assertTrue(all(
            document["source"] == "anne-entry-recovery"
            for document in documents
        ))


if __name__ == "__main__":
    unittest.main()
