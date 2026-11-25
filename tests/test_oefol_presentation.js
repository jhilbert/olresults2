const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Exercise the actual presentation functions without starting the page or fetching data.
const script = fs.readFileSync(path.join(__dirname, '../site/oefol/meistertafel.js'), 'utf8');
const prefix = script.slice(0, script.indexOf('  const node ='))
  .replace('  setupDataNotice();', '');
const api = vm.runInNewContext(prefix + '\nreturn {configureClubs, clubLabel, formatLabel, categoryLabel, formerNames, hasUnresolvedYearConflict};\n})()');
const data = require('../site/oefol/meistertafel.json');
api.configureClubs(data.official_clubs);
for (const award of data.awards.filter(a => a.year === 1966)) assert.equal(api.formatLabel(award), 'OL');
assert.equal(api.formatLabel({year:1965, discipline:'OL', format:null}), '');
assert.equal(api.formatLabel({year:2026, discipline:'MTBO', format:'Massenstart'}), 'MTBO Massenstart');
assert.equal(api.formatLabel({year:2026, discipline:'Ski-O', format:'Verfolgung'}), 'Ski-O Verfolgung');
assert.equal(api.categoryLabel({gender:'female',category:'D12'}), 'D -12');
assert.equal(api.categoryLabel({gender:'female',category:'D15-18'}), 'D 15-18');
assert.equal(api.categoryLabel({gender:'male',category:'U17'}), 'H -17');
assert.equal(api.categoryLabel({gender:'female',category:'über 45'}), 'D 45-');
assert.equal(api.clubLabel('HSV Salzburg', 1995), 'HSV Wals');
assert.equal(api.clubLabel('Union Klagenfurt', 1993), 'SU Klagenfurt');
assert.equal(api.clubLabel('HSV Klagenfurt', 1993), 'HSV Klagenfurt');
assert.equal(api.clubLabel('OLT Klagenfurt', 1999), 'OLT Klagenfurt');
assert.equal(api.formerNames.get('HSV Wals')[0], 'HSV Salzburg');
const conflicts = data.awards.filter(a => a.notes.some(n => n.startsWith('section_year_conflict:')));
assert.equal(conflicts.length, 28);
assert.ok(conflicts.every(a => !api.hasUnresolvedYearConflict(a)));
assert.equal(api.hasUnresolvedYearConflict({id:'other',year:2023,discipline:'Ski-O',notes:['section_year_conflict:Ski-O 2022']}), true);
assert.equal(api.hasUnresolvedYearConflict({...conflicts[0],notes:['section_year_conflict:Ski-O 2021']}), true);
console.log('Meistertafel presentation: OK');
