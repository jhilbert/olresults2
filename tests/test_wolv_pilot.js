"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const {sourceMapping,sourceGroup,historicalMapping,categoryKey,mixedCategoryKey,uniqueAgeRangeCandidate,uniqueExactPodiumCandidate,sourceMatches,compare,anneCorrectEvidence,userConfirmedWolvError,markReconstructedComparison,render} = require('../site/wolv/wolv.js');

test('mixed category aliases preserve age boundaries and require one matching relay', () => {
  for (const value of ['Nachwuchs - 16:', 'Mixed Nachwuchs -16', 'Mixed bis 16']) assert.equal(mixedCategoryKey(value),'youth16');
  for (const value of ['Allgemeine Kategorie:', 'Mixed Allgemein', 'Allgemeine Kat.', 'Allgemeine 17', 'Allgemein 17-', 'Mixed ab 17']) assert.equal(mixedCategoryKey(value),'open');
  for (const value of ['Senioren 35-:', 'Mixed Senioren 35-', 'Mixed ab 35']) assert.equal(mixedCategoryKey(value),'senior35');
  for (const value of ['Senioren 50 -:', 'Mixed Senioren 50-', 'Mixed ab 50']) assert.equal(mixedCategoryKey(value),'senior50');
  for (const value of ['Mix -12', 'Senioren 40-', 'H-16', 'Mixed bis 18']) assert.equal(mixedCategoryKey(value),null);
  const rows = [{event:4880,category:'Allgemeine 17',event_title:'W+NÖ Mixed-Sprint-Staffel'}];
  const fakeDb = (items) => ({prepare:() => {let i=0; return {bind:()=>{},step:()=>i<items.length,
    getAsObject:()=>items[i++],free:()=>{}};}});
  const source = {source_category:'Allgemeine Kategorie:',event_heading:'10.5.2025; Seestadt'};
  assert.deepEqual(historicalMapping(fakeDb(rows),source,2025,'mixed'),{group:'mixed',event:4880,category:'Allgemeine 17'});
  assert.equal(historicalMapping(fakeDb([...rows,...rows.map((r)=>({...r,event:1}))]),source,2025,'mixed').event,null);
  assert.equal(historicalMapping(fakeDb([{...rows[0],event_title:'Wiener Sprint'}]),source,2025,'mixed').event,null);
  assert.equal(historicalMapping(fakeDb(rows),{...source,event_heading:'nicht ausgetragen'},2025,'mixed').event,null);
});

test('maps the mixed relay and different long-distance dates independently', () => {
  assert.deepEqual(sourceMapping({page:2,source_category:'Nachwuchs - 16:'}),
    {group:'mixed',event:5382,category:'Mixed bis 16'});
  assert.equal(sourceMapping({page:1,column:1,source_category:'D 19-'}).event,5247);
  assert.equal(sourceMapping({page:1,column:1,source_category:'D 13-14'}).event,5496);
  assert.equal(sourceMapping({page:1,column:1,source_category:'D 15-18'}).category,'D-18');
  assert.equal(sourceMapping({page:1,column:4,source_category:'H 15-18'}).category,'H15-18');
  assert.equal(categoryKey('H 150-'),categoryKey('H 150'));
  assert.notEqual(categoryKey('H -14'),categoryKey('H 14-'));
  assert.equal(categoryKey('Damen ab 15'.replace('Damen','D')),categoryKey('D 15-'));
  assert.equal(categoryKey('H bis 14'),categoryKey('H -14'));
  assert.notEqual(categoryKey('H ab 14'),categoryKey('H bis 14'));
});

