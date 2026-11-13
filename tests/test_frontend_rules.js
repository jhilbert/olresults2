"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  seasonYear,
  personIdString,
  compareResultCategories,
  regionalCategoryLabel,
  youthTitleCategory,
  youthTitleGrant,
  clubMedalCounts,
  annualClubGoldSeries,
} = require("../site/domain_rules.js");

test("annual gold history counts winning teams once and does not invent older coverage", () => {
  const national = {event_id:1,stage_id:1,result_list_id:'r',category:'H21',championship:'ÖSTM',
    result_kind:'relay',team_number:'7',national_rank:1,club_name:'A',date:'2024-12-05',sport_type:'skiOrienteering'};
  const regional = {event_id:2,stage_id:2,result_list_id:'v',category:'H14',championship:'Wiener MS',
    result_kind:'pair',regional_entry_id:'winning-team',national_rank:1,club_name:'A',date:'2026-05-01',sport_type:'footOrienteering'};
  const rows = annualClubGoldSeries([{...national,person_id:'1'}, {...national,person_id:'2'},
    {...national,event_id:3,stage_id:3,result_list_id:'silver',national_rank:2,date:'2026-05-01',sport_type:'footOrienteering'}],
    [{...regional,person_id:'1'}, {...regional,person_id:'2'}]);
  assert.deepEqual(rows,[{year:'2025',national:1,vienna:null},{year:'2026',national:0,vienna:1}]);
  assert.deepEqual(annualClubGoldSeries([national], [], {startYear:2024})[0],
    {year:'2024',national:null,vienna:null});
  const later = {...regional,event_id:3,stage_id:3,regional_entry_id:'later',date:'2028-05-01'};
  assert.deepEqual(annualClubGoldSeries([{...national,person_id:'1'}], [later])[2],
    {year:'2027',national:null,vienna:null});
  assert.deepEqual(annualClubGoldSeries([],[]),[]);
  const history = {years:{'2000':{national:7,vienna:11},'2012':{national:34,vienna:25}}};
  const historic = annualClubGoldSeries([national],[],{history});
  assert.deepEqual(historic[0], {year:'2000',national:7,vienna:11,source:'club'});
  assert.deepEqual(historic.find((r) => r.year === '2012'),
    {year:'2012',national:34,vienna:25,source:'club'});
  assert.deepEqual(historic.find((r) => r.year === '2013'),
    {year:'2013',national:null,vienna:null});
});

test("club medal counts one team once while preserving ties and different races", () => {
  const base = {event_id: 1, stage_id: 2, result_list_id: 'relay', category: 'Mixed',
    championship: 'ÖSTM', national_rank: 2, rank: 2, result_kind: 'relay',
    club_name: 'Naturfreunde Wien', team_number: '18', team_name: 'NF Wien 2'};
  const rows = [
    {...base, result_id:'1',person_id:'11'}, {...base,result_id:'2',person_id:'12'},
    {...base, result_id:'3',person_id:'11'}, // same runner, second leg
    {...base, result_id:'4',person_id:'13',team_number:'19',team_name:'NF Wien 3'}, // tied team
    {...base, result_id:'5',person_id:'14',event_id:2,stage_id:3}, // another event
    {...base, result_id:'6',person_id:'15',result_kind:'individual',team_number:null,national_rank:1},
  ];
  assert.deepEqual(clubMedalCounts(rows), {gold:1, silver:3, bronze:0});
  const historic = {...base, result_kind:'team', team_number:null, team_name:'Naturfreunde Wien',
    note:'Mannschaft', national_rank:1, rank:1};
  assert.deepEqual(clubMedalCounts([{...historic,result_id:'10'}, {...historic,result_id:'11'}]),
    {gold:2, silver:0, bronze:0});
});

