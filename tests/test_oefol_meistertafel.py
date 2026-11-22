import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ingest'))
import structure_oefol_meistertafel as source


class MeistertafelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = source.structure(source.SNAPSHOT.read_bytes())
        cls.by_id = {a['id']:a for a in cls.data['awards']}

    def test_every_nonempty_source_row_is_accounted_for(self):
        rows = self.data['source_rows']
        self.assertEqual(len(rows), 7964)
        self.assertEqual(len({r['id'] for r in rows}), len(rows))
        self.assertFalse([r for r in rows if r['kind'] == 'unclassified'])
        self.assertEqual({r['year'] for r in rows}, set(range(1966,2026)))
        self.assertTrue(all(a['title_type'] in ('ÖM','ÖSTM') for a in self.data['awards']))
        self.assertTrue(all(a['members'] for a in self.data['awards']))

    def test_2014_malformed_rowspan_does_not_eat_next_title(self):
        a = self.by_id['tablepress-18:row-149']
        self.assertEqual((a['discipline'],a['format'],a['category'],a['title_type'],a['club']),
                         ('OL','Mannschaft','bis 18','ÖM','NF Wien'))
        self.assertEqual([m['name'] for m in a['members']], ['Florian KURZ','Dominik JANDL','Clemens WOLFRAM'])

    def test_teammates_ties_and_historical_tabular_rows(self):
        a = self.by_id['tablepress-70:row-103']
        self.assertEqual((a['year'],a['format'],a['title_type'],a['gender']), (2025,'Staffel','ÖSTM','female'))
        self.assertEqual([m['name'] for m in a['members']], ['Lina Buchberger','Tina Tiefenböck','Jasmina Gassner'])
        a = self.by_id['tablepress-19:row-11']
        self.assertEqual([m['name'] for m in a['members']], ['Denise HLOSTA','Martina WALCH'])
        self.assertEqual(a['members'][1]['club'], 'SU Schöckl Orienteering')
        a = self.by_id['tablepress-34:row-80']
        self.assertEqual(len(a['members']),3)
        self.assertEqual((a['title_type'],a['category'],a['club']),('ÖSTM','D 19','TVN Wien'))

    def test_team_names_in_single_cells_are_separated_without_guessing(self):
        self.assertEqual(source.member_names('Christine Calvet / Barbara Kastner / Marina Skern', 'Mannschaft'),
                         ['Christine Calvet', 'Barbara Kastner', 'Marina Skern'])
        self.assertEqual(source.member_names('Barbara Kastner Claudia Bonek Marina Skern', 'Mannschaft'),
                         ['Barbara Kastner', 'Claudia Bonek', 'Marina Skern'])
        self.assertEqual(source.member_names('Helena AUS DER SCHMITTEN', 'Staffel'), ['Helena AUS DER SCHMITTEN'])
        self.assertEqual(source.member_names('Rose Marie Reindl', 'Staffel'), ['Rose Marie Reindl'])
        self.assertEqual(source.member_names('Michael Tobler. Michael Stockmayer, Manfred Stockmayer', 'Ski-O Staffel'),
                         ['Michael Tobler', 'Michael Stockmayer', 'Manfred Stockmayer'])
        a = self.by_id['tablepress-10:row-106']
        self.assertEqual([m['name'] for m in a['members']], ['Bernd Bäuchler', 'Pierre Kaltenbacher', 'Markus Plohn'])
        self.assertEqual(a['club'], 'HSV OL Wr. Neustadt')
        self.assertEqual(a['printed_names'], 'Bernd Bäuchler, Pierre Kaltenbacher Markus Plohn HSV')
        a = self.by_id['tablepress-7:row-131']
        self.assertEqual([m['name'] for m in a['members']], ['Lilli Egger', 'Romy Anker'])
        self.assertEqual(a['club'], 'Orienteering Innsbruck Imst')
        self.assertIn('club_repeated_in_name_cell', a['notes'])

    def test_no_other_team_member_has_four_or_more_name_parts(self):
        long_names = [(a['id'], m['name']) for a in self.data['awards'] if source.team_format(a['format'])
                      for m in a['members'] if len(m['name'].split()) >= 4]
        self.assertEqual(long_names, [('tablepress-12:row-140', 'Helena AUS DER SCHMITTEN')])

    def test_missing_and_disputed_values_are_not_invented(self):
        a = self.by_id['tablepress-34:row-2']
        self.assertIsNone(a['club'])
        self.assertIn('name_or_club_ambiguous',a['notes'])
        self.assertFalse(any('nicht ausgetragen' in m['name'].lower() for a in self.data['awards'] for m in a['members']))
        self.assertTrue(any(any(note.startswith('section_year_conflict:') for note in a['notes']) for a in self.data['awards']))

    def test_older_state_champion_names_are_not_categories(self):
        for row, name in [('tablepress-29:row-120', 'Elisabeth Hohenwarter'),
                          ('tablepress-26:row-182', 'Michaela GIGON'),
                          ('tablepress-32:row-77', 'Andreas Pölzl')]:
            award = self.by_id[row]
            self.assertIsNone(award['category'])
            self.assertEqual(award['members'][0]['name'], name)
        self.assertFalse([a for a in self.data['awards'] if a['category'] in
                          ('Elisabeth','Michaela','Christian','Sonja','Johann','Thomas','Agota','Andreas')])

    def test_generated_data_is_reproducible(self):
        expected=json.dumps(self.data,ensure_ascii=False,separators=(',',':'))+'\n'
        self.assertEqual(source.OUT.read_text(),expected)

    def test_official_clubs_use_shared_registry(self):
        clubs = {c['code']: c['name'] for c in self.data['official_clubs']}
        self.assertEqual(clubs['GO_'], 'GO_Harzberg/Bad_Voeslau')
        self.assertEqual(clubs['HSVOLWN'], 'HSV OL Wiener Neustadt')

    def test_confirmed_tiefenboeck_rows_keep_source_names(self):
        overlay=json.loads((ROOT / 'site/oefol/person-display.json').read_text())
        confirmed={c['source_row']:c for c in overlay['confirmations']}
        self.assertEqual(confirmed['tablepress-17:row-142']['oefol_id'],3165)
        self.assertEqual(confirmed['tablepress-17:row-148']['oefol_id'],2710)
        self.assertEqual(self.by_id['tablepress-17:row-140']['members'][2]['name'],'TIEFENBÖCK')
        self.assertEqual(self.by_id['tablepress-17:row-146']['members'][2]['name'],'TIEFENBÖCK')

    def test_multiple_resolutions_from_same_source_row(self):
        overlay=json.loads((ROOT / 'site/oefol/person-display.json').read_text())
        confirmed={(c['source_row'],c['source_name']):c for c in overlay['confirmations']}
        self.assertEqual(confirmed[('tablepress-37:row-83','F.Kölbl')]['oefol_id'],890)
        self.assertEqual(confirmed[('tablepress-37:row-83','P.Hartinger')]['oefol_id'],870)
        self.assertEqual(confirmed[('tablepress-9:row-154','David')]['oefol_id'],8792)

    def test_reinisch_2010_relay_and_2006_club_keep_provenance(self):
        overlay = json.loads((ROOT / 'site/oefol/person-display.json').read_text())
        confirmed = {(c['source_row'], c['source_name']): c for c in overlay['confirmations']}
        c = confirmed[('tablepress-22:row-107', 'Theresia REINISCH')]
        self.assertEqual((c['first_name'], c['oefol_id']), ('Theresa', 2354))
        self.assertIn('Magazin2010-04', c['source_url'])
        a = self.by_id['tablepress-26:row-186']
        self.assertEqual((a['source_club'], a['club']), ('NF V,', 'NF Villach'))
        self.assertEqual(a['members'][0]['name'], 'Helena Reinisch')

    def test_stockmayer_typo_keeps_original_and_uses_confirmed_id(self):
        a = self.by_id['tablepress-8:row-176']
        self.assertEqual(a['members'][0]['name'], 'Micheal Stockmayer')
        overlay = json.loads((ROOT / 'site/oefol/person-display.json').read_text())
        c = next(c for c in overlay['confirmations'] if c['source_row'] == a['id'])
        self.assertEqual((c['first_name'], c['last_name'], c['oefol_id']), ('Michael', 'Stockmayer', 816))

    def test_season_person_crosscheck_is_scoped_to_specific_source(self):
        a = self.by_id['tablepress-15:row-278']
        self.assertEqual((a['source_club'], a['club'], a['members'][0]['name']),
                         ('Kitzbühel', 'NF Kitzbühel', 'Lisa KRIMBACHER'))
        a = self.by_id['tablepress-26:row-80']
        self.assertEqual((a['source_club'], a['club'], a['members'][0]['name']),
                         ('NF Villach', 'NF Wien', 'Vera Arbter'))

    def test_ex_aequo_second_team_keeps_own_club(self):
        a = self.by_id['tablepress-8:row-106']
        members = {m['name']:m.get('club') or a['club'] for m in a['members']}
        self.assertEqual(members['Matilda Buschek'], 'Naturfreunde Wien')
        self.assertEqual(members['Emma Frey'], 'OL Kufstein')
        self.assertEqual(members['Maya Eichmüller'], 'OL Kufstein')

    def test_official_2015_team_rosters_cover_all_source_rows(self):
        overlay=json.loads((ROOT / 'site/oefol/person-display.json').read_text())
        byrow={c['source_row']:c for c in overlay['confirmations']}
        for row in range(140,173):
            self.assertIn(f'tablepress-17:row-{row}', byrow)
        self.assertEqual(byrow['tablepress-17:row-140']['first_name'],'Maya')
        self.assertEqual(byrow['tablepress-17:row-141']['first_name'],'Magdalena')
        self.assertEqual({byrow[f'tablepress-17:row-{i}']['first_name'] for i in (149,151)}, {'Jannis','Erik'})
        self.assertEqual({byrow[f'tablepress-17:row-{i}']['first_name'] for i in (152,153)}, {'Anja','Vera'})


if __name__ == '__main__': unittest.main()