test('comparison requires full names, club, roster and matching rank', () => {
  const candidates = [
    {rank:1,club:'Naturfreunde Wien',names:new Set(['Thomas Radon'])},
    {rank:2,club:'Naturfreunde Wien',names:new Set(['Wolfgang Waldhäusl'])},
  ];
  const entry = {medals:[{rank:1,text:'RADON Thomas NF Wien'}, {rank:2,text:'WALDHÄUSL Wolfgang NF Wien'}]};
  assert.equal(sourceMatches(entry.medals[0].text,candidates[0]),true);
  assert.equal(compare(entry,candidates).state,'match');
  assert.equal(compare({medals:[{rank:2,text:'RADON Thomas NF Wien'},{rank:1,text:'WALDHÄUSL Wolfgang NF Wien'}]},candidates).state,'mismatch');
  assert.equal(compare({medals:[{rank:1,text:'RADON Thomas unbekannt'}]},candidates).state,'unknown');
  assert.deepEqual(compare({medals:[{rank:1,text:'RADON Thomas unbekannt'}]},candidates).differences,
    ['WOLV Platz 1: RADON Thomas unbekannt','OLResults Platz 1: Thomas Radon (Naturfreunde Wien)']);
  assert.equal(compare({medals:[]},candidates).state,'unknown');
  assert.equal(compare(entry,null).state,'unknown');
  assert.equal(compare({medals:[entry.medals[0]]},candidates).state,'unknown');
  const tiedCandidates = [...candidates,
    {rank:3,club:'WAT-OL',names:new Set(['Erika Beispiel'])}];
  assert.equal(compare({...entry, source_text:'1) A\n2) B\n3) -'},tiedCandidates).state,'mismatch');
  assert.equal(compare(entry,tiedCandidates).state,'unknown');
  assert.equal(sourceMatches('NF Wien 2: RADON Thomas - KOLAR Fabian',
    {club:'Naturfreunde Wien', names:new Set(['Thomas Radon','Fabian Kolar'])}),true);
  assert.equal(sourceMatches('WAT: GLANER Franz / WAREYKA Sabine / POIRSON Marius',
    {club:'WAT-OL',names:new Set(['Franz Wareyka-Glaner','Sabine Wareyka','Marius Poirson'])}),true);
  assert.equal(sourceMatches('OLT Transdanubien: GLANER Franz',
    {club:'OLT Transdanubien',names:new Set(['Franz Wareyka-Glaner'])}),false);
});

test('known source conflict is red only for the exact documented participants', () => {
  const row = {medals:[{rank:1,text:'IMRISKA Adam NF Wien'},
    {rank:2,text:'FRITZE Reto NF Wien'},{rank:3,text:'DOBLER Oskar MTV Hernals'}]};
  const candidates = [
    {rank:1,club:'Naturfreunde Wien',names:new Set(['Albert Imriska'])},
    {rank:2,club:'Naturfreunde Wien',names:new Set(['Reto Fritze'])},
    {rank:3,club:'MTV Hernals',names:new Set(['Oskar Dobler'])},
  ];
  assert.equal(compare(row,candidates,{event:5391,category:'H-14'}).state,'mismatch');
  assert.equal(compare(row,candidates,{event:9999,category:'H-14'}).state,'unknown');
});

test('ANNE correct is a separate source-pinned verdict and never hides the WOLV mismatch', () => {
  const entry = {medals:[{rank:1,text:'GASSNER Jasmina NF Wien'},
    {rank:2,text:'TIEFENBÖCK Tina NF Wien'},{rank:2,text:'SIMKOVICS Anna OLC Wienerwald'}]};
  const candidates = [
    {rank:1,club:'Naturfreunde Wien',names:new Set(['Jasmina Gassner']),times:new Set([897])},
    {rank:2,club:'Naturfreunde Wien',names:new Set(['Tina Tiefenböck']),times:new Set([925])},
    {rank:3,club:'OLC Wienerwald',names:new Set(['Anna Simkovics']),times:new Set([934])},
  ];
  const mapping = {event:3953,category:'D 15'}, comparison = compare(entry,candidates,mapping);
  assert.equal(comparison.state,'mismatch');
  assert.match(anneCorrectEvidence(entry,candidates,mapping,comparison).explanation,/15:25.*15:34/);
  assert.equal(anneCorrectEvidence(entry,candidates,{...mapping,event:123},comparison),null);
  assert.equal(anneCorrectEvidence(entry,candidates,{...mapping,category:'D 45'},comparison),null);
  assert.equal(anneCorrectEvidence(entry,[...candidates.slice(0,2),{...candidates[2],times:new Set([925])}],mapping,comparison),null);
  assert.equal(anneCorrectEvidence({...entry,medals:entry.medals.slice(0,2)},candidates,mapping,comparison),null);
  assert.equal(anneCorrectEvidence(entry,candidates,mapping,{state:'match',label:'Übereinstimmung'}),null);
});

