const assert = require('node:assert/strict');
const {aggregate, membersForGroup} = require('../site/oefol/titles.js');
const awards = [
  {id:'1', title_type:'ÖSTM', club:'A', members:[{name:'A One'},{name:'B Two'},{name:'A One'}]},
  {id:'2', title_type:'ÖM', club:'A', members:[{name:'A One'},{name:'C Three',club:'B'}]},
];
const label = x => x || '';
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
console.log('Meistertafel aggregation: OK');
