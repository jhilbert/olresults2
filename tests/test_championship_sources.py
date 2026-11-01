import importlib.util
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


sources = module("championship_sources", "ingest/championship_sources.py")
table = module("meistertafel", "ingest/parse_oefol_meistertafel.py")
builder = module("championship_audit", "build/championship_audit.py")
db = module("build_db_for_audit", "build/build_db.py")


class SourceInventoryTests(unittest.TestCase):
    def test_invitation_is_discovered_not_interpreted_and_failure_preserves_prior(self):
        payload = [{"id": 1, "type": "invitation", "url": "https://example.org/a.pdf", "label": "Einladung"},
                   {"id": 2, "type": "starttimes", "url": "https://example.org/start.pdf"},
                   {"id": 3, "type": "results", "url": "javascript:bad"}]
        self.assertEqual([a["id"] for a in sources.relevant_attachments(payload)], [1])
        item = {"event_id": 10, "date": "2026-01-01", "title": "ÖM", "jurisdictions": ["AUT"]}
        original = sources.refresh([item], {}, fetcher=lambda _: sources.relevant_attachments(payload), delay=0)
        def failed(_):
            raise OSError("unavailable")
        later = sources.refresh([item], original, fetcher=failed, delay=0)
        self.assertEqual(later["events"]["10"]["fetch_status"], "error")
        self.assertEqual(later["events"]["10"]["attachments"], original["events"]["10"]["attachments"])

    def test_meistertafel_preserves_year_and_rows_without_guessing(self):
        raw = b'''<details><summary>2024</summary><table id="tablepress-9"><tr><td>OL 2024</td></tr><tr><td>Meisterin</td><td>bis 14</td><td>Anna &amp; Partner</td></tr></table></details><details><summary>2025</summary><table id="tablepress-70"><tr><td>OL 2025</td></tr></table></details>'''
        parsed = table.parse(raw)
        self.assertEqual([t["year"] for t in parsed["tables"]], [2024, 2025])
        self.assertEqual(parsed["tables"][0]["rows"][1]["cells"][2]["text"], "Anna & Partner")
        self.assertNotIn("person_id", parsed["tables"][0]["rows"][1])


class ChampionshipAuditTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(db.SCHEMA)
        cur = self.con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from,sport_type) VALUES (1,'ÖM Sprint','2025-05-01','footOrienteering')")
        cur.execute("INSERT INTO stage VALUES (1,1,1,'Sprint','2025-05-01T00:00:00Z',NULL)")
        cur.execute("INSERT INTO championship_instance VALUES ('c','AUT',1,'D-14','d14','ÖM','test','confirmed','result-championship','cihash')")
        cur.execute("INSERT INTO source_document (id,event_id,source_type,source_url) VALUES ('source',1,'anne-api','https://example.org/result')")
        cur.execute("INSERT INTO result_list (id,stage_id,source_document_id,category,parsed_rows,input_fingerprint) VALUES ('list',1,'source','D-14',1,'lhash')")
        db.insert_result(cur, stage_id=1, result_list_id="list", category="D-14", person_id=123,
                         source="test", status="ok", rank=1, championship="ÖM",
                         identity_state="candidate", championship_eligibility_state="unknown")

    def collect(self, inventory=None, national=None):
        return builder.collect(self.con, inventory or {}, national or {}, {}, {},
                               {"source_url":"https://example.org/official", "tables":[{"year":2025}]}, {})

    def test_discovery_does_not_claim_verified_category_or_champion(self):
        inv = {"events":{"1":{"fetch_status":"ok", "attachments":[{"type":"invitation", "url":"https://example.org/inv"}]}}}
        row = self.collect(inv)["classes"][0]
        self.assertEqual(row["invitation"]["state"], "found_unreviewed")
        self.assertEqual(row["crosscheck"]["state"], "not_compared")
        self.assertEqual(row["result"]["identity_open"], 1)
        self.assertEqual(row["result"]["completeness"], "not_confirmed")
        self.assertEqual(len(row["audit_fingerprint"]), 64)

    def test_exact_category_and_fingerprint_required_for_claims(self):
        national = {"events":{"1":{"event_date":"2025-05-01", "stage_number":1,
                      "categories":["D-14"], "url":"https://example.org/inv", "sha256":"hash"}}}
        self.con.execute("INSERT INTO verification_assertion VALUES ('result_list','list','completeness','confirmed','old',NULL,NULL,NULL)")
        row = self.collect(national=national)["classes"][0]
        self.assertEqual(row["invitation"]["state"], "category_reviewed")
        self.assertEqual(row["result"]["completeness"], "not_confirmed")
        self.con.execute("UPDATE verification_assertion SET input_fingerprint='lhash'")
        row = self.collect(national=national)["classes"][0]
        self.assertEqual(row["result"]["completeness"], "human_confirmed")
        national["events"]["1"]["stage_number"] = 2
        self.assertNotEqual(self.collect(national=national)["classes"][0]["invitation"]["state"], "category_reviewed")

    def test_an_announced_class_without_participants_is_not_an_issue(self):
        national = {"events": {"1": {"event_date": "2025-05-01", "stage_number": 1,
                    "categories": ["D-14", "H-14"], "url": "https://example.org/inv"}}}
        report = self.collect(national=national)
        self.assertNotIn("national_classes_without_instance", report)

    def test_regional_mapping_requires_real_class_and_source_category(self):
        item = {"categories":{"W D-14":"D-14", "H45":{"H45-":["A"],"H50-":["B"]}}}
        self.assertTrue(builder.regional_category_evidence(item, "D-14", {"W D-14"}))
        self.assertFalse(builder.regional_category_evidence(item, "D-14", {"W H-14"}))
        self.assertTrue(builder.regional_category_evidence(item, "H50-", {"H45"}))

    def test_explicit_non_championship_class_only_clears_exact_event_date_stage(self):
        evidence = {"events": {"1": {"event_date": "2025-05-01", "stage_number": 1,
                    "excluded_from_national_championship_categories": ["D-14"]}}}
        self.assertEqual(db.apply_confirmed_national_category_exclusions(self.con.cursor(), evidence), 1)
        self.assertIsNone(self.con.execute("SELECT championship FROM result").fetchone()[0])
        self.con.execute("UPDATE result SET championship='ÖM'")
        evidence["events"]["1"]["stage_number"] = 2
        self.assertEqual(db.apply_confirmed_national_category_exclusions(self.con.cursor(), evidence), 0)
        self.assertEqual(self.con.execute("SELECT championship FROM result").fetchone()[0], "ÖM")

    def test_2026_night_elite_om_is_provisional_not_ostm(self):
        cur = self.con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from,sport_type) VALUES (5397,'ÖM Nacht','2026-04-10','footOrienteering')")
        cur.execute("INSERT INTO stage VALUES (10005397,5397,1,NULL,'2026-04-10',NULL)")
        db.insert_result(cur, stage_id=10005397, category="D21-E", person_id=888,
                         source="test", status="ok", rank=1)
        evidence = {"events": {"5397": {"event_date": "2026-04-10", "stage_number": 1,
            "apply_category_inference": True, "default_championship": "ÖM", "categories": ["D21-E"]}}}
        db.apply_confirmed_national_category_additions(cur, evidence)
        actual = cur.execute("SELECT championship,championship_eligibility_state FROM result WHERE stage_id=10005397").fetchone()
        self.assertEqual(actual, ("ÖM", "provisional"))

    def test_invitation_adds_only_missing_class_and_not_eligibility(self):
        cur = self.con.cursor()
        db.insert_result(cur, stage_id=1, category="D21-E", person_id=10, status="ok", rank=1, source="test")
        db.insert_result(cur, stage_id=1, category="D21-E", person_id=11, status="ok", rank=2, source="test")
        db.insert_result(cur, stage_id=1, category="H21-E", person_id=12, status="ok", rank=1, source="test", championship="ÖSTM")
        db.insert_result(cur, stage_id=1, category="H21-E", person_id=13, status="ok", rank=2, source="test")
        evidence = {"events": {"1": {"event_date": "2025-05-01", "stage_number": 1,
            "apply_category_inference": True, "default_championship": "ÖM",
            "category_championship_types": {"D21-E": "ÖSTM"}, "categories": ["D21-E", "H21-E"]}}}
        self.assertEqual(db.apply_confirmed_national_category_additions(cur, evidence), 2)
        rows = cur.execute("SELECT person_id,championship,championship_eligibility_state FROM result WHERE person_id IN (10,11,13) ORDER BY person_id").fetchall()
        self.assertEqual(rows, [(10, "ÖSTM", "provisional"), (11, "ÖSTM", "provisional"), (13, None, "unknown")])

    def test_suggestions_require_unambiguous_age_and_event_discipline(self):
        self.assertEqual(builder.age_signature("Damen bis 14"), ("D", "upper", 14))
        self.assertEqual(builder.age_signature("H55-"), ("H", "minimum", 55))
        self.assertEqual(builder.age_signature("ab 55", "Österreichischer Meister"), ("H", "minimum", 55))
        self.assertIsNone(builder.age_signature("Damen ab 15 bis 18"))
        self.assertIsNone(builder.age_signature("D15-18"))
        self.assertIsNone(builder.race_kind("ÖM Lang und Mittel"))
        self.assertEqual(builder.event_race_kind("ÖM Lang und Mittel", "Mittel"), "middle")
        self.assertIsNone(builder.event_race_kind("ÖM Sprint", "Lang und Mittel"))
        self.assertIsNone(builder.event_race_kind("ÖM Staffel", "Sprint"))

    def test_annual_table_matching_only_suggests_a_youth_winner(self):
        def row(*cells):
            return {"cells": [{"text": c} for c in cells], "source_row": "row-1"}
        data = {"tables": [{"year": 2025, "table_id": "t", "rows": [
            row("OL 2025"), row("Sprint"), row("Österreichische Meisterin", "bis 14", "Anna Muster", "OLC Graz")]}]}
        index = builder.oefol_winner_candidates(data)
        key = ("2025", "footOrienteering", "sprint", ("D", "upper", 14),
               builder.name_key("Muster Anna"), builder.name_key("OLC Graz"))
        self.assertEqual(len(index[key]), 1)
        self.assertNotIn(("2025", "footOrienteering", "sprint", ("D", "upper", 14),
                          builder.name_key("Anna Muster"), builder.name_key("anderer Verein")), index)

    def test_vienna_club_warning_counts_starters_and_never_awards(self):
        def runner(entry, status, club, eligible="eligible", ooc=0):
            return {"regional_entry_id": entry, "regional_eligibility_state": eligible,
                    "status": status, "official_club": club, "out_of_competition": ooc}
        rows = [runner("1", "ok", "Verein A"), runner("2", "dnf", "Verein A"),
                runner("3", "mp", "Verein A"), runner("4", "dns", "Verein B")]
        finding = builder.regional_rule_findings("WIEN", "2025-01-01", rows)[0]
        self.assertEqual(finding["known_started_entries"], 3)
        self.assertEqual(finding["known_clubs"], ["Verein A"])
        self.assertIn("keine automatische Aberkennung", finding["message"])
        rows.append(runner("5", "dsq", "Verein B"))
        self.assertEqual(builder.regional_rule_findings("WIEN", "2025-01-01", rows), [])
        self.assertEqual(builder.regional_rule_findings("WIEN", "2022-10-09", rows[:3]), [])

    def test_vienna_youth_exceptions_require_context_and_no_invented_split(self):
        self.assertTrue(db.vienna_two_club_exception("D-14", "Wr. Sprint", ""))
        self.assertIsNone(db.vienna_two_club_exception("D-14", "Wiener MS", ""))
        self.assertFalse(db.vienna_two_club_exception("D35-", "Wr. Sprint", ""))
        self.assertFalse(db.vienna_two_club_exception("DH-12", "Wr. Sprint", "", True))
        self.assertFalse(db.vienna_two_club_exception("D-12", "Wr. Sprint", "", False))
        self.assertTrue(db.vienna_two_club_exception("D-12", "Wr. Sprint", "", True))

    def test_vienna_two_club_hold_counts_started_units_not_dns(self):
        cur = self.con.cursor()
        cur.execute("INSERT INTO championship_instance VALUES ('w','WIEN',1,'D35-','d35','LMS','test','confirmed','invitation','hash')")
        for i, (status, club) in enumerate([("ok", "Club A"), ("dnf", "Club A"),
                                             ("mp", "Club A"), ("dns", "Club B")], 1):
            db.insert_result(cur, stage_id=1, result_list_id="list", category="D35-", person_id=200+i,
                             source="test", status=status, rank=i, official_club=club)
            rid = cur.lastrowid
            cur.execute("INSERT INTO championship_entry VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f"entry{i}", "w", 1, f"person:{i}", i, "eligible", "test", "derived", "list", "hash"))
            cur.execute("INSERT INTO championship_entry_result VALUES (?,?)", (f"entry{i}", rid))
        db.apply_vienna_two_club_checks(cur)
        hold = cur.execute("SELECT reason FROM regional_award_hold WHERE championship_instance_id='w'").fetchone()
        self.assertIn("Zwei-Vereine", hold[0])
        cur.execute("DELETE FROM regional_award_hold WHERE championship_instance_id='w'")
        cur.execute("UPDATE result SET status='dsq' WHERE person_id=204")
        db.apply_vienna_two_club_checks(cur)
        self.assertIsNone(cur.execute("SELECT reason FROM regional_award_hold WHERE championship_instance_id='w'").fetchone())


if __name__ == "__main__":
    unittest.main()
