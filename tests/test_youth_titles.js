"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const rules = require("../site/domain_rules.js");

// Exercise the actual renderer without booting sql.js or a browser.
const source = fs.readFileSync(path.join(__dirname, "../site/app.js"), "utf8");
const bootIndex = source.indexOf('window.addEventListener("hashchange", route);');
assert.ok(bootIndex > 0);
const context = vm.createContext({
  window: { OLRDomainRules: rules },
  document: { getElementById: () => ({}) },
  location: { search: "" },
  localStorage: { getItem: () => null },
  URLSearchParams,
});
vm.runInContext(source.slice(0, bootIndex) + "\nglobalThis.renderYouth = renderYouthTitleTable; globalThis.formatDate = fmtDate; globalThis.unitKey = teamUnitKey;", context);

test("club-local Mannschaft numbers do not group different teams", () => {
  const a = {result_kind:"team", team_number:"1", team_name:"Verein A 1"};
  const b = {result_kind:"team", team_number:"1", team_name:"Verein B 1"};
  assert.notEqual(context.unitKey(a), context.unitKey(b));
  assert.equal(context.unitKey(a), context.unitKey({...a}));
  const sourceRow = {result_kind:"team", note:"Mannschaft", team_name:"Naturfreunde Wien", id:1};
  assert.notEqual(context.unitKey(sourceRow), context.unitKey({...sourceRow,id:2}));
});

test("calendar dates and ISO stage timestamps display without time or timezone shift", () => {
  assert.equal(context.formatDate("2026-09-27T00:00:00.000000Z"), "27.09.2026");
  assert.equal(context.formatDate("2026-09-26"), "26.09.2026");
  assert.equal(context.formatDate(""), "");
  assert.equal(context.formatDate("<invalid>"), "&lt;invalid&gt;");
});

test("additional championship classification keeps one start and separate titles", () => {
  const isolated = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0, bootIndex) + `
    query = (sql) => sql.startsWith("PRAGMA table_info") ? [{name:"observed_time_s"}]
      : sql.includes("FROM championship_source_entry cse")
      ? [{person_id:"1",stage_id:1,championship:"ÖSTM",national_rank:3}]
      : [{person_id:"1",stage_id:1,championship:"ÖM",national_rank:1}];
    globalThis.podiums = fetchPodiums;
    globalThis.runnerTable = renderRunnerResultsTable;`, isolated);
  const scope = {dw:{sql:"",params:[]}, personId:"1"};
  assert.equal(isolated.podiums(scope).length, 1);
  const titles = isolated.podiums({...scope, includeAdditional:true});
  assert.equal(titles.length, 2);
  assert.deepEqual(Array.from(titles, (row) => row.championship), ["ÖM", "ÖSTM"]);
  const html = isolated.runnerTable([{stage_date:"2026-03-22",href:"#/event/5203",event_title:"Ski-O",
    category:"D-20",result_kind:"individual",status:"ok",rank:1,time_s:881,
    additional_classifications:[{championship_type:"ÖSTM",category:"Damen",observed_rank:3}]}]);
  assert.equal((html.match(/<tr(?:\s|>)/g) || []).length, 2); // header and one physical result
  assert.match(html, /ÖSTM Damen: Rang 3/);
});

test("youth-title view is restricted to Naturfreunde Wien including old direct links", () => {
  const isolated = vm.createContext({
    window: { OLRDomainRules: rules }, document: { getElementById: () => ({}) },
    location: { search: "" }, localStorage: { getItem: () => null }, URLSearchParams,
  });
  vm.runInContext(source.slice(0, bootIndex) + `
    query = () => [{ n: 1, runners: 1 }];
    regionalJurisdictions = () => [];
    fetchPodiums = () => [];
    fetchRegionalPodiums = () => [];
    globalThis.clubHtml = clubDetailHtml;`, isolated);
  const other = isolated.clubHtml("WAT-OL", "2025", "youth", {});
  assert.doesNotMatch(other, /Jugend-Meistertitel/);
  assert.doesNotMatch(other, /\/youth/);
  assert.match(other, /Alle Medaillen/);
  const own = isolated.clubHtml("Naturfreunde Wien", "2025", null, {});
  assert.match(own, /Jugend-Meistertitel/);
  const ownDeep = isolated.clubHtml("Naturfreunde Wien", "2025", "youth", {});
  assert.match(ownDeep, /Jugend-Meistertitel/);
});

