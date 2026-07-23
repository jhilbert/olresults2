import importlib.util
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "quality_report", ROOT / "build" / "quality_report.py")
quality_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality_report)


class QualityReportTests(unittest.TestCase):
    def database(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""
            CREATE TABLE event(id INTEGER PRIMARY KEY, title TEXT, date_from TEXT);
            CREATE TABLE stage(id INTEGER PRIMARY KEY, event_id INTEGER, title TEXT);
            CREATE TABLE source_document(id TEXT PRIMARY KEY, source_type TEXT,
                file_name TEXT, source_url TEXT);
            CREATE TABLE result_list(id TEXT PRIMARY KEY, stage_id INTEGER,
                source_document_id TEXT, category TEXT, declared_starters INTEGER,
                parsed_entries INTEGER, parsed_rows INTEGER, ranking_basis TEXT);
            CREATE TABLE result(id TEXT, result_list_id TEXT, championship TEXT);
            CREATE TABLE regional_category_mapping(result_list_id TEXT, state TEXT);
            CREATE TABLE audit_issue(result_list_id TEXT, severity TEXT, code TEXT);
        """)
        con.execute("INSERT INTO event VALUES (1,'ÖM Test','2026-01-01')")
        con.execute("INSERT INTO stage VALUES (1,1,'Finale')")
        con.execute("INSERT INTO source_document VALUES ('doc','sportsoftware-pdf','x.pdf','https://x')")
        con.execute("INSERT INTO result_list VALUES ('list',1,'doc','H21',3,2,2,'time')")
        con.execute("INSERT INTO result VALUES ('r','list','ÖM')")
        con.execute("INSERT INTO audit_issue VALUES ('list','blocker','entry_count_mismatch')")
        return con

    def test_groups_by_source_and_prioritizes_actionable_issue(self):
        con = self.database()
        try:
            report = quality_report.collect(
                con, quality_report.load_catalog(), campaign="national")
        finally:
            con.close()
        self.assertEqual(report["summary"]["affected_sources"], 1)
        self.assertEqual(report["summary"]["by_severity"], {"blocker": 1})
        issue = report["sources"][0]["lists"][0]["issues"][0]
        self.assertEqual((issue["rule"], issue["owner"]),
                         ("PARSE-001", "parser"))

    def test_domain_filter_can_exclude_identity_queue(self):
        con = self.database()
        con.execute("INSERT INTO audit_issue VALUES "
                    "('list','warning','provisional_championship_identity')")
        try:
            report = quality_report.collect(
                con, quality_report.load_catalog(), include_domains={"parsing"})
        finally:
            con.close()
        self.assertEqual(report["summary"]["by_code"], {})


if __name__ == "__main__":
    unittest.main()