test('2022 night H45 ANNE-correct marker needs the exact source ranks and times', () => {
  const entry = {medals:[{rank:1,text:'WALDHÄUSL Wolfgang NF Wien'},
    {rank:2,text:'SCHNEIDER Hartmut MTV Hernals'},{rank:2,text:'KROUPA Günther NF Wien'}]};
  const candidates = [
    {rank:1,club:'Naturfreunde Wien',names:new Set(['Wolfgang Waldhäusl']),times:new Set([2294])},
    {rank:2,club:'MTV Hernals',names:new Set(['Hartmut Schneider']),times:new Set([2817])},
    {rank:3,club:'Naturfreunde Wien',names:new Set(['Günther Kroupa']),times:new Set([2972])}];
  const mapping={event:3847,category:'H45'};
  const verdict=anneCorrectEvidence(entry,candidates,mapping,compare(entry,candidates,mapping));
  assert.match(verdict.explanation,/46:57.*49:32/);
  assert.equal(anneCorrectEvidence(entry,[...candidates.slice(0,2),{...candidates[2],times:new Set([2817])}],mapping,{state:'mismatch',label:'Abweichender Rang'}),null);
});

test('2020 and 2021 duplicate second places retain the difference and verified source', () => {
  for (const [event,category,people] of [
    [3401,'H45', [['WALDHÄUSL Wolfgang NF Wien','Wolfgang Waldhäusl','Naturfreunde Wien',2535],
      ['SRB Alexander WAT-OL','Alexander Srb','WAT-OL',3052],['MARKSTEINER Martin WAT-OL','Martin Marksteiner','WAT-OL',3144]]],
    [3054,'D55', [['GOLLMANN Birgit NF Wien','Birgit Gollmann','Naturfreunde Wien',1523],
      ['PAMLITSCHKA Herta WAT-OL','Herta Pamlitschka','WAT-OL',1712],['MICKO Roswitha OLC Wienerwald','Roswitha Micko','OLC Wienerwald',1785]]],
  ]) {
    const entry = {medals:people.map(([text],i)=>({rank:i===0?1:2,text}))};
    const candidates = people.map(([,name,club,time],i)=>({rank:i+1,club,names:new Set([name]),times:new Set([time])}));
    const map = {event,category}, comparison = compare(entry,candidates,map);
    assert.equal(comparison.state,'mismatch');
    assert.ok(anneCorrectEvidence(entry,candidates,map,comparison));
    candidates[2].times = new Set([0]);
    assert.equal(anneCorrectEvidence(entry,candidates,map,comparison),null);
  }
});

test('2019 H40 copied source error is user-confirmed only for the exact cell', () => {
  const entry={page:1,column:1,source_category:'H 40-',source_heading:'',
    discipline:'LANGDISTANZ - OL NACHWUCHS- und SENIOREN',
    event_heading:'Langdistanz MS: 13.10.2019; Nachw.+Sen MS: 31.3.2019',
    source_text:'1) VEITSBERGER Martin OLT Transdanubien\n2) BRETTNER-MESSLER Robert NF Wien',
    medals:[{rank:1,text:'VEITSBERGER Martin OLT Transdanubien'},{rank:2,text:'BRETTNER-MESSLER Robert NF Wien'}]};
  assert.match(userConfirmedWolvError(entry,2019).explanation,/Nutzerbestätigung/);
  assert.equal(userConfirmedWolvError(entry,2018),null);
  assert.equal(userConfirmedWolvError({...entry,source_category:'H 45-'},2019),null);
  assert.equal(userConfirmedWolvError({...entry,medals:[entry.medals[0]]},2019),null);
  const html=render({year:2019,entries:[entry]},null);
  assert.match(html,/✕ WOLV-Quellfehler bestätigt/);
  assert.match(html,/✓ ANNE korrekt/);
  assert.match(html,/Nutzerbestätigung/);
});