test("club ÖSTM view separates individual and team medals and keeps one club medal", () => {
  const isolated = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0, bootIndex) + `
    query = () => [{n: 3, runners: 2}]; regionalJurisdictions = () => [];
    fetchPodiums = () => globalThis.fixture;
    globalThis.clubHtml = clubDetailHtml;`, isolated);
  const base = {event_id:5124,stage_id:1,stage_number:1,event_title:'Mixed',date:'2026-03-14',
    sport_type:'footOrienteering',result_list_id:'team-list',category:'Mixed',club_name:'Naturfreunde Wien',
    rank:2,national_rank:2,championship:'ÖSTM',status:'ok',result_kind:'relay',team_number:'18'};
  isolated.fixture = [
    {...base,person_id:'1',person_name:'Anna',result_id:'1'},
    {...base,person_id:'2',person_name:'Bea',result_id:'2'},
    {...base,person_id:'1',person_name:'Anna',result_id:'3',result_kind:'individual',
      result_list_id:'individual',category:'D21-E',rank:1,national_rank:1},
    {...base,person_id:'2',person_name:'Bea',result_id:'4',championship:'ÖM',
      result_list_id:'om',category:'H-14',rank:1,national_rank:1},
  ];
  const html = isolated.clubHtml('Naturfreunde Wien','2026','ostm',{});
  assert.match(html,/href="#\/club\/Naturfreunde%20Wien\/2026\/ostm"/);
  assert.match(html,/<span>Gold<\/span>/);
  assert.match(html,/<div class="stat"><b>1<\/b><span>Silber<\/span>/);
  assert.match(html,/<th colspan="3" class="num">Einzel<\/th>/);
  assert.match(html,/<th colspan="3" class="num team-medal-col">Team<\/th>/);
  assert.equal((html.match(/class="num team-medal-col"/g) || []).length, 10);
  assert.doesNotMatch(html,/>ÖM<\/span> ·/);
  assert.match(html,/Anna/); assert.match(html,/Bea/);
  const combined = isolated.clubHtml('Naturfreunde Wien','2026','om',{});
  assert.match(combined,/<div class="stat"><b>1<\/b><span>Silber<\/span>/);
  assert.match(combined,/<div class="stat"><b>2<\/b><span>Gold<\/span>/);
  assert.match(combined,/<h3>ÖSTM<\/h3>[\s\S]*?<b>1<\/b><span>Gold<\/span>[\s\S]*?<b>1<\/b><span>Silber<\/span>/);
  assert.match(combined,/<h3>ÖM<\/h3>[\s\S]*?<b>1<\/b><span>Gold<\/span>/);
  assert.match(combined,/<h3>Gesamt<\/h3>[\s\S]*?<b>2<\/b><span>Gold<\/span>/);
});

test("club success chart uses annual club gold units and links to matching views", () => {
  const isolated = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0, bootIndex) + `
    query = () => [{n:3,runners:2}]; regionalJurisdictions = () => [{code:'WIEN',short_name:'Wiener MS'}];
    fetchPodiums = () => globalThis.national; fetchRegionalPodiums = () => globalThis.regional;
    globalThis.clubHtml = clubDetailHtml;`, isolated);
  const base = {event_id:1,stage_id:2,result_list_id:'t',category:'Mixed',club_name:'Naturfreunde Wien',
    result_kind:'relay',team_number:'18',rank:1,national_rank:1,championship:'ÖSTM',
    date:'2026-03-14',sport_type:'footOrienteering'};
  isolated.national = [{...base,person_id:'1'},{...base,person_id:'2'}];
  isolated.regional = [{...base,regional_entry_id:'w',championship:'Wiener MS'},
    {...base,regional_entry_id:'w',championship:'Wiener MS',person_id:'2'}];
  const html = isolated.clubHtml('Naturfreunde Wien',null,'success',{hrefBase:'#/clubs'});
  assert.match(html,/Erfolgsstatistik/);
  assert.match(html,/2026: 1 Gold bei ÖM\/ÖSTM/);
  assert.match(html,/2026: 1 Gold bei Wiener MS/);
  assert.match(html,/href="#\/clubs\/2026\/om"/);
  assert.match(html,/href="#\/clubs\/2026\/lm\/WIEN"/);
  assert.match(html,/Jahreswerte als Tabelle/);
  assert.ok(html.indexOf('Landes MS') < html.indexOf('Jugend-Meistertitel'));
  assert.ok(html.indexOf('Landes MS') < html.indexOf('Erfolgsstatistik'));
  assert.ok(html.indexOf('Erfolgsstatistik') < html.indexOf('Jugend-Meistertitel'));
});

