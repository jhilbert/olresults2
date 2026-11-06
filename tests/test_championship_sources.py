import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_official_2020_ski_classification_beats_conflicting_annual_table(self):
        evidence = json.loads((ROOT / "data/review/official_result_decisions.json").read_text())
        event = evidence["events"]["2916"]
        cur = self.con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (2916,'Ski-O ÖM Lang','2020-01-25')")
        cur.execute("INSERT INTO stage VALUES (2,2916,1,NULL,'2020-01-25',NULL)")
        cur.execute("INSERT INTO source_document (id,event_id,source_type,source_url) VALUES ('ski',2916,'pdf',?)", (event["result_source_url"],))
        for idx, item in enumerate(event["rows"], start=20):
            cur.execute("INSERT INTO person VALUES (?,?,?,NULL,NULL)", (idx,item["observed_name"],str(idx)))
            db.insert_result(cur, stage_id=2, person_id=idx, category=event["category"], status="ok",
                rank=item["rank"], time_s=item["time_s"], source="test", observed_name=item["observed_name"],
                observed_club=item["club"], official_club=item["club"], source_document_id="ski", championship="ÖM")
        self.assertEqual(db.apply_official_result_decisions(cur, evidence), 4)
        cur.execute("CREATE TEMP TABLE ineligible_starter (event_id INTEGER, person_id INTEGER)")
        db.apply_national_minimum_starters(cur)
        self.assertEqual(cur.execute("SELECT eligible_starters,state FROM national_minimum_starters WHERE stage_id=2").fetchone(), (3,"met"))
        db.compute_national_ranks(cur)
        self.assertEqual(cur.execute("SELECT observed_name,championship,national_rank FROM result WHERE stage_id=2 ORDER BY rank").fetchall(),
            [("Cech Adam",None,None), ("Perktold David","ÖM",1), ("Lorenz Konrad","ÖM",2), ("Egger Lilli","ÖM",3)])
        cur.execute("UPDATE result SET time_s=1421 WHERE observed_name='Cech Adam'")
        with self.assertRaisesRegex(ValueError,"no longer matches"):
            db.apply_official_result_decisions(cur, evidence)

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

    def test_2026_mtbo_scope_only_allows_middle_elite_and_preserves_results(self):
        cur = self.con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (5335,'7./8./9. AC ÖSTM Mittel','2026-09-26')")
        for stage_id, number, day in [(101,1,'2026-09-26'), (102,2,'2026-09-27'), (103,3,'2026-09-27')]:
            cur.execute("INSERT INTO stage VALUES (?,?,?,NULL,?,NULL)", (stage_id,5335,number,day))
        cases = [(101,'D21-E','ÖSTM'),(101,'D-14','ÖM'),
                 (102,'D21-E','ÖSTM'),(102,'H21-E','ÖSTM'),(102,'D50-','ÖM'),
                 (103,'Damen ab 21 Elite','ÖSTM'),(103,'Herren bis 14','ÖM')]
        for index, (stage_id, category, championship) in enumerate(cases):
            db.insert_result(cur, stage_id=stage_id, category=category, person_id=1000+index,
                status='ok', rank=1, time_s=1000+index, source='test', observed_name=f'Person {index}',
                championship=championship, national_rank=1)
        evidence = json.loads((ROOT / 'site/data/national-category-evidence.json').read_text())
        scope = {'events': {'5335': evidence['events']['5335']}}
        self.assertEqual(db.apply_confirmed_national_event_scopes(cur, scope), 5)
        rows = cur.execute("""SELECT stage_id,category,championship,national_rank,time_s,observed_name
            FROM result WHERE stage_id IN (101,102,103) ORDER BY stage_id,category""").fetchall()
        self.assertEqual([(r[0],r[1],r[2]) for r in rows if r[2]],
                         [(102,'D21-E','ÖSTM'),(102,'H21-E','ÖSTM')])
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(r[3] is None for r in rows if r[2] is None))
        self.assertEqual(cur.execute("SELECT championship FROM result WHERE stage_id=1").fetchone()[0], 'ÖM')
        self.assertEqual(db.apply_confirmed_national_event_scopes(cur, scope), 0)

    def test_complete_scope_fails_closed_on_changed_stage_date_but_not_missing_classes(self):
        evidence = {'events': {'1': {'event_date':'2025-05-02', 'stage_number':1,
            'complete_event_scope':True, 'default_championship':'ÖM', 'categories':['D-14','H-14']}}}
        with self.assertRaisesRegex(ValueError, 'stage no longer matches'):
            db.apply_confirmed_national_event_scopes(self.con.cursor(), evidence)
        self.assertEqual(self.con.execute('SELECT championship FROM result').fetchone()[0], 'ÖM')
        evidence['events']['1']['event_date'] = '2025-05-01'
        self.assertEqual(db.apply_confirmed_national_event_scopes(self.con.cursor(), evidence), 0)

    def test_complete_scope_does_not_silently_accept_conflicting_extra_ranking(self):
        self.con.execute("""INSERT INTO championship_source_entry
            (id,stage_id,source_document_id,category,category_key,observed_name,observed_name_key,
             championship_type,evidence_kind,source_scope)
            VALUES ('extra',1,'source','H50-','h50','Test','test','ÖM',
                    'official_additional_classification','full_field')""")
        evidence = {'events': {'1': {'event_date':'2025-05-01','stage_number':1,
            'complete_event_scope':True,'default_championship':'ÖM','categories':['D-14']}}}
        with self.assertRaisesRegex(ValueError, 'Additional championship source conflicts'):
            db.apply_confirmed_national_event_scopes(self.con.cursor(), evidence)
        self.assertEqual(self.con.execute('SELECT championship FROM result').fetchone()[0], 'ÖM')

    def test_mtbo_2026_scopes_match_the_announcements(self):
        events = json.loads((ROOT / 'site/data/national-category-evidence.json').read_text())['events']
        self.assertEqual((events['5280']['stage_number'], events['5280']['event_date']), (1,'2026-07-18'))
        self.assertEqual((events['5335']['stage_number'], set(events['5335']['categories'])),
                         (2, {'D21-E','H21-E'}))
        self.assertEqual(set(events['5411']['categories']), {'D-14','H-14','D-17','H-17','D-20','H-20'})
        self.assertEqual(events['5412']['category_championship_types'], {'H21-E':'ÖSTM'})
        self.assertIn('D21-E', events['5207']['categories'])
        self.assertEqual(events['5207']['default_championship'], 'ÖM')

    def test_2026_mtbo_middle_scope_excludes_the_following_long_race(self):
        cur = self.con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (5280,'MTBO ÖM Mittel','2026-07-18')")
        cur.execute("INSERT INTO stage VALUES (110,5280,1,'Mittel','2026-07-18',NULL)")
        cur.execute("INSERT INTO stage VALUES (111,5280,2,'Lang','2026-07-19',NULL)")
        for index, (stage, category) in enumerate([(110,'M17'),(110,'M50'),(111,'M17'),(111,'M50')]):
            db.insert_result(cur,stage_id=stage,person_id=1100+index,category=category,
                status='ok',rank=1,source='test',championship='ÖM')
        events = json.loads((ROOT / 'site/data/national-category-evidence.json').read_text())['events']
        self.assertEqual(db.apply_confirmed_national_event_scopes(cur, {'events':{'5280':events['5280']}}),2)
        rows = cur.execute("SELECT stage_id,championship FROM result WHERE stage_id IN(110,111) ORDER BY stage_id,category").fetchall()
        self.assertEqual(rows, [(110,'ÖM'),(110,'ÖM'),(111,None),(111,None)])

    def test_2026_mtbo_mass_start_adds_om_elite_without_assuming_eligibility(self):
        cur = self.con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (5207,'ÖM Massenstart','2026-04-18')")
        cur.execute("INSERT INTO stage VALUES (120,5207,1,NULL,'2026-04-18',NULL)")
        db.insert_result(cur,stage_id=120,person_id=1201,category='D21-E',status='ok',rank=1,source='test')
        db.insert_result(cur,stage_id=120,person_id=1202,category='H21-E',status='ok',rank=1,source='test')
        db.insert_result(cur,stage_id=120,person_id=1203,category='D-K',status='ok',rank=1,source='test')
        events = json.loads((ROOT / 'site/data/national-category-evidence.json').read_text())['events']
        scope = {'events':{'5207':events['5207']}}
        db.apply_confirmed_national_event_scopes(cur,scope)
        self.assertEqual(db.apply_confirmed_national_category_additions(cur,scope),2)
        rows = cur.execute("SELECT category,championship,championship_eligibility_state FROM result WHERE stage_id=120 ORDER BY person_id").fetchall()
        self.assertEqual(rows,[('D21-E','ÖM','provisional'),('H21-E','ÖM','provisional'),('D-K',None,'unknown')])

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

    def test_ski_pursuit_only_final_is_a_championship_and_jury_is_source_pinned(self):
        cur = self.con.cursor()
        evidence = json.loads((ROOT / 'site/data/national-category-evidence.json').read_text())
        event = evidence['events']['5396']
        scope = {'events': {'5396': event}}
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (5396,'ÖM Verfolgung','2026-03-01')")
        cur.execute("INSERT INTO stage VALUES (91,5396,1,'Sprint','2026-03-01',NULL)")
        cur.execute("INSERT INTO stage VALUES (92,5396,2,'Verfolgung','2026-03-01',NULL)")
        cur.execute("INSERT INTO source_document (id,event_id,source_type,source_url,normalized_sha256) VALUES ('final',5396,'pdf',?,?)",
                    (event['result_url'], event['normalized_sha256']))
        db.insert_result(cur, stage_id=91, category='H-17', person_id=901, rank=1, status='ok', source='test', championship='ÖM')
        db.insert_result(cur, stage_id=92, category='Herren Elite', person_id=902, rank=1, status='ok', source='test', championship='ÖM')
        for number, decision in enumerate(event['jury_decisions'], 1):
            db.insert_result(cur, stage_id=92, category=decision['category'], person_id=910+number,
                observed_name=decision['winner'], time_s=decision['time_s'], rank=1, status='ok',
                source='test', source_document_id='final', championship='ÖM',
                championship_eligibility_state='eligible')
            cur.execute("INSERT INTO national_minimum_starters VALUES (92,?,2,0,'insufficient')", (decision['category'],))
        self.assertEqual(db.apply_confirmed_national_event_scopes(cur, scope), 2)
        self.assertEqual(cur.execute("SELECT championship FROM result WHERE stage_id=91").fetchone()[0], None)
        self.assertEqual(cur.execute("SELECT championship FROM result WHERE category='Herren Elite'").fetchone()[0], None)
        db.apply_national_jury_decisions(cur, scope)
        db.compute_national_ranks(cur)
        self.assertEqual(cur.execute("SELECT eligible_starters,state FROM national_minimum_starters WHERE stage_id=92 ORDER BY category").fetchall(),
                         [(2,'jury_exception'), (2,'jury_exception')])
        self.assertEqual(cur.execute("SELECT national_rank FROM result WHERE stage_id=92 AND championship='ÖM' ORDER BY category").fetchall(), [(1,), (1,)])
        cur.execute("UPDATE source_document SET normalized_sha256='changed' WHERE id='final'")
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            db.apply_national_jury_decisions(cur, scope)

    def test_ski_pursuit_elite_overview_is_not_a_second_start_or_title(self):
        self.assertEqual(db.CHAMPIONSHIP_SOURCE_CONFIG['5396-2']['mode'], 'reference')
        config = db.CHAMPIONSHIP_SOURCE_CONFIG['5396-3']
        self.assertTrue(config['explicit_eligibility'])
        self.assertIn('Damen ab 21 Elite', config['eligible_categories'])
        self.assertNotIn('Offen Kurz', config['eligible_categories'])

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

    def test_historical_residency_and_nationality_conflicts_are_not_guessed(self):
        cur = self.con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (2,'ÖM','2023-06-01')")
        cur.execute("INSERT INTO stage VALUES (2,2,1,NULL,'2023-06-01',NULL)")
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (3,'ÖM','2024-06-01')")
        cur.execute("INSERT INTO stage VALUES (3,3,1,NULL,'2024-06-01',NULL)")
        for pid, nat in ((10,"HUN"),(11,"AUT"),(12,"HUN")):
            cur.execute("INSERT INTO person VALUES (?,?,?,NULL,?)", (pid,str(pid),str(pid),nat))
            cur.execute("INSERT INTO person_identifier VALUES ('oefol_id',?,?,'authoritative','anne-user-registry',NULL)", (str(pid),pid))
        for stage, pid, cat, champ in ((2,10,"H45","ÖM"),(2,11,"D45","ÖM"),
                                       (2,12,"H21E","ÖSTM"),(3,10,"H45","ÖM")):
            db.insert_result(cur, stage_id=stage, person_id=pid, category=cat, rank=1, status="ok",
                source="test", championship=champ, official_club="Testverein")
        db.insert_result(cur, stage_id=2, person_id=10, category="H40", rank=1, status="ok",
            source="test", championship="ÖM", official_club="Testverein",
            championship_eligibility_state="eligible", championship_eligibility_basis="official_championship_ranking")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.json"
            path.write_text(json.dumps({"10":{"2":None,"3":None},"11":{"2":None},"12":{"2":None}}))
            with patch.object(db, "USER_ELIGIBILITY_PATH", path):
                db.apply_championship_eligibility_overrides(cur)
                db.apply_championship_eligibility_evidence(cur)
        rows = cur.execute("SELECT stage_id,person_id,championship,championship_eligibility_state,championship_eligibility_basis FROM result WHERE person_id IN (10,11,12) ORDER BY stage_id,person_id,category").fetchall()
        self.assertEqual(rows.pop(0), (2,10,"ÖM","eligible","official_championship_ranking"))
        self.assertEqual(rows, [(2,10,"ÖM","unknown","wo2023_om_residency_unknown"),
                                (2,11,"ÖM","unknown","nationality_temporal_conflict"),
                                (2,12,None,"ineligible","anne_foreign_no_override"),
                                (3,10,None,"ineligible","anne_foreign_no_override")])

    def test_unknown_eligible_runner_ahead_does_not_promote_next_runner(self):
        cur = self.con.cursor()
        self.con.execute("DELETE FROM result")
        for pid, rank, state, basis in ((1,1,"unknown","wo2023_om_residency_unknown"),
                                       (2,2,"eligible","anne_aut_nationality"),
                                       (3,3,"eligible","anne_aut_nationality")):
            cur.execute("INSERT OR IGNORE INTO person VALUES (?,?,?,NULL,'AUT')", (pid,str(pid),str(pid)))
            db.insert_result(cur, stage_id=1, person_id=pid, category="H45", rank=rank, status="ok",
                             source="test", championship="ÖM", championship_eligibility_state=state,
                             championship_eligibility_basis=basis)
            cur.execute("UPDATE result SET championship_eligibility_state=?, championship_eligibility_basis=? WHERE id=?",
                        (state,basis,cur.lastrowid))
        cur.execute("INSERT INTO national_minimum_starters VALUES (1,'H45',3,1,'met')")
        db.compute_national_ranks(cur)
        self.assertEqual(cur.execute("SELECT national_rank FROM result ORDER BY rank").fetchall(), [(None,), (None,), (None,)])


if __name__ == "__main__":
    unittest.main()
