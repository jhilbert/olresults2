const assert = require('node:assert/strict');
const {aggregate, membersForGroup, rankGroups} = require('../site/oefol/titles.js');
const awards = [
  {id:'1', title_type:'ÖSTM', club:'A', members:[{name:'A One'},{name:'B Two'},{name:'A One'}]},
  {id:'2', title_type:'ÖM', club:'A', members:[{name:'A One'},{name:'C Three',club:'B'}]},
];
const label = x => x || '';
const bergers = [
  {id:'2014',title_type:'ÖM',club:'A',members:[{name:'Alexander Berger',source_row:'tablepress-18:row-45'}]},
  {id:'2013',title_type:'ÖM',club:'A',members:[{name:'Xander Berger',source_row:'tablepress-19:row-187'}]},
  {id:'2006',title_type:'ÖM',club:'B',members:[{name:'Alexander Berger',source_row:'tablepress-26:row-99'}]},
  {id:'2006-lang',title_type:'ÖM',club:'C',members:[{name:'Alexander Berger',source_row:'tablepress-26:row-57'}]},
];
const bergerGroups = aggregate(bergers,'people',label);
assert.equal(bergerGroups.length,3);
assert.equal(bergerGroups.find(g=>g.name==='Xander BERGER').total,2);
assert.equal(bergerGroups.find(g=>g.name==='Alexander Berger').total,1);
assert.equal(bergerGroups.find(g=>g.name==='Alexander BERGER (Zuordnung offen)').total,1);
const a = aggregate(awards, 'people', label);
assert.equal(a.find(x=>x.name==='A One').total, 2);
assert.equal(a.find(x=>x.name==='A One').oestm, 1);
assert.equal(aggregate(awards,'clubs',label).find(x=>x.name==='A').total, 2);
assert.equal(aggregate(awards,'clubs',label).find(x=>x.name==='B').total, 1);
assert.equal(aggregate(awards,'people',label,'A').some(x=>x.name==='C Three'), false);
assert.deepEqual(membersForGroup(awards[1],'clubs','A',label).map(m=>m.name), ['A One']);
assert.deepEqual(membersForGroup(awards[1],'clubs','B',label).map(m=>m.name), ['C Three']);
assert.deepEqual(membersForGroup(awards[1],'people','a one',label).map(m=>m.name), ['A One']);
const formatted = [{id:'x',title_type:'ÖM',club:'A',members:[{name:'Anna Strauß',display_name:'Anna STRAUSS'}]}];
const fg = aggregate(formatted,'people',label)[0];
assert.equal(fg.name,'Anna STRAUSS');
assert.equal(membersForGroup(formatted[0],'people',fg.name,label,fg.key)[0].name,'Anna Strauß');
const unresolved = [{id:'3',title_type:'ÖM',club:'A',members:[{name:'KASTNER',source_row:'r1'}]}, {id:'4',title_type:'ÖM',club:'A',members:[{name:'KASTNER',source_row:'r2'}]}];
assert.equal(aggregate(unresolved,'people',label).length,2);
const kolb = [{id:'5',title_type:'ÖM',club:'A',members:[{name:'Helga Kolb'}]}, {id:'6',title_type:'ÖSTM',club:'A',members:[{name:'Helga Kromp-Kolb'}]}, {id:'7',title_type:'ÖM',club:'A',members:[{name:'Helg Kromp-Kolb'}]}];
const kg = aggregate(kolb,'people',label);
assert.equal(kg.length,1);
assert.equal(kg[0].name,'Helga KOLB / Helga KROMP-KOLB');
assert.equal(kg[0].total,3);
assert.equal(membersForGroup(kolb[0],'people',kg[0].name,label,kg[0].key).length,1);
const changedNames = ['Katja Bonek','Katja Gassner','Claudia Künzel','Claudia Künzl','Claudia Künzel-Bonek','Claudia Bonek'].map((name,i)=>({id:`change-${i}`,title_type:'ÖM',club:'A',members:[{name}]}));
const ng = aggregate(changedNames,'people',label);
assert.equal(ng.length,2);
assert.equal(ng.find(g=>g.name==='Katja BONEK / Katja GASSNER').total,2);
assert.equal(ng.find(g=>g.name==='Claudia KÜNZEL / Claudia BONEK').total,4);
assert.equal(membersForGroup(changedNames[3],'people',ng.find(g=>g.name.startsWith('Claudia')).name,label,ng.find(g=>g.name.startsWith('Claudia')).key).length,1);
for (const [first, second, display] of [
  ['Ursula Kadan','Ursula Fesselhofer','Ursula KADAN / Ursula FESSELHOFER'],
  ['Lucie Böhm','Lucie Rothauer','Lucie BÖHM / Lucie ROTHAUER'],
  ['Elisabeth Nagy','Sissy Knapp','Elisabeth NAGY / Elisabeth KNAPP'],
  ['Gernot Kerschbaumer','Gernot Ymsén-Kerschbaumer','Gernot KERSCHBAUMER / Gernot YMSÉN'],
  ['Regina Mandl','Regina Habenicht','Regina MANDL / Regina HABENICHT'],
  ['Traude Fesselhofer','Waltraud Köpl','Waltraud KÖPL / Traude FESSELHOFER'],
  ['Andrea Eisl','Andrea Strasser','Andrea EISL / Andrea STRASSER'],
  ['Christine Gathercole','Christine Müller','Christine MÜLLER / Christine GATHERCOLE'],
  ['Bati Tobler','Bati Fuchs','Bati TOBLER / Bati FUCHS'],
  ['Sonja Zinkl','Sonja Reisinger-Zinkl','Sonja ZINKL / Sonja REISINGER-ZINKL'],
  ['Elisabeth Hohenwarter','Lisi Sandrisser','Elisabeth HOHENWARTER / Lisi SANDRISSER'],
  ['Thea Lillehov','Thea Schneider-Lillehov','Thea LILLEHOV / Thea SCHNEIDER-LILLEHOV'],
  ['Michaela Gigo','Michaela Gigon','Michaela GIGON'],
  ['Wolfgang Kradischnig','Kradischnig Wolfgang','Wolfgang KRADISCHNIG'],
  ['Günter Kradischnig','Günther Kradischnig','Günter KRADISCHNIG'],
]) {
  const items = [first, second].map((name,i)=>({id:`n-${i}`,title_type:'ÖM',club:'A',members:[{name}]}));
  const group = aggregate(items,'people',label);
  assert.equal(group.length,1);
  assert.equal(group[0].name,display);
  assert.equal(group[0].total,2);
}
const tob = [
  {id:'m',title_type:'ÖM',club:'A',members:[{name:'Barbara Tobler',source_row:'older'}]},
  {id:'u',title_type:'ÖM',club:'A',members:[{name:'Barbara Tobler',source_row:'tablepress-36:row-59'}]},
  {id:'d',title_type:'ÖM',club:'A',members:[{name:'Bati Fuchs'}]},
];
const tg = aggregate(tob,'people',label);
assert.equal(tg.length,3);
assert.equal(tg.find(g=>g.name.includes('Zuordnung offen')).total,1);
assert.equal(tg.find(g=>g.name==='Barbara Tobler').total,1);
assert.equal(tg.find(g=>g.name==='Bati TOBLER / Bati FUCHS').total,1);
assert.notEqual(aggregate([{id:'x',title_type:'ÖM',club:'A',members:[{name:'Michael Gigon',source_row:'other'}]},{id:'y',title_type:'ÖM',club:'A',members:[{name:'Michaela Gigon'}]}],'people',label).length,1);
// Historical source names are grouped while preserving one medal per team.
const historic = [
  {id:'old',title_type:'ÖM',club:'TV Fürstenfeld',members:[{name:'A One'},{name:'B Two'}]},
  {id:'new',title_type:'ÖSTM',club:'OC Fürstenfeld',members:[{name:'A One'}]},
];
const clubAlias = c => c === 'TV Fürstenfeld' ? 'OC Fürstenfeld' : c || '';
const hc = aggregate(historic,'clubs',clubAlias);
assert.equal(hc.length,1);
assert.deepEqual([hc[0].name,hc[0].oem,hc[0].oestm,hc[0].total],['OC Fürstenfeld',1,1,2]);
assert.equal(aggregate(historic,'clubs',clubAlias,'OC Fürstenfeld')[0].total,2);
assert.equal(historic[0].club,'TV Fürstenfeld');
const sameName = [
  {id:'old-innsbruck',year:2016,title_type:'ÖM',club:'Laufklub Kompass Innsbruck',members:[{name:'A One'}]},
  {id:'new-innsbruck',year:2026,title_type:'ÖM',club:'Laufklub Kompass Innsbruck',members:[{name:'B Two'}]},
];
const datedClub = (club, year) => club === 'Laufklub Kompass Innsbruck' && year < 2025 ? 'Orienteering Imst Oberland' : club;
assert.deepEqual(aggregate(sameName,'clubs',datedClub).map(g=>g.name).sort(), ['Laufklub Kompass Innsbruck','Orienteering Imst Oberland']);
assert.equal(membersForGroup(sameName[0],'clubs','Orienteering Imst Oberland',datedClub).length,1);
const ranked = rankGroups([{name:'A',total:5},{name:'Kastner A',total:4},{name:'Kastner B',total:4},{name:'Kastner C',total:2}]);
assert.deepEqual(ranked.map(g=>g.rank), [1,2,2,4]);
assert.deepEqual(ranked.filter(g=>g.name.includes('Kastner')).map(g=>g.rank), [2,2,4]);
console.log('Meistertafel aggregation: OK');