test("source-only team medal counts for club but not for a fabricated runner", () => {
  const isolated = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0, bootIndex) + `
    query = () => [{n:1,runners:0}]; regionalJurisdictions = () => [];
    fetchPodiums = () => globalThis.fixture; globalThis.clubHtml = clubDetailHtml;`, isolated);
  isolated.fixture = [{event_id:1555,stage_id:1,stage_number:1,event_title:'ÖM Mannschaft',
    date:'2015-09-27',sport_type:'footOrienteering',result_list_id:'team-list',category:'Damen -14',
    club_name:'Naturfreunde Wien',rank:1,national_rank:1,championship:'ÖM',status:'ok',
    result_kind:'team',team_name:'Naturfreunde Wien',result_id:'1',person_id:'-1',
    person_name:'Kastner Hufnagel Tiefenböck',identity_basis:'source-team-only'}];
  const html = isolated.clubHtml('Naturfreunde Wien','2015','om',{});
  assert.match(html,/<div class="stat"><b>1<\/b><span>Gold<\/span>/);
  assert.doesNotMatch(html,/Kastner Hufnagel Tiefenböck/);
  assert.match(html,/ohne eindeutig belegte Einzelpersonen/);
});

test("historical club aggregates are identified and hidden when disciplines are filtered", () => {
  const isolated = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0, bootIndex) + `
    query = () => [{n:1,runners:1}]; regionalJurisdictions = () => [];
    fetchPodiums = () => []; fetchRegionalPodiums = () => [];
    clubSuccessHistory = { 'Naturfreunde Wien': {source_label:'Vereinsstatistik',years:{'2000':{national:7,vienna:11}}} };
    globalThis.clubHtml = clubDetailHtml;
    globalThis.filterOnlyFoot = () => { disciplineFilter = new Set(['footOrienteering']); };`, isolated);
  const all = isolated.clubHtml('Naturfreunde Wien',null,'success',{});
  assert.match(all,/2000 · ÖM\/ÖSTM: 7 Gold · Quelle Vereinsstatistik/);
  assert.match(all,/11 <small class="dim">Verein<\/small>/);
  assert.doesNotMatch(all,/href="#\/club\/Naturfreunde%20Wien\/2000\/om"/);
  isolated.filterOnlyFoot();
  const filtered = isolated.clubHtml('Naturfreunde Wien',null,'success',{});
  assert.doesNotMatch(filtered,/2000 · ÖM\/ÖSTM: 7 Gold/);
});

test("club Landesmeisterschaft summary counts a team once but lists each athlete", () => {
  const isolated = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0, bootIndex) + `
    query = () => [{n:3,runners:3}];
    regionalJurisdictions = () => [{code:'WIEN',short_name:'Wiener MS'}];
    fetchRegionalPodiums = () => globalThis.fixture;
    globalThis.clubHtml = clubDetailHtml;`, isolated);
  const base = {event_id:1,stage_id:2,stage_number:1,event_title:'Wr MS',date:'2026-05-01',
    sport_type:'footOrienteering',result_list_id:'team',category:'Mixed',club_name:'Naturfreunde Wien',
    regional_entry_id:'team1',rank:1,national_rank:1,championship:'Wiener MS',status:'ok',
    result_kind:'relay',team_number:'10'};
  isolated.fixture = [
    {...base,person_id:'1',person_name:'Anna'}, {...base,person_id:'2',person_name:'Bea'},
    {...base,person_id:'3',person_name:'Cleo'},
  ];
  const html = isolated.clubHtml('Naturfreunde Wien','2026',null,{regionalCode:'WIEN'});
  assert.match(html,/<div class="stat"><b>1<\/b><span>Gold<\/span>/);
  assert.match(html,/Anna/); assert.match(html,/Bea/); assert.match(html,/Cleo/);
  assert.match(html,/je Team einmal/);
  assert.ok(html.indexOf('Landes MS') < html.indexOf('Jugend-Meistertitel'));
});

test("regional podium query counts a runner with two relay legs only once", () => {
  const isolated = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0, bootIndex) + `
    query = () => globalThis.fixture;
    globalThis.fetch = fetchRegionalPodiums;`, isolated);
  isolated.fixture = [
    {regional_entry_id:'team1', person_id:'1', time_s:500},
    {regional_entry_id:'team1', person_id:'1', time_s:600},
    {regional_entry_id:'team1', person_id:'2', time_s:700},
    {regional_entry_id:'team2', person_id:'1', time_s:800},
  ];
  const rows = isolated.fetch({jurisdiction:'WIEN',dw:{sql:'',params:[]}});
  assert.equal(rows.length, 3);
});