test("pair, regional entries and mixed-club teams keep correct independent units", () => {
  const base = {event_id:1, stage_id:2, result_list_id:'pair',category:'D-14',
    championship:'ÖM',national_rank:1,rank:1,result_kind:'pair',club_name:'Club A'};
  const pair = [
    {...base, person_id:'1',person_name:'Anna A',note:'Partner: Bea B'},
    {...base, person_id:'2',person_name:'Bea B',note:'Partner: Anna A'},
    {...base, person_id:'3',person_name:'Clara C',note:'Partner: Dora D'},
    {...base, person_id:'4',person_name:'Dora D',note:'Partner: Clara C'},
  ];
  assert.deepEqual(clubMedalCounts(pair), {gold:2,silver:0,bronze:0});
  const namesInSourceOrder = [
    {...base,person_id:'5',person_name:'Erik Bonek',observed_name:'Bonek Erik',note:'Partner: Jandl Florian'},
    {...base,person_id:'6',person_name:'Florian Jandl',observed_name:'Jandl Florian',note:'Partner: Bonek Erik'},
    {...base,person_id:'7',person_name:'Ylvi Kastner',observed_name:'Kastner Yl',note:'Partner: Tiefenböck Rita'},
    {...base,person_id:'8',person_name:'Rita Tiefenböck',observed_name:'Tiefenböck Rita',note:'Partner: Kastner Yl'},
  ];
  assert.deepEqual(clubMedalCounts(namesInSourceOrder), {gold:2,silver:0,bronze:0});
  const combined = [
    {...base,person_id:'9',person_name:'Sofia Calvet',observed_name:'Calvet Sofia Kastner M',note:'Partner: Maya Kastner'},
    {...base,person_id:'10',person_name:'Maya Kastner',observed_name:'Calvet Sofia Kastner M',note:'Partner: Sofia Calvet'},
  ];
  assert.deepEqual(clubMedalCounts(combined), {gold:1,silver:0,bronze:0});
  assert.deepEqual(clubMedalCounts([{...base,regional_entry_id:'entry1'},
    {...base,regional_entry_id:'entry1'},{...base,regional_entry_id:'entry2'}]),
    {gold:2,silver:0,bronze:0});
});

test("calendar disciplines use January through December", () => {
  assert.equal(seasonYear("2025-01-01", "footOrienteering"), "2025");
  assert.equal(seasonYear("2025-12-31", "mountainbikeOrienteering"), "2025");
  assert.equal(seasonYear("2025-11-01", "trailOrienteering"), "2025");
});

test("youth grant ages require an explicit youth upper bound", () => {
  for (const name of ["Damen -12", "H-14", "Herren bis 16", "D 18",
    "D15-18", "Herren 13 bis 14", "Mixed Staffel bis 16"]) {
    assert.ok(youthTitleCategory(name), name);
  }
  for (const name of ["Damen -10", "Damen -20", "D 15-", "Damen ab 15",
    "Herren 21-", "Damen ab 165", "Offen"]) {
    assert.equal(youthTitleCategory(name), null, name);
  }
  assert.equal(youthTitleCategory("Wien Damen -14").gender, "D");
  assert.equal(youthTitleCategory("W H-14").gender, "H");
  assert.equal(youthTitleCategory("Damen+Herren -12 W").gender, null);
  assert.equal(youthTitleCategory("Nachwuchs -16").gender, null);
});

test("youth grants never guess the Wiener rate for mixed categories", () => {
  assert.equal(youthTitleGrant("om", null), 400);
  assert.equal(youthTitleGrant("wien", "D"), 110);
  assert.equal(youthTitleGrant("wien", "H"), 90);
  assert.equal(youthTitleGrant("wien", null), null);
});

test("Ski-O season runs from November through October", () => {
  assert.equal(seasonYear("2025-10-31", "skiOrienteering"), "2025");
  assert.equal(seasonYear("2025-11-01", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2025-12-31", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2026-01-01", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2026-10-31", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2026-11-01", "skiOrienteering"), "2027");
});

test("missing dates do not create a season", () => {
  assert.equal(seasonYear("", "skiOrienteering"), "");
  assert.equal(seasonYear(null, "footOrienteering"), "");
});

test("64-bit fallback person IDs remain exact decimal text", () => {
  const fallbackId = "-2617108969934253528";
  assert.equal(personIdString(fallbackId), fallbackId);
  assert.equal(personIdString(9137), "9137");
  assert.equal(personIdString(Number(fallbackId)), null);
  assert.equal(personIdString("not-an-id"), null);
});

test("result categories sort by gender block and ascending age", () => {
  const categories = [
    "Rahmen Offen", "H 45", "D 55", "H -12", "D-14", "D-12", "H -14", "D 19",
  ];
  assert.deepEqual(categories.sort(compareResultCategories), [
    "D-12", "D-14", "D 19", "D 55",
    "H -12", "H -14", "H 45",
    "Rahmen Offen",
  ]);
});

test("regional categories use readable jurisdiction and age labels", () => {
  assert.equal(regionalCategoryLabel("NOE", "D 19"), "NÖ Damen 19-");
  assert.equal(regionalCategoryLabel("NOE", "D-12"), "NÖ Damen -12");
  assert.equal(regionalCategoryLabel("WIEN", "H 45"), "Wien Herren 45-");
  assert.equal(regionalCategoryLabel("BGLD", "DH -14"),
    "Burgenland Damen und Herren -14");
  assert.equal(regionalCategoryLabel("WIEN", "D 15", "Wien Damen 15-"), "Wien Damen 15-");
  assert.equal(regionalCategoryLabel("WIEN", "H 15", "Wien Herren ab 15"), "Wien Herren 15-");
  assert.equal(regionalCategoryLabel("WIEN", "D 14", "Wien Damen -14"), "Wien Damen -14");
});
