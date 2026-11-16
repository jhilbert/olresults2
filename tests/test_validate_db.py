import importlib.util
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("validate_db", ROOT / "build" / "validate_db.py")
validate_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate_db)


def health(result=100, conflicts=1, decisions=20):
    return {
        "counts": {"event": 10, "stage": 11, "person": 50, "result": result},
        "eligibility": {"people": 5, "decisions": decisions},
        "quality": {"identifier_conflicts": conflicts},
    }


class BuildHealthTests(unittest.TestCase):
    def test_course_sources_cannot_leak_into_results_or_another_event(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""CREATE TABLE stage(id INTEGER, event_id INTEGER);
            CREATE TABLE source_document(id TEXT,event_id INTEGER);
            CREATE TABLE course_result(stage_id INTEGER,source_document_id TEXT);
            CREATE TABLE result(source_document_id TEXT);
            INSERT INTO stage VALUES(1,4404); INSERT INTO source_document VALUES('course',4404);
            INSERT INTO course_result VALUES(1,'course');""")
        validate_db.validate_course_isolation(con)
        con.execute("INSERT INTO result VALUES('course')")
        with self.assertRaisesRegex(RuntimeError, "leaked into ordinary results"):
            validate_db.validate_course_isolation(con)
        con.execute("DELETE FROM result")
        con.execute("UPDATE stage SET event_id=999")
        with self.assertRaisesRegex(RuntimeError, "different event"):
            validate_db.validate_course_isolation(con)

    def test_logical_fingerprint_ignores_insertion_order(self):
        def database(rows):
            con = sqlite3.connect(":memory:")
            for table in validate_db.FINGERPRINT_ORDER:
                con.execute(f"CREATE TABLE {table} (id INTEGER, value TEXT)")
                con.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
            return con

        first = database([(1, "a"), (2, "b")])
        second = database([(2, "b"), (1, "a")])
        try:
            with patch.dict(
                    validate_db.FINGERPRINT_ORDER,
                    {table: "id" for table in validate_db.FINGERPRINT_ORDER}, clear=True):
                self.assertEqual(
                    validate_db.logical_fingerprint(first),
                    validate_db.logical_fingerprint(second),
                )
        finally:
            first.close()
            second.close()

    def test_small_growth_is_allowed(self):
        validate_db.validate_against_baseline(health(result=101), health(), 0.02)

    def test_large_result_drop_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "result dropped"):
            validate_db.validate_against_baseline(health(result=90), health(), 0.02)

    def test_eligibility_shrink_and_new_identifier_conflict_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "eligibility decisions shrank"):
            validate_db.validate_against_baseline(health(decisions=19), health(), 0.02)
        with self.assertRaisesRegex(RuntimeError, "identifier conflicts increased"):
            validate_db.validate_against_baseline(health(conflicts=2), health(), 0.02)


if __name__ == "__main__":
    unittest.main()