test("2024 Vienna D14 winner with dated club mapping counts toward youth title", () => {
  const row = { person_id: "9030", person_name: "Matilda Buschek", event_id: 4251,
    date: "2024-04-20", stage_number: 1, result_list_id: "list-d14", stage_id: 1,
    event_title: "NÖ + W Mitteldistanz", club_name: "Naturfreunde Wien",
    category: "D-14", category_full: "D-14", source_category: "W D-14",
    source_category_full: "W D-14", regional_entry_id: "regional-matilda",
    detection_basis: "invitation", identity_state: "resolved", result_kind: "individual",
    status: "ok", out_of_competition: 0, time_s: 1686, national_rank: 1 };
  const html = context.renderYouth([], [row]);
  assert.match(html, /Matilda Buschek/);
  assert.match(html, /Wiener MS<br><small>Σ 1/);
  assert.match(html, /110\s*€/);
  assert.match(html, /event\/4251/);
});

test("2022 Mannschaft D14 is an evidenced championship, individual race is not", () => {
  const source = JSON.parse(fs.readFileSync(path.join(__dirname, "../site/data/national-category-evidence.json"), "utf8"));
  context.nationalFixture = source.events;
  vm.runInContext("nationalCategoryEvidence = nationalFixture", context);
  const base = {event_id:3507, date:"2022-09-24", stage_number:1, category:"Damen bis 14",
    category_full:"Damen bis 14", championship:"ÖM", national_rank:1,
    championship_eligibility_state:"eligible", championship_source_scope:"inferred",
    status:"ok", out_of_competition:0, identity_state:"resolved", result_kind:"team",
    result_list_id:"list:1562752144d8718e6d05698d", team_number:"1", time_s:1771,
    club_name:"Naturfreunde Wien", event_title:"ÖM Mannschaft"};
  const rows = [["9209","Mika Asenbauer"],["9210","Sona Asenbauer"],["8506","Anna Skern"]]
    .map(([person_id,person_name]) => ({...base, person_id, person_name}));
  const html = context.renderYouth(rows, [], {year:"2022",club:"Naturfreunde Wien"});
  assert.match(html, /Σ 3<\/small>/);
  assert.match(html, /Σ 1\s*200 €/);
  const single = context.renderYouth([{...rows[0], result_kind:"individual", category:"Damen bis 14 Einzel"}], []);
  assert.match(single, /ÖM-Kategorie nur abgeleitet/);
});