test('WOLV-based reconstruction is not presented as an independent match', () => {
  const match={state:'match',label:'Übereinstimmung'};
  assert.equal(markReconstructedComparison(match,[{wolvAgeSplit:true}]).label,'WOLV-Podium · Altersrekonstruktion');
  assert.equal(markReconstructedComparison(match,[{wolvAgeSplit:false}]).label,'Übereinstimmung');
  const conflict={state:'mismatch',label:'Abweichender Rang'};
  assert.equal(markReconstructedComparison(conflict,[{wolvAgeSplit:true}]),conflict);
});

test('Natalie and Natalia Machold are matched only for the confirmed club', () => {
  const person={club:'Naturfreunde Wien',names:new Set(['Natalia Machold'])};
  assert.ok(sourceMatches('MACHOLD Natalie NF Wien',person));
  assert.equal(sourceMatches('MACHOLD Natalie WAT-OL',{...person,club:'WAT-OL'}),false);
  assert.equal(sourceMatches('MACHOLD Nathalie NF Wien',person),false);
});

test('2020 WOLV Annika is matched to ANNE Anika for the confirmed club', () => {
  const person={club:'Naturfreunde Wien',names:new Set(['Anika Gassner'])};
  assert.ok(sourceMatches('GASSNER Annika NF Wien',person));
  assert.equal(sourceMatches('GASSNER Annika WAT-OL',{...person,club:'WAT-OL'}),false);
  assert.equal(sourceMatches('GASSNER Annik NF Wien',person),false);
});

test('2019 relay source typo is matched only for the confirmed full roster', () => {
  const team={club:'Naturfreunde Wien',names:new Set(['Tina Tiefenböck','Jasmina Gassner'])};
  assert.ok(sourceMatches('NF Wien: TIEFENBÖCK Tina / GASSNER Jasminai',team));
  assert.equal(sourceMatches('NF Wien: TIEFENBÖCK Rita / GASSNER Jasminai',team),false);
});

test('ambiguous H14 category requires one exact complete podium and ranks', () => {
  const entry={medals:[{rank:1,text:'HINTERPLATTNER Oskar MTV Hernals'},
    {rank:2,text:'URBANEK Lauri NF Wien'},{rank:3,text:'KOLAR Fabian NF Wien'}]};
  const candidates=[{rank:1,club:'MTV Hernals',names:new Set(['Oskar Hinterplattner'])},
    {rank:2,club:'Naturfreunde Wien',names:new Set(['Lauri Urbanek'])},
    {rank:3,club:'Naturfreunde Wien',names:new Set(['Fabian Kolar'])}];
  const row={event:3503,category:'H 14'};
  assert.equal(uniqueExactPodiumCandidate(entry,[row],()=>candidates),row);
  assert.equal(uniqueExactPodiumCandidate(entry,[row,{...row,event:2}],()=>candidates),null);
  assert.equal(uniqueExactPodiumCandidate({...entry,medals:entry.medals.slice(0,2)},[row],()=>candidates),null);
  assert.equal(uniqueExactPodiumCandidate(entry,[row],()=>candidates.map(c=>({...c,rank:4-c.rank}))),null);
});

