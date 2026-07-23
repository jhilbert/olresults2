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
        self.assertEqual(build_db.classify_family_category("F", "Familie"), "family")
        self.assertEqual(build_db.classify_family_category("AT-F", "Family"), "family")
        self.assertEqual(build_db.classify_family_category("F", event_id=4245), "family")
        self.assertEqual(build_db.classify_family_category("F", event_id=4248), "ordinary")
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
        self.assertEqual(build_db.normalize_status("nc", "nc"), ("ok", 1))
        self.assertEqual(build_db.normalize_status("dnf", "DNF", True), ("dnf", 1))
        self.assertFalse(build_db.is_active_anne_result({"classification": "inactive"}))
        self.assertTrue(build_db.is_active_anne_result({"classification": "classified"}))
        self.assertFalse(build_db.is_active_anne_result({
            "classification": "disqualified", "categoryShortTitle": "empty",
            "clubName": "27:16:00 0"}))
        self.assertTrue(build_db.is_active_anne_result({
            "classification": "disqualified", "categoryShortTitle": "H21",
            "clubName": "HSV Ried"}))
        self.assertTrue(build_db.is_vienna_championship_candidate("Wr/NÖ MS Mittel"))
        self.assertTrue(build_db.is_vienna_championship_candidate("Landesmeisterschaften für Wien, NÖ"))
        self.assertFalse(build_db.is_vienna_championship_candidate(
            "47. Wiener Neustädter Stadtmeisterschaft"))
        self.assertFalse(build_db.is_vienna_championship_candidate("Wiener Schulmeisterschaft"))
        self.assertFalse(build_db.is_om_eligible_category("CZ-D35B"))
        self.assertFalse(build_db.is_om_eligible_category("M40 (CZE)"))
        self.assertTrue(build_db.is_om_eligible_category("AT-D 35-"))

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

    def test_memberless_dns_team_can_exist_without_person_identity(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=None, category="Mixed bis 16",
            status="dns", result_kind="relay", source="test",
            team_number="74", team_name="HSV OL Wiener Neustadt 3",
            team_status="dns", observed_status="dns",
            identity_basis="not-applicable-memberless-team",
            identity_confidence=1.0, identity_state="not_applicable")
        self.assertEqual(
            con.execute(
                "SELECT person_id, team_number, team_name, status, identity_state FROM result"
            ).fetchone(),
            (None, "74", "HSV OL Wiener Neustadt 3", "dns", "not_applicable"),
        )

    def test_relay_placeholders_are_not_person_names(self):
        self.assertTrue(build_db.is_relay_placeholder_name("N.N."))
        self.assertTrue(build_db.is_relay_placeholder_name("N.N. N.N."))
        self.assertTrue(build_db.is_relay_placeholder_name("N.N. N Ang"))
        self.assertFalse(build_db.is_relay_placeholder_name("Nina Muster"))

    def test_memberless_pair_uses_explicit_team_metadata(self):
        row = {
            "resultKind": "pair", "memberlessTeam": True,
            "teamNumber": "pair-2", "teamName": "WAT-OL",
            "status": "ok", "teamStatus": "ok", "timeS": 9000,
            "teamTimeS": 9000, "teamTimeText": "2:30:00",
        }
        self.assertEqual(build_db.relay_metadata(row, "pair"), {
            "team_number": "pair-2", "team_name": "WAT-OL",
            "leg_number": None, "leg_count": None,
            "individual_status": None, "team_status": "ok",
            "team_time_s": 9000, "observed_team_time": "2:30:00",
        })

    def test_regional_team_key_keeps_numeric_team_numbers_distinct(self):
        base = (1, 100, "relay")
        row_28 = base + ("28", "WAT-OL 1", "WAT-OL", "WAT-OL", 1, "ok", 1000, 0)
        row_29 = base + ("29", "WAT-OL 2", "WAT-OL", "WAT-OL", 2, "ok", 1100, 0)
        self.assertNotEqual(build_db._regional_unit_key(row_28),
                            build_db._regional_unit_key(row_29))

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

    def test_championship_category_codes_match_spelled_source_classes(self):
        self.assertEqual(build_db.championship_category_key("D-14"), "d14")
        self.assertEqual(build_db.championship_category_key("Damen bis 14"), "d14")
        self.assertEqual(build_db.championship_category_key("H21-E"), "h21e")
        self.assertEqual(build_db.championship_category_key("Herren Elite"), "h21e")

    def test_regional_category_detection_splits_shared_courses_not_results(self):
        mappings = build_db.regional_mappings_for_list(
            "H40-(St,B)/H45-(NOe,W)", "LM Nacht Ost", "", "ergebnis.pdf")
        actual = {(m["jurisdiction"], m["category_key"]) for m in mappings}
        self.assertEqual(actual, {
            ("STMK", "h40"), ("BGLD", "h40"),
            ("NOE", "h45"), ("WIEN", "h45"),
        })
        self.assertTrue(all(m["partition_required"] for m in mappings))
        asymmetric = build_db.regional_mappings_for_list(
            "H40 B/H45 NÖ,W", "Landesmeisterschaften für Wien, NÖ, Bgld", "", "erg.pdf")
        self.assertEqual({(m["jurisdiction"], m["category_key"]) for m in asymmetric}, {
            ("BGLD", "h40"), ("NOE", "h45"), ("WIEN", "h45"),
        })
        self.assertTrue(all(m["partition_required"] for m in asymmetric))

    def test_parenthesized_eastern_night_title_and_source_nat_codes(self):
        mappings = build_db.regional_mappings_for_list(
            "D-14", "LM Nacht (Ost)", "", "event_4963_ergebnis.pdf")
        self.assertEqual(
            {mapping["jurisdiction"] for mapping in mappings},
            {"WIEN", "NOE", "BGLD", "STMK"})
        self.assertTrue(all(mapping["partition_required"] for mapping in mappings))
        self.assertEqual(
            [build_db.source_nat_jurisdiction(value)
             for value in ("W", "NÖ", "B", "St.", "AUT", "")],
            ["WIEN", "NOE", "BGLD", "STMK", None, None])

    def test_source_nat_promotes_title_candidate_to_confirmed_entry(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id,title) VALUES (4963,'LM Nacht (Ost)')")
        cur.execute("INSERT INTO stage (id,event_id,number) VALUES (1,4963,1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type,file_name) "
            "VALUES ('doc',4963,'sportsoftware-pdf','event_4963_ergebnis.pdf')")
        cur.execute(
            """INSERT INTO result_list
               (id,stage_id,source_document_id,category,parsed_entries,
                parsed_rows,input_fingerprint)
               VALUES ('list',1,'doc','D-14',1,1,'fingerprint')""")
        build_db.insert_result(
            cur, stage_id=1, result_list_id="list", category="D-14",
            status="ok", rank=1, time_s=1200, source="sportsoftware-pdf",
            source_document_id="doc", observed_name="Mona Oswald",
            observed_nation="B")
        build_db.populate_regional_championships(cur, {}, {})
        self.assertEqual(cur.execute(
            """SELECT ci.jurisdiction,ci.state,ce.eligibility_state,
                      ce.eligibility_basis
                 FROM championship_instance ci
                 JOIN championship_entry ce ON ce.championship_instance_id=ci.id"""
        ).fetchall(), [("BGLD", "confirmed", "eligible", "explicit-source-nat")])

    def test_regional_compact_codes_and_frame_categories(self):
        event = "Wr/NÖ Mitteldistanz MS"
        wien = build_db.regional_mappings_for_list(
            "Damen 45-W", event, "", "ergebnis.pdf")
        self.assertEqual([(m["jurisdiction"], m["category_key"]) for m in wien],
                         [("WIEN", "d45")])
        self.assertTrue(wien[0]["partition_required"])
        self.assertEqual(build_db.regional_mappings_for_list(
            "Damen 45-R", event, "", "ergebnis.pdf"), [])
        self.assertEqual(build_db.regional_mappings_for_list(
            "Rahmenbewerb Familie", event, "", "ergebnis.pdf"), [])
        self.assertEqual(build_db.regional_mappings_for_list(
            "R Fam", event, "", "ergebnis.pdf"), [])
        prefixed = build_db.regional_mappings_for_list(
            "N D-12", "NÖ & Wr. Sprint-MS", "", "ergebnis.pdf")
        self.assertEqual([(m["jurisdiction"], m["category_key"]) for m in prefixed],
                         [("NOE", "d12")])

    def test_women_and_novice_classes_are_not_compact_state_codes(self):
        event = "NÖ & Wr. Sprint-MS"
        self.assertEqual(build_db.regional_mappings_for_list(
            "W -14", event, "", "results.pdf"), [])
        self.assertEqual(build_db.regional_mappings_for_list(
            "W 40-", event, "", "results.pdf"), [])
        self.assertEqual(build_db.regional_mappings_for_list(
            "N", event, "", "results.pdf"), [])
        self.assertEqual(build_db.regional_mappings_for_list(
            "AT-N", "NÖ MS Ski-O", "", "results.pdf"), [])
        self.assertEqual(
            {m["jurisdiction"] for m in build_db.regional_mappings_for_list(
                "D45-W", event, "", "results.pdf")}, {"WIEN"})
        self.assertEqual(
            {m["jurisdiction"] for m in build_db.regional_mappings_for_list(
                "H35(W,NÖ)", event, "", "results.pdf")}, {"WIEN", "NOE"})

    def test_historical_regional_club_aliases_are_exact(self):
        official = build_db.load_official_clubs()
        self.assertEqual(build_db.canonicalize_official_club(
            "SOLV Salzburg 6", official), "SOLV Salzburg")
        self.assertEqual(build_db.canonicalize_official_club(
            "TV Fürstenfeld", official), "OC Fürstenfeld")
        self.assertEqual(build_db.canonicalize_official_club(
            "Leibnitzer Athletik Club-OLGem", official), "Leibnitzer AC OLG")
        self.assertEqual(build_db.canonicalize_official_club(
            "WAT-OL WAT-OL", official), "WAT-OL")
        self.assertEqual(build_db.canonicalize_official_club(
            "FUN-OL NÖe", official), "FUN.O NOe")
        self.assertEqual(build_db.canonicalize_official_club(
            "Naturfreunde Kitzb?hel", official), "Naturfreunde Kitzbühel")
        self.assertEqual(build_db.canonicalize_official_club(
            "ner  OK gittis Klosterneubu", official),
            "Orienteering Klosterneuburg")
        self.assertEqual(build_db.canonicalize_official_club(
            "OLG DKB", official), "SKV OLG Deutsch Kaltenbrunn")
        self.assertEqual(
            build_db.KNOWN_RESULT_CLUB_OVERRIDES[(3847, "Thomas Neuhold")],
            "Orienteering Klosterneuburg")

    def test_clubless_regional_row_is_not_unresolved_membership(self):
        base = (1, 100, "individual", None, None)
        clubless = base + ("Vereinslos (no club)", None, 1, "ok", 1000, 0)
        unknown = base + ("Historischer OL Verein", None, 1, "ok", 1000, 0)
        self.assertFalse(build_db._regional_unit_has_unresolved_club(
            [clubless], {"WAT-OL": "WIEN"}))
        self.assertTrue(build_db._regional_unit_has_unresolved_club(
            [unknown], {"WAT-OL": "WIEN"}))

    def test_compact_b_class_is_not_misread_as_burgenland(self):
        mappings = build_db.regional_mappings_for_list(
            "Damen 19B- Stmk", "Landesmeisterschaften für Stmk, Bgld", "", "erg.pdf")
        self.assertEqual({m["jurisdiction"] for m in mappings}, {"STMK"})
        self.assertEqual(mappings[0]["category_key"], "d19b")

    def test_compact_state_codes_require_matching_event_context(self):
        kurz = build_db.regional_mappings_for_list(
            "H21-K", "ÖM Mittel und St. MS", "", "erg.pdf")
        self.assertEqual({m["jurisdiction"] for m in kurz}, {"STMK"})
        self.assertTrue(all(m["state"] == "candidate" for m in kurz))
        self.assertNotIn("KTN", {m["jurisdiction"] for m in kurz})

        b_class = build_db.regional_mappings_for_list(
            "D19-B", "St. MS Lang", "", "erg.pdf")
        self.assertEqual({m["jurisdiction"] for m in b_class}, {"STMK"})
        self.assertEqual(b_class[0]["category_key"], "d19b")
        self.assertNotIn("BGLD", {m["jurisdiction"] for m in b_class})

    def test_foreign_joint_event_categories_are_not_state_rankings(self):
        self.assertEqual(build_db.regional_mappings_for_list(
            "W18-SLO", "NÖ, Wien und Stmk Landesmeisterschaft", "", "erg.pdf"), [])

    def test_national_and_dotted_abbreviations_do_not_create_false_states(self):
        self.assertEqual(build_db.extract_regional_jurisdictions(
            "Ö(ST)M Mixed Sprint Staffel", compact=True), set())
        self.assertEqual(build_db.extract_regional_jurisdictions(
            "N.Ö./W StaffelMS", compact=True), {"NOE", "WIEN"})
        self.assertEqual(build_db.regional_mappings_for_list(
            "O", "Ö(ST)M Mixed Sprint Staffel", "", "erg.pdf"), [])

    def test_state_codes_without_championship_marker_are_not_enough(self):
        self.assertEqual(build_db.regional_mappings_for_list(
            "Damen 15 B", "1. SC B u. St", "", "erg.pdf"), [])

    def test_discipline_and_comparison_text_do_not_add_states(self):
        self.assertEqual(build_db.extract_regional_jurisdictions(
            "Ski-O Austria Cup / ÖSTM / Steir. MS", compact=True), {"STMK"})
        self.assertEqual(build_db.extract_regional_jurisdictions(
            "Steirische MS, Ländervergleich Steiermark-Kärnten", compact=True),
            {"STMK"})

    def test_dedicated_wiener_result_document_is_authoritative(self):
        mappings = build_db.regional_mappings_for_list(
            "Damen 45-", "Wr/NÖ Nachwuchs- und Senioren MS", "",
            "event_3055_ergebnis-wienerwertung.pdf")
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["jurisdiction"], "WIEN")
        self.assertEqual(mappings[0]["evidence_kind"], "document")
        self.assertEqual(mappings[0]["state"], "confirmed")
        self.assertFalse(mappings[0]["partition_required"])

    def test_wiener_neustadt_is_not_misread_as_wien(self):
        self.assertEqual(build_db.extract_regional_jurisdictions(
            "47. Wiener Neustädter Stadtmeisterschaft"), set())

    def test_numbered_result_attachment_maps_to_stage_number(self):
        self.assertEqual(
            build_db.ETAPPE_FILENAME_RE.search(
                "PANNON-MTBO2026-RESULT2.html").group(1),
            "2")
        self.assertEqual(
            build_db.ETAPPE_FILENAME_RE.search(
                "Ergebnisse_1Etappe_nach Kategorien.html").group(1),
            "1")

    def test_anne_transport_duplicates_are_collapsed(self):
        base = {
            "id": 100, "updatedAt": "2026-01-01T00:00:00Z",
            "firstName": "Anna", "lastName": "Muster", "rank": 1,
            "time": 1234, "categoryShortTitle": "D21",
        }
        duplicate = dict(base, id=101, updatedAt="2026-01-02T00:00:00Z")
        distinct = dict(base, id=102, rank=2)

        rows = build_db.deduplicate_anne_rows([base, duplicate, distinct])

        self.assertEqual(rows, [base, distinct])

    def test_names_and_clubs_repair_safe_mojibake_and_placeholders(self):
        self.assertEqual(build_db.clean_name("BjÃ¶rn Chudoba"), "Björn Chudoba")
        self.assertEqual(build_db.clean_club("Naturfreunde KitzbÃ¼hel"),
                         "Naturfreunde Kitzbühel")
        self.assertFalse(build_db.is_valid_name("Vakant"))
        self.assertFalse(build_db.is_valid_name("N.N."))
        self.assertTrue(build_db.is_valid_name("Václav Novák"))
        self.assertEqual(
            build_db.canonicalize_official_club(
                "HSV OL Wr. Neustadt", build_db.OFFICIAL_CLUBS),
            "HSV OL Wiener Neustadt",
        )
        index = build_db.AnneProfileIndex([{
            "oefol_id": 3289, "first_name": "Gregor", "last_name": "SchÃ¼tz",
            "active_memberships": [{
                "club": {"name": "HSV OL Wiener Neustadt"},
                "sport_type": "footOrienteering",
                "date_from": "2020-01-01T00:00:00Z",
                "date_to": None,
                "active": True,
            }],
        }])
        self.assertEqual(index.by_id[3289]["name"], "Gregor Schütz")
        self.assertEqual(index.by_id[3289]["active_memberships"], ({
            "club": "HSV OL Wiener Neustadt",
            "sport_type": "footOrienteering",
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_to": None,
            "active": True,
        },))
        self.assertEqual(build_db.KNOWN_NAME_TYPOS[(1583, "Jánošková Ta�jana")],
                         "Taťjana Jánošková")
        self.assertEqual(build_db.KNOWN_NAME_TYPOS[(4482, "Uwe Waldh?tter")],
                         "Uwe Waldhütter")
        self.assertEqual(
            build_db.clean_result_name(4482, "Uwe Waldh?tter"),
            "Uwe Waldhütter",
        )
        self.assertTrue(build_db.is_valid_name(
            build_db.clean_result_name(4482, "Maya Eichm?ller")))

    def test_qualitative_results_never_gain_a_rank(self):
        categories = [{"results": [
            {"name": "Kind Eins", "timeText": "gut", "status": "ok", "rank": 1},
            {"name": "Kind Drei", "timeText": "g Erfolgreich teilgenommen",
             "status": "ok", "rank": 1},
            {"name": "Kind Zwei", "timeText": "N Ang", "status": "dns"},
        ]}]

        build_db.normalize_qualitative_result_ranks(categories)

        self.assertNotIn("rank", categories[0]["results"][0])
        self.assertNotIn("rank", categories[0]["results"][1])

    def test_course_view_is_dropped_only_with_same_stage_category_view(self):
        docs = [
            {"eventId": 1, "fileName": "Ergebnisse_1Etappe_nach Kategorien.html",
             "listType": "race", "_anneStage": {"number": 1, "date": "2026-07-08"}},
            {"eventId": 1, "fileName": "Ergebnisse_1Etappe_nach_Bahnen.html",
             "listType": "race", "_anneStage": {"number": 1, "date": "2026-07-08"}},
            {"eventId": 1, "fileName": "Ergebnisse_2Etappe_nach_Bahnen.html",
             "listType": "race", "_anneStage": {"number": 2, "date": "2026-07-09"}},
        ]

        kept = build_db.drop_redundant_course_views(docs)

        self.assertEqual(
            [doc["fileName"] for doc in kept],
            ["Ergebnisse_1Etappe_nach Kategorien.html",
             "Ergebnisse_2Etappe_nach_Bahnen.html"],
        )

    def test_supplemental_attachment_reuses_existing_anne_stage(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id,title) VALUES (5280,'Zwei Etappen')")
        cur.execute(
            "INSERT INTO stage (id,event_id,number,title,date) VALUES (728,5280,2,'6. AC','2026-07-19')")
        stage_ids = {728}
        self.assertEqual(build_db.anne_mapped_stage(
            cur, {"id": 5280, "location": "Márkó"}, stage_ids,
            {"number": 2, "title": "6. AC", "date": "2026-07-19"}), 728)
        self.assertEqual(cur.execute(
            "SELECT COUNT(*) FROM stage WHERE event_id=5280 AND number=2"
        ).fetchone()[0], 1)

    def test_filtered_pair_ranking_marks_both_source_members_eligible(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id,title) VALUES (1,'ÖM Nacht')")
        cur.execute("INSERT INTO stage (id,event_id,number) VALUES (1,1,1)")
        cur.execute("INSERT INTO person VALUES (1,'Anna Eins','anna eins',2012,'AUT')")
        cur.execute("INSERT INTO person VALUES (2,'Berta Zwei','berta zwei',2012,'AUT')")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) VALUES ('doc',1,'pdf')")
        for person_id, name in ((1, "Anna Eins"), (2, "Berta Zwei")):
            build_db.insert_result(
                cur, stage_id=1, person_id=person_id, category="Damen bis 14",
                club="OLC Graz", rank=3, status="ok", result_kind="pair",
                source="sportsoftware-pdf", observed_name=name,
                observed_club="OLC Graz")
        cur.execute(
            """INSERT INTO championship_source_entry VALUES
               ('e',1,'doc','D-14','d14','Anna-Berta Eins-Zwei',
                'anna berta eins zwei','OLC Graz',3,'ok',NULL,'ÖM',
                'official_championship_inclusion','medal_places_only')""")
        build_db.apply_championship_source_entries(cur)
        self.assertEqual(cur.execute(
            "SELECT championship,championship_eligibility_state FROM result ORDER BY id"
        ).fetchall(), [("ÖM", "eligible"), ("ÖM", "eligible")])

    def test_unknown_foreign_runner_gets_no_national_rank_or_medal(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id,title,date_from) VALUES (1,'ÖM Sprint','2026-03-22')")
        cur.execute("INSERT INTO stage (id,event_id,number) VALUES (1,1,1)")
        runners = [
            (1, "Tobias", 1, "eligible"),
            (2, "Adrian", 2, "eligible"),
            (3, "Vít", 3, "unknown"),
            (4, "Jakob", 4, "eligible"),
        ]
        for person_id, name, rank, eligibility in runners:
            cur.execute("INSERT INTO person VALUES (?,?,?,NULL,NULL)",
                        (person_id, name, build_db.name_key(name)))
            build_db.insert_result(
                cur, stage_id=1, person_id=person_id, category="H-12",
                rank=rank, status="ok", result_kind="individual", source="test",
                observed_name=name)
            cur.execute(
                """UPDATE result SET championship='ÖM',
                       championship_eligibility_state=?,
                       championship_eligibility_basis=?
                   WHERE person_id=?""",
                (eligibility,
                 "anne_aut_nationality" if eligibility == "eligible"
                 else "no_verified_eligibility_evidence",
                 person_id))

        build_db.compute_national_ranks(cur)
        self.assertEqual(cur.execute(
            "SELECT person_id,national_rank FROM result ORDER BY rank"
        ).fetchall(), [(1, 1), (2, 2), (3, None), (4, 3)])

        build_db.populate_championship_model(cur)
        self.assertEqual(cur.execute(
            """SELECT r.person_id,a.medal,a.state FROM award a
               JOIN result r ON r.id=a.result_id ORDER BY a.award_rank"""
        ).fetchall(), [
            (1, "gold", "derived"),
            (2, "silver", "derived"),
            (4, "bronze", "derived"),
        ])

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

    def test_person_result_counts_repeated_relay_legs_once_per_team(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        common = {
            "stage_id": 1, "person_id": 3946,
            "result_list_id": "list:skio", "category": "Mixed Staffel ab 18",
            "rank": 1, "status": "ok", "result_kind": "relay",
            "team_name": "OLC Graz / LZ OMAHA", "source": "test",
        }
        build_db.insert_result(cur, **common, leg_number=1)
        build_db.insert_result(cur, **common, leg_number=3)
        build_db.insert_result(cur, **common, leg_number=5)
        build_db.insert_result(
            cur, **{**common, "team_name": "OLC Graz 2"}, leg_number=1)
        self.assertEqual(
            con.execute(
                "SELECT team_name, leg_number FROM person_result ORDER BY id"
            ).fetchall(),
            [("OLC Graz / LZ OMAHA", 1), ("OLC Graz 2", 1)],
        )
    def test_mixed_relay_label_resolves_each_unique_anne_member_club(self):
        profiles = build_db.AnneProfileIndex([
            {"oefol_id": 9718, "first_name": "Lisa", "last_name": "Hauser",
             "active_memberships": [{"club": {"name": "Naturfreunde Kitzbühel"}}]},
            {"oefol_id": 8792, "first_name": "David", "last_name": "Kaltenbacher",
             "active_memberships": [{"club": {"name": "HSV OL Wiener Neustadt"}}]},
            {"oefol_id": 2001, "first_name": "Günter", "last_name": "Doppelt",
             "active_memberships": [{"club": {"name": "OLC Graz"}}]},
            {"oefol_id": 2002, "first_name": "Günter", "last_name": "Doppelt",
             "active_memberships": [{"club": {"name": "STOLV"}}]},
        ])
        label = "NF Kitzb./HSV Wr. Neust."
        self.assertEqual(profiles.relay_member_club("Lisa Hauser", label),
                         "Naturfreunde Kitzbühel")
        self.assertEqual(profiles.relay_member_club("David Kaltenbacher", label),
                         "HSV OL Wiener Neustadt")
        self.assertIsNone(profiles.relay_member_club("Unbekannt Person", label))
        self.assertEqual(profiles.relay_member_club(
            "Günter Doppelt", "OLC Graz / OLC Wienerw."), "OLC Graz")
        self.assertTrue(build_db.relay_club_component_matches(
            "NF Kitzbühl", "Naturfreunde Kitzbühel"))
        self.assertTrue(build_db.relay_club_component_matches(
            "OCFF", "OC Fürstenfeld"))
        self.assertTrue(build_db.relay_club_component_matches(
            "AHDO", "ASKÖ Henndorf Orienteering"))

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

    def test_anne_minute_precision_detection_is_document_wide(self):
        rows = [
            {"classification": "classified", "time": 15 + index, "rank": None}
            for index in range(20)
        ]
        self.assertTrue(build_db.anne_results_have_minute_precision(rows))
        self.assertFalse(build_db.anne_results_have_minute_precision(rows[:19]))
        self.assertFalse(build_db.anne_results_have_minute_precision(
            [dict(row, rank=1 if index == 0 else None)
             for index, row in enumerate(rows)]))
        self.assertFalse(build_db.anne_results_have_minute_precision(
            [dict(row, time=301 if index == 0 else row["time"])
             for index, row in enumerate(rows)]))

    def test_complete_exact_attachment_replaces_lossy_anne_stage(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'ÖM Test')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) "
            "VALUES ('anne',1,'anne-api')")
        cur.execute(
            """INSERT INTO result_list
               (id,stage_id,source_document_id,category,parsed_entries,
                parsed_rows,input_fingerprint)
               VALUES ('anne-list',1,'anne','H21',20,20,'x')""")
        for index in range(20):
            build_db.insert_result(
                cur, stage_id=1, result_list_id="anne-list", category="H21",
                status="ok", time_s=(20 + index) * 60, source="anne-api",
                source_document_id="anne", observed_name=f"Runner {index}",
                note="ANNE-Altimport: Zeit nur minutengenau")
        exact_doc = {"categories": [{"results": [
            {"name": f"Runner {index}", "rank": index + 1,
             "timeS": 1200 + index * 17}
            for index in range(20)
        ]}]}
        self.assertTrue(build_db.replace_minute_precision_anne_with_legacy(
            cur, 1, exact_doc))
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM result").fetchone()[0], 0)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM result_list").fetchone()[0], 0)

    def test_minute_precision_anne_list_is_visible_to_review(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'Altimport')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) "
            "VALUES ('anne',1,'anne-api')")
        cur.execute(
            """INSERT INTO result_list
               (id,stage_id,source_document_id,category,parsed_entries,
                parsed_rows,input_fingerprint)
               VALUES ('list',1,'anne','H21',1,1,'x')""")
        build_db.insert_result(
            cur, stage_id=1, result_list_id="list", category="H21",
            status="ok", time_s=1020, source="anne-api",
            source_document_id="anne", observed_name="Max Muster",
            note="ANNE-Altimport: Zeit nur minutengenau")
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT code || ':' || severity FROM audit_issue"
        ).fetchone()[0], "anne_minute_precision:warning")

    def test_nonempty_results_without_a_category_are_visible_to_review(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'Altimport')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) "
            "VALUES ('anne',1,'anne-api')")
        cur.execute(
            """INSERT INTO result_list
               (id,stage_id,source_document_id,category,parsed_entries,
                parsed_rows,input_fingerprint)
               VALUES ('list',1,'anne','empty',1,1,'x')""")
        build_db.insert_result(
            cur, stage_id=1, result_list_id="list", category="empty",
            status="ok", time_s=1200, source="anne-api",
            source_document_id="anne", observed_name="Max Muster")
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT code || ':' || severity FROM audit_issue"
        ).fetchone()[0], "anne_missing_category:warning")

    def test_known_anne_category_omission_is_a_source_warning(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (3438, '1. KOLV Cup')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 3438, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) VALUES ('doc',3438,'anne-api')")
        cur.execute(
            """INSERT INTO result_list
               (id,stage_id,source_document_id,category,parsed_entries,
                parsed_rows,input_fingerprint)
               VALUES ('list',1,'doc','empty',1,1,'x')""")
        build_db.insert_result(
            cur, stage_id=1, result_list_id="list", category="empty",
            status="ok", time_s=1200, source="anne-api",
            source_document_id="doc", observed_name="Max Muster")
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT code || ':' || severity FROM audit_issue"
        ).fetchone()[0], "source_category_missing:warning")

    def test_confirmed_source_rank_inversion_is_not_a_parser_finding(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1734, 'Süd-Ost-Cup')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1734, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) VALUES ('doc',1734,'sportsoftware-pdf')")
        list_id = build_db.register_result_list(cur, 1, "doc", "B", "B", 2, [
            {"name": "Anna", "rank": 21, "timeS": 200},
            {"name": "Berta", "rank": 22, "timeS": 190},
        ])
        for name, rank, seconds in (("Anna", 21, 200), ("Berta", 22, 190)):
            build_db.insert_result(
                cur, stage_id=1, result_list_id=list_id, category="B",
                rank=rank, status="ok", time_s=seconds,
                source="sportsoftware-pdf", observed_name=name)
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT code || ':' || severity FROM audit_issue"
        ).fetchone()[0], "source_rank_anomaly:warning")

    def test_unreadable_source_value_is_not_reported_as_parser_failure(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (5204, 'Staffel')")
        cur.execute(
            "INSERT INTO stage (id, event_id, number) VALUES (1, 5204, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) "
            "VALUES ('pdf',5204,'sportsoftware-pdf')")
        cur.execute(
            """INSERT INTO result_list
               (id,stage_id,source_document_id,category,parsed_entries,
                parsed_rows,input_fingerprint)
               VALUES ('list',1,'pdf','Mixed Staffel bis 17',1,1,'x')""")
        build_db.insert_result(
            cur, stage_id=1, result_list_id="list",
            category="Mixed Staffel bis 17", status="ok", rank=2,
            result_kind="relay", source="sportsoftware-pdf",
            source_document_id="pdf", observed_name="Pia Aspalter",
            observed_time="er 11")
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT code || ':' || severity FROM audit_issue"
        ).fetchone()[0], "source_value_unreadable:warning")

    def test_clean_stale_confirmation_falls_back_to_automatic_checks(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        self.assertFalse(build_db.stale_verification_requires_review(
            cur, "list", "confirmed"))
        self.assertTrue(build_db.stale_verification_requires_review(
            cur, "list", "flagged"))
        build_db.add_audit_issue(
            cur, "list", "provisional_championship_identity", "warning",
            "Identität später prüfen")
        self.assertFalse(build_db.stale_verification_requires_review(
            cur, "list", "confirmed"))
        build_db.add_audit_issue(
            cur, "list", "source_problem", "warning", "Quelle prüfen")
        self.assertTrue(build_db.stale_verification_requires_review(
            cur, "list", "confirmed"))

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

    def test_team_member_times_do_not_raise_rank_time_inversion(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'Schulteam')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) "
            "VALUES ('doc',1,'sportsoftware-pdf')")
        rows = [
            {"name": "Anna Team", "rank": 1, "timeS": 1800,
             "resultKind": "team"},
            {"name": "Berta Team", "rank": 2, "timeS": 1200,
             "resultKind": "team"},
        ]
        list_id = build_db.register_result_list(
            cur, 1, "doc", "Damen", "Damen", 2, rows)
        for row in rows:
            build_db.insert_result(
                cur, stage_id=1, result_list_id=list_id, category="Damen",
                rank=row["rank"], status="ok", time_s=row["timeS"],
                result_kind="team", source="sportsoftware-pdf",
                observed_name=row["name"])
        build_db.populate_quality_model(cur)
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

        for list_id in (score, meos_score, series):
            for rank, elapsed in ((1, 120), (2, 100)):
                build_db.insert_result(
                    cur, stage_id=1, result_list_id=list_id, category="Source",
                    rank=rank, status="ok", time_s=elapsed,
                    source="anne-api", observed_name=f"Runner {rank}")
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT count(*) FROM audit_issue WHERE code='rank_time_inversion'"
        ).fetchone()[0], 0)

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

    def test_registered_but_unlisted_starts_are_a_source_warning(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        cur = con.cursor()
        cur.execute("INSERT INTO event (id, title) VALUES (1, 'Quell-Auslassung')")
        cur.execute("INSERT INTO stage (id, event_id, number) VALUES (1, 1, 1)")
        cur.execute(
            "INSERT INTO source_document (id,event_id,source_type) "
            "VALUES ('doc',1,'sportsoftware-pdf')")
        rows = [
            {"name": "Anna A", "rank": 1, "timeS": 100, "status": "ok"},
            {"name": "Berta B", "rank": 2, "timeS": 110, "status": "ok"},
        ]
        list_id = build_db.register_result_list(
            cur, 1, "doc", "Damen", "Damen", 3, rows)
        for row in rows:
            build_db.insert_result(
                cur, stage_id=1, result_list_id=list_id, category="Damen",
                rank=row["rank"], status="ok", time_s=row["timeS"],
                source="sportsoftware-pdf", observed_name=row["name"])
        build_db.populate_quality_model(cur)
        self.assertEqual(cur.execute(
            "SELECT code || ':' || severity FROM audit_issue"
        ).fetchone()[0], "source_declared_omission:warning")

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

    def test_unique_name_club_can_override_wrong_result_year(self):
        profiles = build_db.AnneProfileIndex([{
            "oefol_id": 1661,
            "first_name": "Claudia",
            "last_name": "Bonek",
            "year_of_birth": 1969,
            "active_memberships": [{"club": {"name": "Naturfreunde Wien"}}],
        }])
        persons = build_db.PersonRegistry(profiles)
        pid, basis, confidence, state = persons.from_legacy(
            "Claudia Bonek", 1955, "Naturfreunde Wien")
        self.assertEqual((pid, basis, confidence, state),
                         (1661, "anne-registry-name-club", 0.95, "resolved"))

    def test_identity_match_uses_curated_club_canonicalization(self):
        profiles = build_db.AnneProfileIndex([{
            "oefol_id": 2419,
            "first_name": "Thomas",
            "last_name": "Rothauer",
            "active_memberships": [{
                "club": {"name": "ASKÖ Henndorf Orienteering"}}],
        }])
        persons = build_db.PersonRegistry(profiles)
        self.assertEqual(
            persons.from_legacy("Thomas Rothauer", None, "ASKÖ Henndorf"),
            (2419, "anne-registry-name-club", 0.95, "resolved"),
        )
        self.assertEqual(
            build_db.canonicalize_official_club(
                "NF Wien Naturfreunde Wien", build_db.OFFICIAL_CLUBS),
            "Naturfreunde Wien",
        )

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

    def test_real_birth_year_profile_wins_over_anne_1901_placeholder(self):
        profiles = build_db.AnneProfileIndex([
            {"oefol_id": 8137, "first_name": "Herwig", "last_name": "Hierzegger",
             "year_of_birth": 1901, "anne_is_verified": False},
            {"oefol_id": 275, "first_name": "Herwig", "last_name": "Hierzegger",
             "year_of_birth": 1941, "anne_is_verified": True},
        ])
        persons = build_db.PersonRegistry(profiles)
        for profile in profiles.by_id.values():
            persons.from_anne(profile["oefol_id"], profile["name"],
                              profile["year_of_birth"], profile["nationality"])

        merges = build_db.duplicate_identity_merge_edges(persons)

        self.assertIsNone(profiles.by_id[8137]["year_of_birth"])
        self.assertEqual(merges, {8137: 275})

    def test_legacy_placeholder_birth_year_is_unknown(self):
        persons = build_db.PersonRegistry()

        pid, _basis, _confidence, _state = persons.from_legacy(
            "Karin Fritz", 1901, "Naturfreunde Wien")

        self.assertIsNone(persons.by_id[pid][2])

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

    def test_crossed_external_anne_id_repairs_only_the_matching_member_row(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        profiles = build_db.AnneProfileIndex([
            {"oefol_id": 1285, "first_name": "Thomas", "last_name": "Hnilica",
             "year_of_birth": 1968,
             "active_memberships": [{"club": {"name": "OLT Transdanubien"},
                                      "sport_type": "footOrienteering"}]},
            {"oefol_id": 1711, "first_name": "Thomas", "last_name": "Hlosta",
             "year_of_birth": 1967,
             "active_memberships": [{"club": {"name": "Naturfreunde Wien"},
                                      "sport_type": "footOrienteering"}]},
        ])
        persons = build_db.PersonRegistry(profiles)
        hnilica = persons.from_anne(1285, "Thomas Hnilica", 1968, "AUT")
        persons.record(hnilica, "Thomas Hnilica", authoritative=True)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=hnilica, category="H40", status="ok",
            source="anne-api", observed_name="Thomas Hnilica",
            observed_club="OLT Transdanubien", official_club="OLT Transdanubien",
            observed_user_id="1285", identity_basis="source-oefol-id",
            identity_confidence=1.0,
        )
        # Historic ANNE row: the name and club are Hlosta's, but the source
        # accidentally stamped Hnilica's otherwise-valid ÖFOL-ID onto it.
        persons.record(hnilica, "Thomas Hlosta", authoritative=True)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=hnilica, category="H40", status="ok",
            source="anne-api", observed_name="Thomas Hlosta",
            observed_club="NF Wien", official_club="Naturfreunde Wien",
            observed_user_id="1285", identity_basis="source-oefol-id",
            identity_confidence=1.0,
        )
        persons.from_anne(1711, "Thomas Hlosta", 1967, "AUT")
        members = [{
            "ofol_id": 1711, "name": "Thomas Hlosta",
            "name_key": build_db.name_key("Thomas Hlosta"), "yob": 1967,
            "club": "Naturfreunde Wien",
        }]

        corrections = build_db.prepare_verified_member_identities(
            con.cursor(), persons, members)

        self.assertEqual(
            con.execute("SELECT observed_name, person_id FROM result ORDER BY id").fetchall(),
            [("Thomas Hnilica", 1285), ("Thomas Hlosta", 1711)],
        )
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["assigned_person_id"], 1711)

    def test_disjoint_crossed_id_without_registry_target_becomes_fallback(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        profiles = build_db.AnneProfileIndex([{
            "oefol_id": 1487, "first_name": "Joachim", "last_name": "Friessnig",
            "year_of_birth": 1958,
        }])
        persons = build_db.PersonRegistry(profiles)
        source_id = persons.from_anne(1487, "Joachim Friessnig", 1958, "AUT")
        persons.record(source_id, "Thomas Krejci", authoritative=True)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=source_id, category="H21", status="ok",
            source="anne-api", observed_name="Thomas Krejci",
            observed_club="TV Fürstenfeld", official_club="OC Fürstenfeld",
            observed_user_id="1487", identity_basis="source-oefol-id",
            identity_confidence=1.0,
        )

        corrections = build_db.prepare_verified_member_identities(
            con.cursor(), persons, [])
        row = con.execute(
            "SELECT person_id, identity_basis, identity_state FROM result").fetchone()

        self.assertLess(row[0], 0)
        self.assertEqual(row[1:], ("legacy-name", "candidate"))
        self.assertEqual(corrections[0]["observed_user_id"], 1487)

    def test_crossed_source_id_reconciliation_never_assigns_family_rows(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        profiles = build_db.AnneProfileIndex([{
            "oefol_id": 3887, "first_name": "Nicole", "last_name": "Winkler",
            "year_of_birth": 1990,
        }])
        persons = build_db.PersonRegistry(profiles)
        build_db.insert_result(
            con.cursor(), stage_id=1, person_id=None, category="Family", status="ok",
            result_kind="family", source="anne-api", observed_name="Nicole Winkler",
            observed_user_id="3887", identity_basis="not-applicable-family",
            identity_confidence=1.0,
        )

        corrections = build_db.prepare_verified_member_identities(
            con.cursor(), persons, [])

        self.assertEqual(corrections, [])
        self.assertIsNone(con.execute("SELECT person_id FROM result").fetchone()[0])

    def test_incompatible_registry_ids_on_one_person_are_rejected(self):
        con = sqlite3.connect(":memory:")
        con.executescript(build_db.SCHEMA)
        con.execute("INSERT INTO person VALUES (?,?,?,?,?)",
                    (1711, "Thomas Hlosta", "hlosta thomas", 1967, "AUT"))
        for identifier in (1285, 1711):
            con.execute("INSERT INTO person_identifier VALUES (?,?,?,?,?,?)",
                        ("oefol_id", str(identifier), 1711, "authoritative",
                         "anne-user-registry", None))
        profiles = build_db.AnneProfileIndex([
            {"oefol_id": 1285, "first_name": "Thomas", "last_name": "Hnilica",
             "year_of_birth": 1968},
            {"oefol_id": 1711, "first_name": "Thomas", "last_name": "Hlosta",
             "year_of_birth": 1967},
        ])

        conflicts = build_db.registry_identifier_merge_conflicts(
            con.cursor(), profiles)

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["identifiers"], [1285, 1711])


if __name__ == "__main__":
    unittest.main()