test("historical Wiener night pairs count both verified winners once", () => {
  const source = JSON.parse(fs.readFileSync(path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const base = {category:"H-14", category_full:"H-14", source_category:"Herren -14",
    source_category_full:"Herren -14", result_kind:"pair", status:"ok", out_of_competition:0,
    rank:1, national_rank:1, identity_state:"resolved", result_list_id:"night",
    regional_entry_id:"winner-pair", stage_number:1, event_id:3401, event_title:"Wiener Nacht",
    date:"2021-10-08", club_name:"Naturfreunde Wien", time_s:1720, detection_basis:"category"};
  const rows = [["8901","Lauri Urbanek"],["9220","Cleo Machold"]]
    .map(([person_id,person_name]) => ({...base, person_id, person_name}));
  const html = context.renderYouth([], rows, {year:"2021",club:"Naturfreunde Wien"});
  assert.match(html, /Σ 2<\/small>/);
  assert.match(html, /Σ 180 €/);
  assert.doesNotMatch(html, /Personenzuordnung prüfen/);
});

test("compact elite U16 classes and official team champions are counted with evidence", () => {
  assert.equal(rules.youthTitleCategory("D16-E").maxAge, 16);
  assert.equal(rules.youthTitleCategory("H18E").maxAge, 18);
  assert.equal(rules.youthTitleCategory("D16-"), null);
  const base = { category: "D16-E", category_full: "D16-E", result_list_id: "list1",
    stage_number: 1, event_id: 4245, event_title: "ÖM Mittel", date: "2024-04-13",
    person_id: "9209", person_name: "Mika Asenbauer", national_rank: 1, status: "ok",
    out_of_competition: 0, identity_state: "resolved", result_kind: "individual",
    championship: "ÖM", championship_eligibility_state: "eligible", championship_source_scope: "winner_only" };
  const team = { ...base, category: "Damen bis 14", category_full: "Damen bis 14", person_id: "9030",
    person_name: "Matilda Buschek", event_id: 4515, date: "2024-06-01", result_kind: "pair",
    result_list_id: "list2", championship_source_scope: "inferred",
    official_national_title_url: "https://www.orienteeringaustria.at/termine/meistertafel/" };
  const html = context.renderYouth([base, team], []);
  assert.match(html, /Mika Asenbauer/);
  assert.match(html, /Matilda Buschek/);
  assert.match(html, /ÖFOL-Meistertafel bestätigt/);
  assert.match(html, /Σ 800 €/);
  assert.match(context.renderYouth([{...team, official_national_title_url: null}], []), /ÖM-Kategorie nur abgeleitet/);
});

test("youth table counts only evidenced winners and makes unknowns visible", () => {
  const base = { person_id: "1", person_name: "Anna", category: "D-14",
    category_full: "Damen -14", result_list_id: "list1", stage_id: 1,
    stage_number: 1, event_id: 1, event_title: "Wettkampf", date: "2025-01-01",
    national_rank: 1, status: "ok", out_of_competition: 0,
    identity_state: "resolved", championship: "ÖM",
    championship_eligibility_state: "eligible", championship_source_scope: "winner_only" };
  const regional = { ...base, regional_entry_id: "region1", detection_basis: "category",
    source_category: "D-14 Wien", source_category_full: "Damen -14 Wien" };
  const inferred = { ...base, person_id: "2", person_name: "Zweite", result_list_id: "list2",
    championship_source_scope: "inferred" };
  const mixed = { ...regional, person_id: "3", person_name: "Dritte", regional_entry_id: "region2",
    source_category: "DH-12 Wien", source_category_full: "Damen+Herren -12 Wien" };
  const notWinner = { ...base, person_id: "4", person_name: "Vierte", national_rank: 2 };
  const wrongAge = { ...base, person_id: "5", person_name: "Fünfte", category_full: "Damen -20" };
  const ooc = { ...base, person_id: "7", person_name: "Siebte", out_of_competition: 1 };
  const noEvidence = { ...regional, person_id: "6", person_name: "Sechste", regional_entry_id: "region3",
    detection_basis: "event_title" };
  const natOnly = { ...regional, person_id: "8", person_name: "Achte", regional_entry_id: "region4",
    detection_basis: "source_nat" };
  const html = context.renderYouth([base, inferred, notWinner, wrongAge, ooc], [regional, regional, mixed, noEvidence, natOnly]);
  assert.match(html, /Σ 1<\/small><\/th><th class="num">Wiener MS<br><small>Σ 2/);
  assert.match(html, /Σ 510 € \+ offen/);
  assert.match(html, /3 mögliche Jugendtitel noch prüfen/);
  assert.match(html, /ÖM-Kategorie nur abgeleitet/);
  assert.match(html, /Wiener Meisterschaftskategorie prüfen/);
  assert.doesNotMatch(html, /Vierte/);
  assert.doesNotMatch(html, /Fünfte/);
  assert.doesNotMatch(html, /Siebte/);
  assert.match(html, /Fördersatz offen/);
});

test("WOLV evidence requires matching event, date, person, club and time", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  assert.equal(source.events["4879"].titles.length, 10);
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const winner = { person_id: "8980", person_name: "Albert Imriska", category: "DH -12",
    source_category: "Wien Damen&Herren -12", source_category_full: "Wien Damen&Herren -12", regional_entry_id: "w1", detection_basis: "category",
    result_list_id: "list1", stage_number: 1, event_id: 4879, event_title: "Sprint MS",
    date: "2025-05-10", national_rank: 1, identity_state: "resolved", status: "ok",
    out_of_competition: 0, time_s: 532, club_name: "Naturfreunde Wien" };
  const matched = context.renderYouth([], [winner]);
  assert.match(matched, /WOLV-Meistertitel bestätigt \(D-12\)/);
  assert.match(matched, /Fördersatz 90 € bestätigt/);
  assert.match(matched, /Σ 90 €/);
  assert.doesNotMatch(matched, /Fördersatz offen/);
  for (const changed of [{ time_s: 533 }, { date: "2025-05-11" },
    { club_name: "Anderer Verein" }, { person_id: "1" }, { event_id: 1234 },
    { source_category: "andere Klasse" }]) {
    const html = context.renderYouth([], [{ ...winner, ...changed }]);
    assert.doesNotMatch(html, /WOLV-Meistertitel bestätigt/);
    assert.doesNotMatch(html, /Fördersatz 90 € bestätigt/);
    assert.doesNotMatch(html, /Σ 90 €/);
  }
});

test("WOLV night list confirms both winners in the pair but not unannotated ranks", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  assert.equal(source.events["4963"].titles.length, 7);
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const base = { category: "H-14", source_category_full: "H-14", detection_basis: "source_nat",
    regional_entry_id: "pair", result_list_id: "list1", stage_number: 1, event_id: 4963,
    event_title: "Nacht MS", date: "2025-10-11", national_rank: 1,
    identity_state: "resolved", status: "ok", result_kind: "pair", out_of_competition: 0,
    time_s: 1576, club_name: "WAT-OL" };
  const emil = { ...base, person_id: "10035", person_name: "Emil Wally" };
  const arthur = { ...base, person_id: "10052", person_name: "Arthur Wally" };
  const maria = { ...base, person_id: "8110", person_name: "Maria Beck", category: "D15-18",
    source_category_full: "D15-18", regional_entry_id: "maria", result_kind: "individual",
    time_s: 4047, club_name: "Naturfreunde Wien" };
  const html = context.renderYouth([], [emil, arthur, maria]);
  assert.match(html, /Wiener MS<br><small>Σ 2/);
  assert.match(html, /Σ 180 €/);
  assert.equal((html.match(/WOLV-Meistertitel bestätigt \(H-14\)/g) || []).length, 2);
  assert.match(html, /1 mögliche Jugendtitel noch prüfen/);
  assert.match(html, /WOLV nennt Rang 1, aber keinen Meistertitel/);
  assert.match(html, /WOLV: Rang 1, Titel nicht ausgewiesen/);
});

