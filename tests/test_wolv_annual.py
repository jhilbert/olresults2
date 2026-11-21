import importlib.util
import json
import sqlite3
import tempfile
import re
import hashlib
import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("wolv_annual", ROOT / "build" / "extract_wolv_annual.py")
wolv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wolv)


class WolvAnnualTests(unittest.TestCase):
    def test_2013_night_joint_second_is_two_individual_medal_rows(self):
        import sys
        sys.path.insert(0, str(ROOT / 'build'))
        try:
            from build_wolv_pilot import parse_cell
            cell={'page':1,'row':3,'column':4,'row_heading':'H 15-16',
                  'discipline_heading':'NACHT - OL','event_heading':'12.10.2013',
                  'cell':'H 15-18:\n1) KURZ Florian NF Wien\n2) KASTNER Nicolas NF Wien +\nWOLFRAM Clemens NF Wien\n(7)'}
            parsed=parse_cell(cell,2013)
        finally:
            sys.path.pop(0)
        self.assertEqual(parsed['source_text'],cell['cell'])
        self.assertEqual(parsed['medals'],[{'rank':1,'text':'KURZ Florian NF Wien'},
            {'rank':2,'text':'KASTNER Nicolas NF Wien'},{'rank':2,'text':'WOLFRAM Clemens NF Wien'}])

    def test_2019_sprint_h60_and_age_reconstruction_evidence(self):
        import pdfplumber
        invitation=ROOT / 'data/review/archived_sources/2019/event-2533-invitation.pdf'
        self.assertEqual(hashlib.sha256(invitation.read_bytes()).hexdigest(),
                         '2dee416a32473e770bd7f9e8f44fd9627642824b542de406ae1c4aeea559658c')
        with pdfplumber.open(invitation) as pdf:
            self.assertIn('H 60-',pdf.pages[1].extract_text())
        entries=(ROOT / 'data/review/archived_sources/2019/event-2533-entries.html').read_text()
        self.assertIn('Herren 60- W',entries)
        self.assertIn('Zapletal Josef',entries)
        config=json.loads((ROOT / 'data/review/regional_source_classes.json').read_text())['events']
        self.assertEqual(config['2533']['jurisdictions']['WIEN']['categories']['H 55-'],'H60-')
        source=json.loads((ROOT / 'data/normalized/2632-1.json').read_text())['categories']
        splits=config['2632']['jurisdictions']['WIEN']['categories']
        for category,definition in splits.items():
            rows=next(c['results'] for c in source if c['name']==category)
            self.assertEqual(sorted(r['name'] for r in rows),sorted(n for names in definition.values() for n in names))
            for key,names in definition.items():
                lower=int(re.search(r'\d+',key).group())
                for name in names:
                    row=next(r for r in rows if r['name']==name)
                    age=2019-row['yearOfBirth']
                    self.assertTrue(lower <= age and (age < lower+5 or lower==70),(key,name,age))

    def test_2019_wiener_langdistanz_evidence_matches_imported_19_classes(self):
        import sys
        import pdfplumber
        sys.path.insert(0, str(ROOT / 'ingest'))
        try:
            from parse_sportsoftware_html import decode, parse_document
            invitation = ROOT / 'data/review/archived_sources/2019/event-2764-invitation.pdf'
            results = ROOT / 'data/review/archived_sources/2019/event-2764-vienna.html'
            self.assertEqual(hashlib.sha256(invitation.read_bytes()).hexdigest(),
                             'a034b5e9ad97aa21841576359f1889eec7a8adf7889469ced6d6cfa5b9fa2bdc')
            self.assertEqual(hashlib.sha256(results.read_bytes()).hexdigest(),
                             '4aa23e461cf2016b6e12d1519ac8b4f90421d06f3f10139df9da5c14bae01aa1')
            with pdfplumber.open(invitation) as pdf:
                self.assertIn('Wr.Landesmeisterschaft: D/H 19-',pdf.pages[1].extract_text())
            vienna=parse_document(decode(results.read_bytes()))
            imported=json.loads((ROOT / 'data/normalized/2764-2.json').read_text())['categories']
            def signature(row):
                name=unicodedata.normalize('NFKD',row['name'].casefold())
                return (' '.join(sorted(re.findall(r'\w+',name))),row.get('timeS'))
            for category in ('Damen 19-', 'Herren 19-'):
                original=next(c for c in vienna if c['name']=='Wr.Landesmeisterschaft '+category)
                known=next(c for c in imported if c['name']==category)
                self.assertEqual(sorted(map(signature,original['results'])),
                                 sorted(map(signature,known['results'])))
        finally:
            sys.path.pop(0)

    def test_full_archive_preserves_all_years_ties_and_shared_non_award_notice(self):
        import sys
        sys.path.insert(0, str(ROOT / "build"))
        try:
            from build_wolv_pilot import build_archive, write_database
            from extract_wolv_history import extract, read_tables
            archive = build_archive()
            self.assertEqual([x["year"] for x in archive["years"]], list(range(1994, 2027)))
            years = {x["year"]: x for x in archive["years"]}
            corrected = next(e for e in years[2025]['entries']
                             if (e['page'], e['row'], e['column']) == (1, 19, 1))
            self.assertEqual(corrected['source_category'], 'D 13-14')
            self.assertEqual(corrected['source_text'], 'nicht ausgetragen')
            self.assertEqual(corrected['medals'], [])
            def h40(year):
                return next(e for e in years[year]['entries'] if e['source_category']=='H 40-'
                            and e['discipline'].startswith('LANGDISTANZ'))
            self.assertEqual(h40(2018)['medals'],h40(2019)['medals'])
            cells2019 = {(e['row'],e['column']):e for e in years[2019]['entries'] if e['page'] == 1}
            for key, winner in [((10,1),'KROUPA Günther'),((10,4),'KROUPA Günther'),
                                ((14,1),'DYTLEWSKI Nick'),((14,4),'GRÜN Paul'),
                                ((20,1),'GASSNER Jasmina')]:
                entry = cells2019[key]
                self.assertEqual(entry['medals'][0]['rank'],1)
                self.assertIn(winner,entry['medals'][0]['text'])
                self.assertRegex(entry['source_text'],r'^\(1\d+\)\)')
                self.assertEqual(entry['source_heading'],'')
            self.assertEqual(cells2019[(17,1)]['source_heading'],'')
            self.assertEqual(cells2019[(25,2)]['source_heading'],'D 55-:')
            self.assertTrue(any(e["discipline"].startswith("STAFFEL") and e["page"] == 2
                                for e in years[2017]["entries"]))
            mixed = [e for e in years[2020]["entries"] if "MIXED" in e["discipline"]]
            self.assertEqual(len(mixed), 4)
            self.assertTrue(all("COVID" in e["shared_notice"] and not e["medals"] for e in mixed))
            ties = [e for e in years[2016]["entries"] if [m["rank"] for m in e["medals"]] == [1,2,2]]
            self.assertTrue(ties)
            pattern = re.compile(r"(?m)^\s*[123]\s*\)")
            for year in (*range(1994, 2012), 2014):
                path = ROOT / f"data/review/archived_sources/{year}/wolv-annual-sheet.html"
                grid = read_tables(path)[0]
                raw = sum(len(pattern.findall(cell or "")) for row in grid[2:]
                          if row and row[0] for cell in row[1:])
                self.assertEqual(raw, sum(len(pattern.findall(e["cell"])) for e in extract(year,path)), year)
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "wolv.db"
                write_database(archive, db)
                with sqlite3.connect(db) as con:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM source").fetchone()[0], 33)
                    self.assertGreater(con.execute("SELECT COUNT(*) FROM medal").fetchone()[0], 4000)
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            sys.path.pop(0)

    def test_html_visual_cell_continuation_does_not_merge_unrelated_classes(self):
        import sys
        sys.path.insert(0, str(ROOT / "build"))
        try:
            from extract_wolv_history import continued_cell
            self.assertTrue(continued_cell("H 15-:", "1) Example"))
            self.assertTrue(continued_cell("H 15-:\n1) A", "2) B"))
            self.assertTrue(continued_cell("H 15-:\n1) A\n2) B", "3) C"))
            self.assertTrue(continued_cell("H 15-:\n1) A\n2) B\n3) C", "(9)"))
            self.assertFalse(continued_cell("H 15-:\n1) A\n2) B\n3) C", "nicht ausgetragen"))
        finally:
            sys.path.pop(0)

    def test_extracts_complete_wrapped_relay_winner_not_other_places(self):
        result = wolv.extract_cell("Senioren 35-:\n1) Club: ERSTE Anna -\n- ZWEITE Max\n2) Andere")
        self.assertEqual(result["first_place_text"], "Club: ERSTE Anna - - ZWEITE Max")

    def test_no_winner_in_explanatory_count(self):
        self.assertIsNone(wolv.extract_cell("keine Wertung\n(1)")["first_place_text"])

    def test_snapshot_preserves_official_no_award_reason(self):
        data = json.loads((ROOT / "data" / "review" / "wolv_annual_medal_tables_2024_2026.json").read_text())
        self.assertEqual([d["year"] for d in data["documents"]], [2024, 2025, 2026])
        doc = data["documents"][2]
        cells = [c for c in doc["cells"] if c["page"] == 1 and c["column"] == 4
                 and c["row_heading"] == "D 19-"]
        self.assertEqual(len(cells), 1)
        self.assertIn("nur 3 Läuferinnen aus einem Verein", cells[0]["cell"])
        self.assertIsNone(cells[0]["first_place_text"])
        self.assertEqual(doc["extraction_status"], "unmatched_review_only")

    def test_archived_2026_pdf_reproduces_all_cells_and_comments(self):
        pdf = ROOT / "data/review/archived_sources/2026/wolv-annual.pdf"
        original = wolv.extract_document(2026, pdf)
        snapshot = json.loads((ROOT / "data/review/wolv_annual_medal_tables_2024_2026.json").read_text())
        self.assertEqual(original, snapshot["documents"][2])

    def test_pilot_retains_all_cells_medals_and_non_award_reasons(self):
        import sys
        sys.path.insert(0, str(ROOT / "build"))
        try:
            from build_wolv_pilot import build, write_database
            data = build()
        finally:
            sys.path.pop(0)
        self.assertEqual(len(data["entries"]), 73)
        self.assertEqual(sum(len(c["medals"]) for c in data["entries"]), 141)
        reasons = [c for c in data["entries"] if "nur 3 Läuferinnen aus einem Verein" in c["source_text"]]
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0]["medals"], [])
        self.assertTrue(any("zusammen mit H -12" in c["source_text"] for c in data["entries"]))
        self.assertTrue(any("nicht ausgetragen" in c["source_text"] for c in data["entries"]))
        corrected = next(c for c in data["entries"] if (c["page"], c["row"], c["column"]) == (1, 25, 2))
        self.assertIn("K A STNER", corrected["source_text"])
        self.assertIn("KASTNER Barbara", corrected["medals"][0]["text"])
        cells = {(c["page"], c["row"], c["column"]): c for c in data["entries"]}
        self.assertIn("IMRISKA Albert", cells[(1, 3, 2)]["medals"][0]["text"])
        self.assertIn("HINTERPLATTNER Oskar", cells[(1, 4, 1)]["medals"][1]["text"])
        self.assertEqual([m["rank"] for m in cells[(1, 13, 3)]["medals"]], [1, 2, 3, 3])
        self.assertIn("GÖSCHL Erich", cells[(1, 13, 3)]["medals"][3]["text"])
        self.assertIn("URBANEK Annina", cells[(1, 20, 5)]["medals"][0]["text"])
        self.assertIn("KOVACS Margit", cells[(1, 27, 4)]["medals"][2]["text"])
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "wolv.db"
            write_database(data, database)
            with sqlite3.connect(database) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM classification").fetchone()[0], 73)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM medal").fetchone()[0], 141)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM classification WHERE source_text LIKE '%nur 3 Läuferinnen aus einem Verein%'").fetchone()[0], 1)