test('cancelled heading is a source note only when there are no medals', () => {
  const base={page:1,column:1,source_category:'D 19-',source_heading:'',source_text:'',
    event_heading:'8.11.2020; wegen COVID abgesagt'};
  const note=render({entries:[{...base,medals:[]}]},null);
  assert.match(note,/kein ANNE-Abgleich">✓/);
  const actual=render({entries:[{...base,medals:[{rank:1,text:'Beispiel'}]}]},null);
  assert.doesNotMatch(actual,/kein ANNE-Abgleich">✓/);
});

test('unmatched source and reasons are retained and escaped', () => {
  const html = render({entries:[{page:1,column:4,source_category:'D 19-',source_heading:'',
    source_text:'keine Wertung\nda nur 3 Läuferinnen aus einem Verein (NF Wien) am Start (3)',medals:[]}]},null);
  assert.match(html,/keine Wertung/);
  assert.match(html,/nur 3 Läuferinnen aus einem Verein/);
  assert.doesNotMatch(html,/Vergleich nicht verfügbar|Nicht vergleichbar/);
  assert.match(html,/aria-label="Hinweis aus der WOLV-Quelle; kein ANNE-Abgleich">✓/);
  assert.doesNotMatch(html,/Tabellenzeile/);
  const notHeld = render({entries:[{page:1,column:4,source_category:'D 19-',source_heading:'',
    source_text:'nicht ausgetragen',medals:[]}]},null);
  assert.doesNotMatch(notHeld,/Vergleich nicht verfügbar|Nicht vergleichbar/);
  assert.match(notHeld,/kein ANNE-Abgleich">✓/);
  const noSeparate = render({entries:[{page:1,column:4,source_category:'D 19-',source_heading:'',
    source_text:'keine eigene Wertung, da nur 2 Läufer am Start',medals:[]}]},null);
  assert.match(noSeparate,/kein ANNE-Abgleich">✓/);
  assert.match(noSeparate,/nur 2 Läufer am Start/);
  const combined = render({year:2026,entries:[{page:1,column:2,source_category:'D -12 / H+D -12',source_heading:'',
    source_text:'D -12 zusammen mit H -12 als H+D -12 gewertet',medals:[]}]},null);
  assert.match(combined,/kein ANNE-Abgleich">✓/);
  assert.match(combined,/zusammen mit H -12 als H\+D -12 gewertet/);
  assert.doesNotMatch(combined,/Nicht vergleichbar|Vergleich nicht verfügbar|<ol class="wolv-medals">/);
});

test('historic years never reuse the pilot event IDs or invent pre-2013 ANNE matches', () => {
  const entry = {page:1,column:2,discipline:'MITTELDISTANZ - OL',source_category:'H 45-',
    source_heading:'H 45-:',event_heading:'24.5.2025; Mauerbach'};
  assert.equal(sourceMapping(entry,2025).event,null);
  assert.equal(sourceGroup({discipline:'SKI - OL'}),'ski');
  assert.equal(sourceGroup({discipline:'STAFFEL - OL',page:2}),'staffel');
  const fakeDb = (rows) => ({prepare:() => {
    let i=0; return {bind:()=>{},step:()=>i<rows.length,getAsObject:()=>rows[i++],free:()=>{}};
  }});
  assert.equal(historicalMapping(fakeDb([{event:4847,category:'H45-'}]),entry,2025,'mittel').event,4847);
  assert.equal(historicalMapping(fakeDb([{event:1,category:'H45-'},{event:2,category:'H45-'}]),entry,2025,'mittel').event,null);
  assert.equal(historicalMapping(fakeDb([{event:1,category:'H45-'}]),entry,2012,'mittel').event,null);
  const long = {source_category:'H 21- /\nH 19-',source_heading:'',
    event_heading:'Langdistanz MS : 28.9.2025; Purkersdorf / NÖ\nNachw.+Sen MS : 25.5.2025; Gablitz'};
  let bound;
  const longDb = {prepare:() => {let i=0; return {bind:(args)=>{bound=args;},
    step:()=>i<1,getAsObject:()=>{i++; return {event:4915,category:'H19'};},free:()=>{}};}};
  assert.deepEqual(historicalMapping(longDb,long,2025,'lang'),{group:'lang',event:4915,category:'H19'});
  assert.deepEqual(bound,['2025-09-28']);
  assert.equal(historicalMapping(fakeDb([{event:4915,category:'H17'}]),
    {...long,source_category:'H 21- / H 17-'},2025,'lang').event,null);
  const middle12 = {source_category:'H -12 / H+D -12',source_heading:'',event_heading:'24.5.2025; Mauerbach'};
  assert.equal(historicalMapping(fakeDb([{event:4847,category:'DH-12'}]),middle12,2025,'mittel').category,'DH-12');
  const long12 = {source_category:'H -12 / H+D -12',source_heading:'',
    event_heading:'Langdistanz MS : 21.4.2024; Höflein\nNachw.+Sen MS : 8.9.2024; Saubersdorf'};
  let selectedDate;
  const long12Db = {prepare:() => {let i=0; return {bind:(args)=>{selectedDate=args;},
    step:()=>i<1,getAsObject:()=>{i++; return {event:4589,category:'DH-12'};},free:()=>{}};}};
  assert.deepEqual(historicalMapping(long12Db,long12,2024,'lang'),{group:'lang',event:4589,category:'DH-12'});
  assert.deepEqual(selectedDate,['2024-09-08']);
  assert.equal(historicalMapping(fakeDb([{event:3,category:'H-12'}]),
    {...long12,event_heading:'1.9.2023; Ort'},2023,'lang').category,'H-12');
  assert.equal(historicalMapping(fakeDb([{event:3,category:'D-12'}]),
    {...long12,event_heading:'1.9.2023; Ort'},2023,'lang').event,null);
  assert.equal(historicalMapping(fakeDb([{event:3953,category:'D und Herren -12'}]),
    {...long12,event_heading:'9.9.2023; Wolkersdorf'},2023,'sprint').category,'D und Herren -12');
  const sprint = {source_category:'D -12 / H+D -12',source_heading:'',event_heading:'10.5.2025; Seestadt'};
  assert.equal(historicalMapping(fakeDb([{event:4879,category:'DH -12'}]),sprint,2025,'sprint').event,4879);
  assert.equal(historicalMapping(fakeDb([{event:4879,category:'H-14'}]),
    {...sprint,source_category:'H 13-14'},2025,'sprint').category,'H-14');
  assert.equal(historicalMapping(fakeDb([{event:4879,category:'D-14'}]),
    {...sprint,source_category:'D 13-14'},2025,'sprint').category,'D-14');
  assert.equal(historicalMapping(fakeDb([{event:3562,category:'H 13 bis 14'}]),
    {...sprint,event_heading:'26.3.2022; Rust',source_category:'H 13-14'},2022,'sprint').category,'H 13 bis 14');
  assert.equal(historicalMapping(fakeDb([{event:1,category:'D-14'}]),
    {...sprint,event_heading:'10.5.2024; Seestadt',source_category:'H 13-14'},2024,'sprint').event,null);
  const night = {source_category:'H 45-',source_heading:'H 45-:',event_heading:'11.10.2025; St. Lorenzen'};
  assert.equal(historicalMapping(fakeDb([{event:4963,category:'H40-,H45'}]),night,2025,'nacht').category,'H40-,H45');
  assert.equal(historicalMapping(fakeDb([{event:2,category:'H40-,H45'}]),
    {...night,event_heading:'11.10.2024'},2024,'nacht').event,null);
  const night2023 = {source_category:'H 15-18',source_heading:'H 15-18:',event_heading:'22.4.2023; Klosterneuburg'};
  assert.equal(historicalMapping(fakeDb([{event:3584,category:'H-18'}]),night2023,2023,'nacht').event,null);
  assert.equal(historicalMapping(fakeDb([{event:3584,category:'H-18'}]),
    {...night2023,event_heading:'22.4.2022'},2022,'nacht').event,null);
});

test('15–18 fallback requires one unique complete podium and preserves rank conflicts', () => {
  const medals = [{rank:1,text:'PISKORZ Kiara-Sophie WAT-OL'}, {rank:2,text:'RADON Livia NF Wien'},
    {rank:3,text:'BIEL Corinna NF Wien'}];
  const candidates = [
    {rank:1,club:'WAT-OL',names:new Set(['Kiara-Sophie Piskorz'])},
    {rank:2,club:'Naturfreunde Wien',names:new Set(['Livia Radon'])},
    {rank:3,club:'Naturfreunde Wien',names:new Set(['Corinna Biel'])}];
  const rows = [{event:3812,category:'D -18'}];
  assert.equal(uniqueAgeRangeCandidate({medals},rows,()=>candidates),rows[0]);
  assert.equal(uniqueAgeRangeCandidate({medals},rows,()=>[...candidates.slice(0,2),{...candidates[2],rank:2}]),rows[0]);
  assert.equal(uniqueAgeRangeCandidate({medals},[...rows,{event:1,category:'D -18'}],()=>candidates),null);
  assert.equal(uniqueAgeRangeCandidate({medals},rows,()=>candidates.slice(0,2)),null);
  assert.equal(uniqueAgeRangeCandidate({medals:medals.slice(0,1)},rows,()=>candidates.slice(0,1)),null);
  assert.equal(uniqueAgeRangeCandidate({medals},rows,()=>[{...candidates[0],names:new Set(['Andere Person'])},...candidates.slice(1)]),null);
});

test('15–18 podium maps to −18 in either gender and year, but not for relays', () => {
  const medals = [{rank:1,text:'PISKORZ Kiara-Sophie WAT-OL'}, {rank:2,text:'RADON Livia NF Wien'},
    {rank:3,text:'BIEL Corinna NF Wien'}];
  const podium = [{id:1,rank:1,club:'WAT-OL',name:'Kiara-Sophie Piskorz'},
    {id:2,rank:2,club:'Naturfreunde Wien',name:'Livia Radon'},
    {id:3,rank:3,club:'Naturfreunde Wien',name:'Corinna Biel'}];
  const fake = (gender) => ({prepare(sql) { let i=0;
    const data = sql.includes('SELECT DISTINCT s.event_id') ? [{event:3812,category:`${gender} -18`}]
      : sql.includes('SELECT ci.id,ci.category') ? [{id:7,category:`${gender} -18`}]
      : sql.includes('COUNT(*) AS n') ? [{n:3}] : sql.includes('SELECT ce.id,ce.regional_rank') ? podium : [];
    return {bind(){},step(){return i<data.length},getAsObject(){return data[i++]},free(){}};
  }});
  for (const year of [2013,2019,2022,2023,2025]) for (const gender of ['H','D']) {
    const entry = {source_category:`${gender} 15-18`,source_heading:'',event_heading:`18.9.${year}`,medals};
    assert.equal(historicalMapping(fake(gender),entry,year,'lang').category,`${gender} -18`);
    assert.equal(historicalMapping(fake(gender),entry,year,'staffel').event,null);
  }
});

test('2023 OLT relay matches the evidenced Ginzel spelling variant only for the same full roster', () => {
  const candidate = {rank:2,club:'OLT Transdanubien',names:['Martin Veitsberger','Elisabeth Gaudernak','Marianne Ginzel']};
  assert.ok(sourceMatches('OLT Transdanubien: VEITSBERGER Martin - GAUDERNAK Elisabeth - - GINTZEL Marianne',candidate));
  assert.equal(sourceMatches('OLT Transdanubien: VEITSBERGER Martin - GAUDERNAK Elisabeth - GINTZEL Maria',candidate),false);
  assert.equal(sourceMatches('WAT-OL: VEITSBERGER Martin - GAUDERNAK Elisabeth - GINTZEL Marianne',candidate),false);
});

test('2023 source name without second given name matches the evidenced ANNE identity', () => {
  const candidate = {rank:2,club:'Naturfreunde Wien',names:['Robert Viktor Misko']};
  assert.ok(sourceMatches('MISKO Robert NF Wien', candidate));
  assert.equal(sourceMatches('MISKO Robert WAT-OL', candidate), false);
  assert.equal(sourceMatches('MISKO Richard NF Wien', candidate), false);
});

test('2022 Vienna relay compares the documented Riki/Rikki spelling without merging identities', () => {
  const team = {rank:2,club:'Naturfreunde Wien',names:['Marina Skern','Rikki Tiefenböck']};
  assert.ok(sourceMatches('NF Wien: SKERN Marina - TIEFENBÖCK Riki',team));
  assert.equal(sourceMatches('NF Wien: SKERN Anna - TIEFENBÖCK Riki',team),false);
});

test('dash-only WOLV cells are source notes, not claims of an ANNE match', () => {
  const html = render({year:2023,entries:[{discipline:'NACHT - OL',column:4,source_category:'H 80-',source_heading:'H 80-:',
    event_heading:'22.4.2023',source_text:'- - - - - - - - - -',medals:[]}]},null);
  assert.match(html,/kein ANNE-Abgleich">✓/);
  assert.doesNotMatch(html,/Nicht vergleichbar|Übereinstimmung mit ANNE<\/span>/);
});
