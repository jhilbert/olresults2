import importlib.util
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_db", ROOT / "build" / "build_db.py")
build_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_db)


class AnneIdentityTests(unittest.TestCase):
    def test_family_categories_are_conservative_and_status_ooc_is_orthogonal(self):
        self.assertEqual(build_db.classify_family_category("Family"), "family")
        self.assertEqual(build_db.classify_family_category("Rahmenbewerb Familie"), "family")
        self.assertEqual(build_db.classify_family_category("Familiy"), "family")
        self.assertEqual(build_db.classify_family_category("F"), "ambiguous")
        self.assertEqual(build_db.classify_family_category("AT-F"), "ambiguous")
        self.assertEqual(build_db.classify_family_category("D 14"), "ordinary")
        self.assertEqual(build_db.normalize_status("nc", "AK"), ("ok", 1))
        self.assertEqual(build_db.normalize_status("ok", "(39:58)"), ("ok", 1))
        self.assertEqual(build_db.normalize_status("unknown", "OMT"), ("dns", 0))
        self.assertEqual(build_db.normalize_status("unknown", "n. Ang."), ("dns", 0))
        self.assertEqual(build_db.normalize_status("unknown", "Missing Punch"), ("mp", 0))
        self.assertEqual(build_db.normalize_status("unknown", "2 Posten fehlen"), ("mp", 0))
        self.assertEqual(build_db.normalize_status("unknown", "Not Finish"), ("dnf", 0))
        self.assertEqual(build_db.normalize_status("unknown", "dis."), ("dsq", 0))
        self.assertEqual(build_db.ANNE_STATUS["overTime"], "dsq")
        self.assertEqual(build_db.normalize_status("nc", "nc"), ("unknown", 0))
        self.assertEqual(build_db.normalize_status("dnf", "DNF", True), ("dnf", 1))
        self.assertTrue(build_db.is_vienna_championship_candidate("Wr/NÖ MS Mittel"))
        self.assertTrue(build_db.is_vienna_championship_candidate("Landesmeisterschaften für Wien, NÖ"))
        self.assertFalse(build_db.is_vienna_championship_candidate(
            "47. Wiener Neustädter Stadtmeisterschaft"))
        self.assertFalse(build_db.is_vienna_championship_candidate("Wiener Schulmeisterschaft"))

    def test_family_result_can_exist_without_person_identity(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=None, category="Familie",
            status="ok", result_kind="family", source="test",
            observed_name="Livia + Papa", identity_basis="not-applicable-family",
            identity_confidence=1.0, identity_state="not_applicable")
        self.assertEqual(
            con.execute(
                "SELECT person_id, observed_name, result_kind, identity_state FROM result"
            ).fetchone(),
            (None, "Livia + Papa", "family", "not_applicable"),
        )

    def test_numeric_rank_classifies_a_parser_row_without_printed_time(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        build_db.insert_result(
            con.cursor(), stage_id=1, category="D3", rank=1,
            status="unknown", source="sportsoftware-pdf",
            observed_name="Anna Rang", observed_status="unknown")
        build_db.insert_result(
            con.cursor(), stage_id=1, category="D3", rank=2,
            status="unknown", source="anne-api",
            observed_name="Berta API", observed_status="notClassified")
        self.assertEqual(con.execute(
            "SELECT status FROM result ORDER BY id"
        ).fetchall(), [("ok",), ("unknown",)])

    def test_same_runner_on_two_relay_legs_has_distinct_unit_identity(self):
        first = {"resultKind": "relay", "teamNumber": "239", "leg": 1}
        second = {"resultKind": "relay", "teamNumber": "239", "leg": 2}
        first_identity = build_db.legacy_result_unit_identity(
            first, "relay", build_db.relay_metadata(first, "relay"), True)
        second_identity = build_db.legacy_result_unit_identity(
            second, "relay", build_db.relay_metadata(second, "relay"), True)
        self.assertEqual(first_identity, ("239", 1))
        self.assertEqual(second_identity, ("239", 2))
        self.assertNotEqual(first_identity, second_identity)

    def test_quality_model_does_not_flag_a_deduplicated_second_source(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'ÖM Test')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        for doc_id, list_id in (("doc:pdf", "list:pdf"), ("doc:html", "list:html")):
            cur.execute(
                "INSERT INTO source_document (id, event_id, source_type, file_name) VALUES (?,?,?,?)",
                (doc_id, 1, "sportsoftware-pdf", doc_id))
            cur.execute(
                """INSERT INTO result_list (id, stage_id, source_document_id, category,
                   declared_starters, parsed_entries, parsed_rows, input_fingerprint)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (list_id, 1, doc_id, "H21", 1, 1, 1, doc_id))
        build_db.insert_result(cur, stage_id=1, result_list_id="list:pdf", category="H21",
                               status="ok", time_s=100, rank=1, source="sportsoftware-pdf",
                               observed_name="Max Beispiel")
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT count(*) FROM audit_issue WHERE code = 'entry_count_mismatch'"
        ).fetchone()[0], 0)

    def test_annulled_category_does_not_require_a_ranking(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'ÖM Test')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) VALUES ('doc',1,'sportsoftware-html')")
        cur.execute(
            """INSERT INTO result_list
               (id,stage_id,source_document_id,category,category_full,
                declared_starters,parsed_entries,parsed_rows,input_fingerprint)
               VALUES ('list',1,'doc','D40','D40 (3) Annulliert',3,3,3,'x')""")
        for name, seconds in (("Anna A", 100), ("Berta B", 110), ("Clara C", 120)):
            build_db.insert_result(
                cur, stage_id=1, result_list_id="list", category="D40",
                status="ok", time_s=seconds, source="sportsoftware-html",
                observed_name=name)
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT count(*) FROM audit_issue WHERE code = 'missing_ranking'"
        ).fetchone()[0], 0)

    def test_score_ranking_does_not_raise_time_inversion(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'Score-OL')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) VALUES ('doc',1,'sportsoftware-pdf')")
        rows = [
            {"name": "Anna Punkte", "rank": 1, "timeS": 2700, "scoreText": "300"},
            {"name": "Berta Schnell", "rank": 2, "timeS": 2400, "scoreText": "250"},
        ]
        list_id = build_db.register_result_list(
            cur, 1, "doc", "Damen", "Damen", 2, rows)
        for row in rows:
            build_db.insert_result(
                cur, stage_id=1, result_list_id=list_id, category="Damen",
                rank=row["rank"], status="ok", time_s=row["timeS"],
                source="sportsoftware-pdf", observed_name=row["name"])
        build_db.populate_quality_model(cur)
        self.assertEqual(
            cur.execute("SELECT ranking_basis FROM result_list").fetchone()[0], "score")
        self.assertEqual(cur.execute(
            "SELECT count(*) FROM audit_issue WHERE code='rank_time_inversion'"
        ).fetchone()[0], 0)

    def test_source_native_score_and_cup_series_are_not_time_rankings(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'Source rankings')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) VALUES ('doc',1,'anne-api')")

        score = build_db.register_result_list(
            cur, 1, "doc", "Pro", "Pro", 2, [
                {"name": "Anna", "rank": 1, "timeS": 1802, "scorePoints": 330},
                {"name": "Berta", "rank": 2, "timeS": 1740, "scorePoints": 312},
            ])
        meos_score = build_db.register_result_list(
            cur, 1, "doc", "Score", "Score", 2, [
                {"name": "Clara", "rank": 1, "timeS": 2700,
                 "club": "WAT 640 p."},
                {"name": "Dora", "rank": 2, "timeS": 2400,
                 "club": "Bad Vöslau 623 p."},
            ])
        series = build_db.register_result_list(
            cur, 1, "doc", "DA 1.Lauf - A 2.Lauf - B", "DA", 2, [
                {"name": "Eva", "rank": 1, "timeS": 3806},
                {"name": "Fiona", "rank": 2, "timeS": 3334},
            ])
        self.assertEqual(
            cur.execute("SELECT ranking_basis FROM result_list WHERE id=?", (score,)).fetchone()[0],
            "score")
        self.assertEqual(
            cur.execute("SELECT ranking_basis FROM result_list WHERE id=?", (meos_score,)).fetchone()[0],
            "score")
        self.assertEqual(
            cur.execute("SELECT ranking_basis FROM result_list WHERE id=?", (series,)).fetchone()[0],
            "other")

    def test_fully_classified_extra_source_row_is_not_a_parser_blocker(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'Fehlerhafter Quellkopf')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) VALUES ('doc',1,'sportsoftware-pdf')")
        rows = [
            {"name": "Anna A", "rank": 1, "timeS": 100, "status": "ok"},
            {"name": "Berta B", "rank": 2, "timeS": 110, "status": "ok"},
            {"name": "Clara C", "rank": 3, "timeS": 120, "status": "ok"},
        ]
        list_id = build_db.register_result_list(cur, 1, "doc", "Damen", "Damen", 2, rows)
        for row in rows:
            build_db.insert_result(
                cur, stage_id=1, result_list_id=list_id, category="Damen",
                rank=row["rank"], status="ok", time_s=row["timeS"],
                source="sportsoftware-pdf", observed_name=row["name"])
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT code || ':' || severity FROM audit_issue"
        ).fetchone()[0], "source_count_anomaly:warning")

    def test_normalized_source_count_groups_expanded_team_members(self):
        rows = [
            {"resultKind": "relay", "teamNumber": "106", "name": "Anna", "leg": 1},
            {"resultKind": "relay", "teamNumber": "106", "name": "Berta", "leg": 2},
            {"resultKind": "relay", "teamNumber": "119", "name": "Clara", "leg": 1},
            {"resultKind": "individual", "name": "Dora"},
        ]
        self.assertEqual(build_db.normalized_source_unit_count(rows), 3)

    def test_pair_roster_count_is_stable_with_duplicate_display_names(self):
        rows = [
            {"resultKind": "pair", "name": "Muslic B",
             "note": "Partner: Muslic T, Muslic S, Muslic S"},
            {"resultKind": "pair", "name": "Muslic T",
             "note": "Partner: Muslic B, Muslic S, Muslic S"},
            {"resultKind": "pair", "name": "Muslic S",
             "note": "Partner: Muslic B, Muslic T"},
        ]
        self.assertEqual(build_db.normalized_source_unit_count(rows), 1)

    def test_synthetic_ids_are_deterministic_and_identity_scoped(self):
        first = build_db.PersonRegistry().from_legacy("Anna Beispiel", 1988)[0]
        second = build_db.PersonRegistry().from_legacy("Anna Beispiel", 1988)[0]
        different_yob = build_db.PersonRegistry().from_legacy("Anna Beispiel", 1989)[0]
        self.assertLess(first, 0)
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_yob)

    def test_user_id_normalizes_numeric_strings(self):
        self.assertEqual(build_db.anne_user_id("9910"), 9910)
        self.assertEqual(build_db.anne_user_id(9910), 9910)
        self.assertIsNone(build_db.anne_user_id(None))
        self.assertIsNone(build_db.anne_user_id("not-an-id"))

    def test_unique_anne_name_and_club_match_is_automatically_resolved(self):
        profiles = build_db.AnneProfileIndex([{
            "oefol_id": 9589,
            "first_name": "Mariana",
            "last_name": "König-Brasil",
            "year_of_birth": 2015,
            "active_memberships": [{"club": {"name": "OLC Graz"}}],
        }])
        persons = build_db.PersonRegistry(profiles)
        pid, basis, confidence, state = persons.from_legacy(
            "Mariana König-Brasil", None, "OLC Graz")
        self.assertEqual(pid, 9589)
        self.assertEqual(basis, "anne-registry-name-club")
        self.assertEqual(confidence, 0.95)
        self.assertEqual(state, "resolved")

    def test_ambiguous_anne_name_and_club_match_stays_candidate(self):
        profiles = build_db.AnneProfileIndex([
            {"oefol_id": 1001, "first_name": "Max", "last_name": "Muster",
             "active_memberships": [{"club": {"name": "Testverein"}}]},
            {"oefol_id": 1002, "first_name": "Max", "last_name": "Muster",
             "active_memberships": [{"club": {"name": "Testverein"}}]},
        ])
        persons = build_db.PersonRegistry(profiles)
        pid, basis, _confidence, state = persons.from_legacy(
            "Max Muster", None, "Testverein")
        self.assertLess(pid, 0)
        self.assertEqual(basis, "legacy-name")
        self.assertEqual(state, "candidate")

    def test_relay_members_keep_authoritative_anne_ids(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        persons = build_db.PersonRegistry()
        team = {
            "teamMembers": [
                {"userId": "9910", "firstName": "Jara", "lastName": "Leonhardt",
                 "yearOfBirth": 2014, "nationality": "AUT", "leg": 1,
                 "classification": "classified", "overall": {"time": 600}},
                {"userId": "9787", "firstName": "Mira", "lastName": "Veitsberger",
                 "yearOfBirth": 2013, "nationality": "AUT", "leg": 2,
                 "classification": "classified", "overall": {"time": 1100}},
            ],
            "teamName": "Leonhardt/Veitsberger",
            "clubName": "OC Fürstenfeld",
            "categoryTitle": "D14",
            "rank": 1,
            "classification": "classified",
        }

        inserted = build_db.insert_anne_relay(con.cursor(), persons, 1, "D14", team)

        self.assertEqual(inserted, 2)
        self.assertEqual(set(persons.by_id), {9910, 9787})
        self.assertEqual(
            con.execute("SELECT person_id, time_s FROM result ORDER BY person_id").fetchall(),
            [(9787, 500), (9910, 600)],
        )
        self.assertEqual(persons.by_id[9910][2:], (2014, "AUT"))
        self.assertEqual(
            con.execute(
                "SELECT observed_user_id, identity_basis, identity_confidence "
                "FROM result ORDER BY person_id").fetchall(),
            [("9787", "source-oefol-id", 1.0), ("9910", "source-oefol-id", 1.0)],
        )

    def test_verified_members_survive_a_crossed_anne_name_and_user_id(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        persons = build_db.PersonRegistry()

        persons.from_anne(1644, "Christian Arbter", None, None)
        persons.record(1644, "Christian Arbter", authoritative=True)
        persons.from_anne(3682, "Christian Arbter", None, None)
        persons.record(3682, "Christian Arbter", authoritative=True)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=3682, category="H21", status="ok",
            source="anne-api", observed_name="Christian Arbter",
            observed_user_id="3682", identity_basis="source-oefol-id",
            identity_confidence=1.0,
        )

        synthetic_sabine = persons.from_legacy("Sabine Jandl", None)[0]
        persons.record(synthetic_sabine, "Sabine Jandl")
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=synthetic_sabine, category="D45",
            status="ok", source="anne-api", observed_name="Sabine Jandl",
            identity_basis="legacy-name", identity_confidence=0.55,
        )
        persons.from_anne(3682, "Sabine Jandl", None, None)
        persons.record(3682, "Sabine Jandl", authoritative=True)

        members = [
            {"ofol_id": 1644, "name": "Christian Arbter",
             "name_key": build_db.name_key("Christian Arbter"), "yob": 1990},
            {"ofol_id": 3682, "name": "Sabine Jandl",
             "name_key": build_db.name_key("Sabine Jandl"), "yob": 1969},
        ]
        corrections = build_db.prepare_verified_member_identities(
            con.cursor(), persons, members)
        merges = build_db.duplicate_identity_merge_edges(
            persons, (m["ofol_id"] for m in members))

        self.assertEqual(persons.by_id[1644][:3],
                         ("Christian Arbter", "arbter christian", 1990))
        self.assertEqual(persons.by_id[3682][:3],
                         ("Sabine Jandl", "jandl sabine", 1969))
        self.assertEqual(merges.get(synthetic_sabine), 3682)
        self.assertNotIn(3682, merges)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(
            con.execute(
                "SELECT person_id, observed_user_id, identity_basis "
                "FROM result WHERE observed_user_id = '3682'").fetchone(),
            (1644, "3682", "club-book-of-record"),
        )

    def test_verified_member_id_wins_over_a_duplicate_anne_account(self):
        persons = build_db.PersonRegistry()
        persons.from_anne(3000, "Ada Beispiel", 1985, None)
        persons.from_anne(5000, "Ada Beispiel", 1985, None)

        merges = build_db.duplicate_identity_merge_edges(persons, {5000})

        self.assertEqual(merges, {3000: 5000})

    def test_disjoint_roster_name_repairs_only_the_crossed_source_row(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        persons = build_db.PersonRegistry()
        persons.from_anne(8520, "Kathrin Kollndorfer", 1981, None)
        persons.record(8520, "Kathrin Kollndorfer", authoritative=True)
        persons.from_anne(10344, "Kathrin Kollndorfer", 1981, None)
        persons.record(10344, "Kathrin Kollndorfer", authoritative=True)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=10344, category="D40", status="ok",
            source="anne-api", observed_name="Kathrin Kollndorfer",
            observed_user_id="10344", identity_basis="source-oefol-id",
            identity_confidence=1.0,
        )
        members = [{
            "ofol_id": 10344, "name": "Jean-Baptiste Le Blanc",
            "name_key": build_db.name_key("Jean-Baptiste Le Blanc"), "yob": 1981,
        }]

        corrections = build_db.prepare_verified_member_identities(
            con.cursor(), persons, members)
        merges = build_db.duplicate_identity_merge_edges(persons, {10344})

        self.assertEqual(con.execute("SELECT person_id FROM result").fetchone(), (8520,))
        self.assertEqual(persons.by_id[10344][0], "Jean-Baptiste Le Blanc")
        self.assertNotIn(8520, merges)
        self.assertNotIn(10344, merges)
        self.assertEqual(corrections[0]["assigned_person_id"], 8520)


if __name__ == "__main__":
    unittest.main()
