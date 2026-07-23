import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self.assertEqual(
            [item["event_id"] for item in report["unexplained_unpublished_sources"]],
            [3],
        )

    def test_committed_source_exception_is_not_a_parser_gap(self):
        def event(event_id):
            return {
                "id": event_id, "shortTitle": "Series",
                "dateFrom": "2020-01-01", "eventType": "competition",
                "visibility": "public", "status": "completed",
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            calendar = tmp / "events.json"
            attachments = tmp / "attachments.json"
            normalized = tmp / "normalized"
            raw_files = tmp / "files"
            database = tmp / "results.db"
            normalized.mkdir()
            raw_files.mkdir()
            calendar.write_text(json.dumps([event(7)]))
            attachments.write_text(json.dumps({
                "7": [{"fileName": "overall.pdf",
                       "url": "https://x/overall.pdf"}],
            }))
            (raw_files / "7-0.pdf").write_bytes(b"cached")
            con = sqlite3.connect(database)
            con.executescript(
                "CREATE TABLE event(id INTEGER PRIMARY KEY);"
                "CREATE TABLE source_document(normalized_path TEXT);"
                "CREATE TABLE stage(id INTEGER PRIMARY KEY, event_id INTEGER);"
                "CREATE TABLE result(id INTEGER PRIMARY KEY, stage_id INTEGER);"
            )
            con.close()
            with patch.object(
                    audit, "MANUAL_ATTACHMENT_SKIP", {(7, "overall.pdf")}):
                report = audit.collect(
                    calendar, attachments, normalized, raw_files, database)

        self.assertFalse(report["cached_sources_without_parsed_rows"])
        self.assertFalse(report["events_with_result_source_but_no_results"])
        self.assertEqual(
            [item["event_id"] for item in report["expected_skipped_sources"]],
            [7],
        )

    def test_index_exception_can_target_one_of_two_unnamed_attachments(self):
        event = {
            "id": 8, "shortTitle": "Race", "dateFrom": "2020-01-01",
            "eventType": "competition", "visibility": "public",
            "status": "completed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            calendar, attachments = tmp / "events.json", tmp / "attachments.json"
            normalized, raw_files = tmp / "normalized", tmp / "files"
            database = tmp / "results.db"
            normalized.mkdir()
            raw_files.mkdir()
            calendar.write_text(json.dumps([event]))
            attachments.write_text(json.dumps({
                "8": [
                    {"fileName": "", "url": "https://x/full"},
                    {"fileName": "", "url": "https://x/club"},
                ],
            }))
            (raw_files / "8-0.html").write_text("full")
            (raw_files / "8-1.html").write_text("club")
            con = sqlite3.connect(database)
            con.executescript(
                "CREATE TABLE event(id INTEGER PRIMARY KEY);"
                "CREATE TABLE source_document(normalized_path TEXT);"
                "CREATE TABLE stage(id INTEGER PRIMARY KEY, event_id INTEGER);"
                "CREATE TABLE result(id INTEGER PRIMARY KEY, stage_id INTEGER);"
            )
            con.close()
            with patch.object(audit, "MANUAL_ATTACHMENT_INDEX_SKIP", {(8, 1)}):
                report = audit.collect(
                    calendar, attachments, normalized, raw_files, database)

        self.assertEqual(
            [(item["event_id"], item["attachment_index"])
             for item in report["expected_skipped_sources"]],
            [(8, 1)],
        )
        self.assertIn(
            (8, 0),
            {(item["event_id"], item["attachment_index"])
             for item in report["ambiguous_cached_sources_without_parsed_rows"]},
        )

    def test_intentionally_excluded_event_is_not_a_database_gap(self):
        event = {
            "id": 9, "shortTitle": "No usable result",
            "dateFrom": "2020-01-01", "eventType": "competition",
            "visibility": "public", "status": "completed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            calendar, attachments = tmp / "events.json", tmp / "attachments.json"
            normalized, raw_files = tmp / "normalized", tmp / "files"
            database = tmp / "results.db"
            normalized.mkdir()
            raw_files.mkdir()
            calendar.write_text(json.dumps([event]))
            attachments.write_text("{}")
            (normalized / "9-0.json").write_text(json.dumps({
                "categories": [{"results": [{"name": "Approximate row"}]}],
            }))
            con = sqlite3.connect(database)
            con.executescript(
                "CREATE TABLE event(id INTEGER PRIMARY KEY);"
                "CREATE TABLE source_document(normalized_path TEXT);"
                "CREATE TABLE stage(id INTEGER PRIMARY KEY, event_id INTEGER);"
                "CREATE TABLE result(id INTEGER PRIMARY KEY, stage_id INTEGER);"
            )
            con.close()
            report = audit.collect(
                calendar, attachments, normalized, raw_files, database,
                excluded_events={
                    9: {"reason": "No source", "decision": "exclude-test"},
                })

        self.assertFalse(report["snapshot_events_not_in_database"])
        self.assertFalse(report["unexplained_unpublished_sources"])
        self.assertEqual(
            [item["event_id"]
             for item in report["excluded_events_without_usable_results"]],
            [9],
        )
        self.assertEqual(
            [item["event_id"]
             for item in report["parsed_sources_excluded_with_event"]],
            [9],
        )


if __name__ == "__main__":
    unittest.main()