test("WOLV middle list establishes the mixed U12 winner's female title and grant", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  assert.equal(source.events["4847"].titles.length, 8);
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const winner = { person_id: "10345", person_name: "Lihuen Le Blanc", category: "DH - 12",
    source_category: "Herren+Damen - 12W", source_category_full: "Herren+Damen - 12W",
    regional_entry_id: "mittel-u12", detection_basis: "category", result_kind: "individual",
    result_list_id: "list1", stage_number: 1, event_id: 4847, event_title: "Mittel MS",
    date: "2025-05-24", national_rank: 1, identity_state: "resolved", status: "ok",
    out_of_competition: 0, time_s: 2392, club_name: "Naturfreunde Wien" };
  const html = context.renderYouth([], [winner]);
  assert.match(html, /WOLV-Meistertitel bestätigt/);
  assert.match(html, /Σ 110 €/);
  assert.match(html, /Fördersatz 110 € bestätigt/);
  assert.doesNotMatch(html, /Fördersatz offen/);
  const other = context.renderYouth([], [{ ...winner, person_id: "2" }]);
  assert.doesNotMatch(other, /Σ 110 €/);
  assert.match(other, /Fördersatz offen/);
  const noTitle = context.renderYouth([], [{ ...winner, person_id: "8506",
    person_name: "Anna Skern", category: "D 13-14W", source_category: "Damen 13-14W",
    source_category_full: "Damen 13-14W", time_s: 1871 }]);
  assert.match(noTitle, /WOLV nennt Rang 1, aber keinen Meistertitel/);
  assert.doesNotMatch(noTitle, /Σ 110 €/);
});

