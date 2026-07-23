import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "audit_event_coverage", ROOT / "build" / "audit_event_coverage.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class EventCoverageTests(unittest.TestCase):
    def test_reports_gaps_at_each_pipeline_boundary(self):
        def event(event_id, title):
            return {
                "id": event_id, "shortTitle": title,
                "dateFrom": "2020-01-01", "eventType": "competition",
                "visibility": "public", "status": "completed",
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            calendar = tmp / "events.json"
            external = tmp / "external.json"
            attachments = tmp / "attachments.json"
            normalized = tmp / "normalized"
            raw_files = tmp / "files"
            database = tmp / "results.db"
            normalized.mkdir()
            raw_files.mkdir()
            calendar.write_text(json.dumps([event(1, "parser gap"), event(3, "build gap")]))
            external.write_text(json.dumps([
                event(1, "parser gap"), event(2, "calendar gap"), event(3, "build gap")]))
            attachments.write_text(json.dumps({
                "1": [{"fileName": "Ergebnis.pdf", "url": "https://x/Ergebnis.pdf"}],
            }))
            (raw_files / "1-0.pdf").write_bytes(b"cached")
            (normalized / "3-0.json").write_text(json.dumps({
                "categories": [{"results": [{"name": "Runner"}]}],
            }))
            con = sqlite3.connect(database)
            con.executescript(
                "CREATE TABLE event(id INTEGER PRIMARY KEY);"
                "CREATE TABLE source_document(normalized_path TEXT);"
                "CREATE TABLE stage(id INTEGER PRIMARY KEY, event_id INTEGER);"
                "CREATE TABLE result(id INTEGER PRIMARY KEY, stage_id INTEGER);"
                "INSERT INTO event VALUES(3);"
            )
            con.commit()
            con.close()

            report = audit.collect(
                calendar, attachments, normalized, raw_files, database, external)

        self.assertEqual(
            [item["event_id"] for item in report["external_events_not_in_snapshot"]],
            [2],
        )
        self.assertEqual(
            [item["event_id"] for item in report["snapshot_events_not_in_database"]],
            [1],
        )
        self.assertEqual(
            [item["event_id"] for item in report["cached_sources_without_parsed_rows"]],
            [1],
        )
        self.assertEqual(
            [item["event_id"] for item in report["events_with_result_source_but_no_results"]],
            [1],
        )
        self.assertEqual(
            [item["event_id"] for item in report["parsed_sources_not_published"]],
            [3],
        )


if __name__ == "__main__":
    unittest.main()