test("2026 mixed relay grants are confirmed only for the exact winning team members", () => {
  const source = JSON.parse(fs.readFileSync(path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const base = { category: "Mixed bis 16", source_category: "W Nachw-16", source_category_full: "Wien Nachwuchs -16",
    regional_entry_id: "team", result_list_id: "list", stage_number: 1, event_id: 5382,
    event_title: "Mixed-Staffel", date: "2026-06-21", national_rank: 1, team_number: "5",
    identity_state: "resolved", status: "ok", result_kind: "relay", out_of_competition: 0,
    club_name: "Naturfreunde Wien", detection_basis: "invitation" };
  const rows = [
    {...base, person_id:"8979", person_name:"Adam Imriska", time_s:511},
    {...base, person_id:"8900", person_name:"Annina Urbanek", time_s:681},
    {...base, person_id:"8980", person_name:"Albert Imriska", time_s:635},
  ];
  const html = context.renderYouth([], rows);
  assert.match(html, /Wiener MS<br><small>Σ 3/);
  assert.match(html, /Σ 290 €/);
  assert.equal((html.match(/Fördersatz 90 € bestätigt/g) || []).length, 2);
  assert.equal((html.match(/Fördersatz 110 € bestätigt/g) || []).length, 1);
  assert.doesNotMatch(html, /Fördersatz offen/);
  for (const changed of [{team_number:"6"}, {time_s:512}, {person_id:"7"},
    {club_name:"WAT-OL"}, {date:"2026-06-22"}, {result_kind:"individual"}]) {
    const invalid = context.renderYouth([], [{...rows[0], ...changed}]);
    assert.doesNotMatch(invalid, /Fördersatz 90 € bestätigt/);
    assert.doesNotMatch(invalid, /Σ 90 €/);
  }
});

test("source conflicts remain visible without fabricating or double-counting results", () => {
  const source = JSON.parse(fs.readFileSync(path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const empty = context.renderYouth([], [], {year:"2025", club:"Naturfreunde Wien"});
  assert.match(empty, /Anna Skern/);
  assert.match(empty, /möglicher Titel mit Quellenwiderspruch – nicht mitgezählt/);
  assert.match(empty, /Veranstalter-Ergebnis/);
  assert.match(empty, /Σ 0 €/);
  assert.doesNotMatch(context.renderYouth([], [], {year:"2026", club:"Naturfreunde Wien"}), /möglicher Titel mit Quellenwiderspruch/);
  assert.doesNotMatch(context.renderYouth([], [], {year:"2025", club:"WAT-OL"}), /Anna Skern/);
  const winner = {person_id:"8980", person_name:"Albert Imriska", category:"H-14",
    source_category:"H -14", source_category_full:"H -14", regional_entry_id:"h14", detection_basis:"invitation",
    result_list_id:"list", stage_number:1, event_id:5391, event_title:"Mitteldistanz",
    date:"2026-04-26", national_rank:1, identity_state:"resolved", status:"ok", result_kind:"individual",
    out_of_competition:0, time_s:1566, club_name:"Naturfreunde Wien"};
  const html = context.renderYouth([], [winner], {year:"2026", club:"Naturfreunde Wien"});
  assert.match(html, /ANNE-Ergebnis: Name maßgeblich/);
  assert.match(html, /abweichender Vorname/);
  assert.match(html, /Albert Imriska/);
  assert.match(html, /Σ 90 €/);
  assert.doesNotMatch(context.renderYouth([], [{...winner, time_s:1567}]), /ANNE-Ergebnis: Name maßgeblich/);
});

test("2026 audited national team evidence is restricted to event, date, category and kind", () => {
  const source = JSON.parse(fs.readFileSync(path.join(__dirname, "../site/data/national-category-evidence.json"), "utf8"));
  context.nationalFixture = source.events;
  vm.runInContext("nationalCategoryEvidence = nationalFixture", context);
  const row = {person_id:"9030", person_name:"Matilda Buschek", category:"D15-18", category_full:"Damen 15 bis 18",
    result_list_id:"list", result_kind:"relay", stage_number:1, event_id:5340, event_title:"ÖM Mannschaft",
    date:"2026-10-26", national_rank:1, identity_state:"resolved", status:"ok", out_of_competition:0,
    championship:"ÖM", championship_eligibility_state:"eligible", championship_source_scope:"inferred"};
  const html = context.renderYouth([row], []);
  assert.match(html, /ÖM-Kategorie laut Ausschreibung/);
  assert.match(html, /Ergebnis ↗/);
  assert.match(html, /Σ 400 €/);
  for (const changed of [{event_id:5341}, {date:"2026-10-25"}, {stage_number:2},
    {category:"H15-18"}, {result_kind:"individual"}]) {
    assert.doesNotMatch(context.renderYouth([{...row,...changed}], []), /ÖM-Kategorie laut Ausschreibung/);
  }
});

test("ÖM invitation confirms only its listed event classes, not a person's eligibility", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/national-category-evidence.json"), "utf8"));
  context.nationalFixture = source.events;
  vm.runInContext("nationalCategoryEvidence = nationalFixture", context);
  const winner = { person_id: "8900", person_name: "Annina Urbanek", category: "Damen bis 14",
    category_full: "Damen bis 14", result_list_id: "list3", result_kind: "individual",
    stage_number: 1, event_id: 4830, event_title: "ÖM Mittel", date: "2025-09-21",
    national_rank: 1, identity_state: "resolved", status: "ok", out_of_competition: 0,
    championship: "ÖM", championship_eligibility_state: "eligible", championship_source_scope: "inferred" };
  assert.match(context.renderYouth([winner], []), /ÖM-Kategorie laut Ausschreibung/);
  assert.match(context.renderYouth([winner], []), /Σ 400 €/);
  for (const changed of [{ event_id: 4831 }, { date: "2025-09-22" }, { stage_number: 2 },
    { category: "Damen ab 15 bis 18", category_full: "Damen ab 15 bis 18" },
    { result_kind: "relay" }]) {
    const html = context.renderYouth([{ ...winner, ...changed }], []);
    assert.doesNotMatch(html, /ÖM-Kategorie laut Ausschreibung/);
    assert.doesNotMatch(html, /Σ 400 €/);
  }
  const unclearEligibility = context.renderYouth([{ ...winner, championship_eligibility_state: "provisional" }], []);
  assert.match(unclearEligibility, /ÖM-Berechtigung prüfen/);
  assert.doesNotMatch(unclearEligibility, /Σ 400 €/);
});

test("2026 ÖM sprint invitation confirms both U18 elite winners only on stage 2", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/national-category-evidence.json"), "utf8"));
  context.nationalFixture = source.events;
  vm.runInContext("nationalCategoryEvidence = nationalFixture", context);
  const base = { result_kind: "individual", stage_number: 2, event_id: 5278,
    event_title: "ÖM 3Tage-4Läufe", date: "2026-05-02", national_rank: 1,
    identity_state: "resolved", status: "ok", out_of_competition: 0,
    championship: "ÖM", championship_eligibility_state: "eligible",
    championship_source_scope: "inferred" };
  const mika = { ...base, person_id: "9209", person_name: "Mika Asenbauer",
    category: "D18-E", category_full: "Damen bis 18 Elite", result_list_id: "d18" };
  const lauri = { ...base, person_id: "8901", person_name: "Lauri Urbanek",
    category: "H18-E", category_full: "Herren bis 18 Elite", result_list_id: "h18" };
  const html = context.renderYouth([mika, lauri], []);
  assert.match(html, /Σ 800 €/);
  assert.equal((html.match(/ÖM-Kategorie laut Ausschreibung/g) || []).length, 2);
  assert.doesNotMatch(html, /noch prüfen/);
  for (const changed of [{ stage_number: 3 }, { date: "2026-05-03" },
    { category: "D15-18" }, { championship_eligibility_state: "provisional" },
    { identity_state: "candidate" }, { result_kind: "relay" }]) {
    const invalid = context.renderYouth([{ ...mika, ...changed }], []);
    assert.doesNotMatch(invalid, /Σ 400 €/);
  }
});

test("user-confirmed 2024 youth titles are exact and do not override other checks", () => {
  // Isolate the user-confirmation path: separately reviewed invitations can
  // independently establish the category even when a confirmation changes.
  const previousEvidence = vm.runInContext("nationalCategoryEvidence", context);
  vm.runInContext("nationalCategoryEvidence = {}", context);
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/youth-title-confirmations.json"), "utf8"));
  context.confirmationFixture = source.events;
  vm.runInContext("youthTitleConfirmations = confirmationFixture", context);
  const rows = Object.entries(source.events).flatMap(([eventId, event]) => event.entries.map((e) => ({
    ...e, event_id: Number(eventId), event_title: "ÖM", date: event.date,
    stage_number: event.stage_number, person_name: e.name, club_name: e.club,
    category_full: e.category, national_rank: 1, status: "ok", out_of_competition: 0,
    identity_state: "resolved", championship: "ÖM", championship_eligibility_state: "eligible",
    championship_source_scope: "inferred" })));
  assert.equal(rows.length, 9);
  const html = context.renderYouth(rows, []);
  assert.match(html, /Σ 3[.\s\u00a0]600 €/);
  assert.equal((html.match(/ÖM-Titel vom Nutzer bestätigt/g) || []).length, 9);
  assert.equal((html.match(/H15–18 ohne Eintrag/g) || []).length, 3);
  assert.doesNotMatch(html, /noch prüfen/);
  const individual = rows[0], relay = rows.find((r) => r.result_kind === "relay");
  for (const changed of [{ event_id: 4000 }, { date: "2024-06-09" }, { stage_number: 2 },
    { person_id: "1" }, { club_name: "Anderer Verein" }, { category: "Herren bis 14" },
    { result_list_id: "other" }, { time_s: individual.time_s + 1 },
    { result_kind: "relay" }, { identity_state: "candidate" },
    { championship_eligibility_state: "unknown" }]) {
    assert.doesNotMatch(context.renderYouth([{ ...individual, ...changed }], []), /Σ 400 €/);
  }
  assert.doesNotMatch(context.renderYouth([{ ...relay, team_number: "999" }], []), /Σ 400 €/);
  context.savedNationalEvidence = previousEvidence;
  vm.runInContext("nationalCategoryEvidence = savedNationalEvidence", context);
});
