/* WOLV is an independent source. Never create an ANNE medal from this file. */
"use strict";
const groups = [
  {key:"lang", title:"Langdistanz · Nachwuchs & Senioren"},
  {key:"tag", title:"Tag-OL"},
  {key:"mittel", title:"Mitteldistanz"},
  {key:"sprint", title:"Sprint"},
  {key:"nacht", title:"Nacht"},
  {key:"staffel", title:"Staffel"},
  {key:"mixed", title:"Mixed-Sprintstaffel"},
  {key:"ski", title:"Ski-OL"},
  {key:"sonstige", title:"Weitere Wertungen"},
];
const rowKeys = {
  'H-12/H+D-12':'H-12', 'D-12/H+D-12':'D-12', 'H13-14':'H-14', 'D13-14':'D-14',
  'H15-16':'H15', 'D15-16':'D15', 'H19-20':'H19', 'H21-/H19-':'H19',
  'H15-18':'H-18', 'D15-18':'D-18',
  'D19-':'D19', 'H35-':'H35', 'D35-':'D35', 'H45-':'H45', 'D45-':'D45',
  'H50-':'H50', 'D50-':'D50', 'H55-':'H55', 'D55-':'D55',
  'H60-':'H60', 'D60-':'D60', 'H65-':'H65', 'D65-':'D65',
  'H75-':'H75', 'D40-':'D80',
};
const mixedKeys = {'Nachwuchs-16:':'Mixed bis 16','AllgemeineKategorie:':'Mixed ab 17',
  'Senioren35-:':'Mixed ab 35','Senioren50-:':'Mixed ab 50'};
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const compact = (s) => String(s || '').replace(/\s+/g,'');
const tokens = (s) => String(s || '').toLocaleLowerCase('de-AT').replace(/ß/g,'ss')
  .normalize('NFD').replace(/[\u0300-\u036f]/g,'').match(/[a-z0-9]+/g) || [];
const bag = (s) => tokens(s).sort().join(' ');

function sourceGroup(entry) {
  const heading = String(entry.discipline || '').toUpperCase();
  if (/MIXED/.test(heading) && /STAFFEL/.test(heading)) return 'mixed';
  if (/STAFFEL/.test(heading)) return 'staffel';
  if (/NACHT/.test(heading)) return 'nacht';
  if (/MITTEL/.test(heading)) return 'mittel';
  if (/SPRINT/.test(heading)) return 'sprint';
  if (/SKI/.test(heading)) return 'ski';
  if (/LANGDISTANZ/.test(heading)) return 'lang';
  if (/TAG\s*-\s*OL/.test(heading)) return 'tag';
  return ['', 'lang','mittel','sprint','nacht','staffel'][entry.column] || 'sonstige';
}

function sourceMapping(entry, year = 2026) {
  const row = compact(entry.source_category);
  if (year !== 2026) return {group:sourceGroup(entry),event:null,category:null};
  if (entry.page === 2) return {group:'mixed', event:5382, category:mixedKeys[row]};
  const group = ['', 'lang','mittel','sprint','nacht','staffel'][entry.column];
  let category = rowKeys[row];
  const event = group === 'lang' ? (row === 'H21-/H19-' || row === 'D19-' ? 5247 : 5496)
    : {mittel:5391,sprint:5357,nacht:5370,staffel:5495}[group];
  if (group === 'lang' || group === 'nacht') {
    if (category === 'H15') category = 'H-18';
    if (category === 'D15') category = 'D-18';
    if (group === 'nacht') {
      if (category === 'H-12') category = 'H-14';
      if (category === 'D-12') category = 'D-14';
      if (category === 'H-18') category = 'H15-18';
      if (category === 'D-18') category = 'D15-18';
    }
  }
  if (group === 'mittel' && (category === 'H-12' || category === 'D-12')) category = 'DH-12';
  if (group === 'staffel') {
    category = ({'H-12':'H-14','H50':'H150','H60':'H180','D40':'D80','D50':'D100','D60':'D120'})[category] || category;
  }
  if (group === 'mixed') category = mixedKeys[row];
  return {group,event,category};
}

function query(db, sql, params) {
  const stmt = db.prepare(sql);
  try { stmt.bind(params); const rows=[]; while (stmt.step()) rows.push(stmt.getAsObject()); return rows; }
  finally { stmt.free(); }
}

// Categories differ in punctuation, but not in age boundaries. Never use a
// fuzzy age/name match to decide that two championships are identical.
function categoryKey(value) {
  const key = compact(value).toUpperCase().replace(/[–—]/g,'-').replace(/-$/,'');
  const range = key.match(/^(D|H)(AB|BIS)(\d+)$/);
  return range ? `${range[1]}${range[2] === 'BIS' ? '-' : ''}${range[3]}` : key;
}

function jointUnder12(value) {
  const key = compact(value).toUpperCase().replace(/[–—]/g,'-').replace(/BIS12/,'-12');
  return /^(?:DH|D(?:AMEN)?(?:UND|\+|&)H(?:ERREN)?|H(?:ERREN)?(?:UND|\+|&)D(?:AMEN)?)-?12-?$/.test(key);
}

function under14Alternative(value, gender) {
  const key = compact(value).toUpperCase().replace(/[–—]/g,'-');
  return key === `${gender}-14` || key === `${gender}13-14` || key === `${gender}13BIS14`;
}

// A range 15–18 is not intrinsically identical to a class "bis 18".
// The latter can include younger runners or combine age groups. Only use it
// as a fallback when the same dated race has the same complete podium.
function uniqueAgeRangeCandidate(entry, rows, candidatesFor) {
  if (!entry.medals || entry.medals.length < 2) return null;
  const matching = rows.filter((row) => {
    const candidates = candidatesFor(row);
    if (!candidates || candidates.length !== entry.medals.length) return false;
    const matches = entry.medals.map((medal) => candidates.filter((candidate) => sourceMatches(medal.text,candidate)));
    return matches.every((items) => items.length === 1)
      && new Set(matches.map((items) => items[0])).size === entry.medals.length;
  });
  return matching.length === 1 ? matching[0] : null;
}

function uniqueExactPodiumCandidate(entry, rows, candidatesFor) {
  if (entry.medals?.length !== 3) return null;
  const exact = rows.filter((row) => compare(entry,candidatesFor(row),row).state === 'match');
  return exact.length === 1 ? exact[0] : null;
}

// These are source-attested names for the four WOLV mixed-sprint classes.
// Explicit aliases avoid confusing a different age class (e.g. Mix -12).
function mixedCategoryKey(value) {
  const key = compact(value).toUpperCase().replace(/[–—]/g,'-').replace(/[.:]/g,'');
  if (/^(?:(?:MIX|MIXED))?(?:NACHWUCHS-?16|BIS-?16|-16)$/.test(key)) return 'youth16';
  if (/^(?:(?:MIX|MIXED))?(?:ALLGEMEIN(?:E(?:KATEGORIE|KAT)?)?(?:17-?)?|AB17-?)$/.test(key)) return 'open';
  if (/^(?:(?:MIX|MIXED))?(?:SENIOREN(?:AB)?|AB)(35|50)-?$/.test(key)) return `senior${key.match(/(35|50)/)[1]}`;
  return null;
}

// Historical comparisons are attempted only when a source date and exact
// class identify one confirmed Vienna championship instance. Ambiguity stays ?.
function historicalMapping(db, entry, year, group) {
  if (year < 2013 || !db) return {group,event:null,category:null};
  const dates = [...String(entry.event_heading || '').matchAll(/\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b/g)]
    .map((m) => `${m[3]}-${m[2].padStart(2,'0')}-${m[1].padStart(2,'0')}`)
    .filter((date) => date.startsWith(`${year}-`));
  const distinct = [...new Set(dates)];
  let source = String(entry.source_heading || entry.source_category || '').replace(/:$/,'').trim();
  if (group === 'mixed') {
    const sourceKey = mixedCategoryKey(source);
    if (!sourceKey || !distinct.length) return {group,event:null,category:null};
    const placeholders = distinct.map(() => '?').join(',');
    const rows = query(db, `SELECT DISTINCT s.event_id AS event,ci.category,e.title AS event_title,s.title AS stage_title
      FROM championship_instance ci JOIN stage s ON s.id=ci.stage_id JOIN event e ON e.id=s.event_id
      WHERE ci.jurisdiction='WIEN' AND ci.championship_type='LMS' AND ci.state='confirmed'
      AND substr(COALESCE(s.date,e.date_from),1,10) IN (${placeholders})`,distinct)
      .filter((row) => mixedCategoryKey(row.category) === sourceKey
        && /mixed|\bmix\b|sprint\s*(?:staffel|relay)/i.test(`${row.event_title || ''} ${row.stage_title || ''}`));
    return rows.length === 1 ? {group,event:rows[0].event,category:rows[0].category}
      : {group,event:null,category:null};
  }
  // The WOLV's long-distance heading explicitly combines the old H21 and
  // newer H19 names for this one championship class. Do not generalize this
  // to other combined age groups, which may represent different contests.
  if (group === 'lang' && compact(source).toUpperCase().replace(/–/g,'-') === 'H21-/H19-') source = 'H19';
  // Source-verified 2025 WOLV classes refer to these combined / renamed
  // Vienna classifications. Restrict aliases to their actual event dates;
  // the same headings can describe different age splits in other years.
  const rawCategory = compact(source).toUpperCase().replace(/–/g,'-');
  // A WOLV row labelled H/D-12 / H+D-12 can mean either a separate
  // girls/boys class or a joint class, depending on the particular race.
  // Let the confirmed ANNE categories (and, when needed, podiums) decide;
  // never apply the same assumption to night or relay age classes.
  const combinedUnder12 = ['lang','mittel','sprint'].includes(group)
    ? rawCategory.match(/^(H|D)-12\/H\+D-12$/) : null;
  if (combinedUnder12) source = `${combinedUnder12[1]}-12`;
  const under14 = rawCategory.match(/^(H|D)13-14$/);
  if (!distinct.length || !/^(?:DH|D|H)\s*[-\d\s]+$/i.test(source))
    return {group,event:null,category:null};
  // The 2025 night championship explicitly joins H40 and H45 in ANNE;
  // WOLV calls this same classification H45. Do not split the results.
  let targetCategory = year === 2025 && group === 'nacht' && distinct.includes('2025-10-11')
    && categoryKey(source) === 'H45' ? 'H40-,H45' : source;
  let selected = distinct;
  if (group === 'lang' && distinct.length === 2) {
    selected = [distinct[/^(?:D|H)\s*(?:19|21)\s*-?$/i.test(source) ? 0 : 1]];
  }
  const placeholders = selected.map(() => '?').join(',');
  const allRows = query(db, `SELECT DISTINCT s.event_id AS event,ci.category FROM championship_instance ci
    JOIN stage s ON s.id=ci.stage_id JOIN event e ON e.id=s.event_id
    WHERE ci.jurisdiction='WIEN' AND ci.championship_type='LMS' AND ci.state='confirmed'
    AND substr(COALESCE(s.date,e.date_from),1,10) IN (${placeholders})`,selected);
  const rows = allRows.filter((row) => categoryKey(row.category) === categoryKey(targetCategory)
      || (combinedUnder12 && jointUnder12(row.category))
      || (under14 && under14Alternative(row.category, under14[1])));
  if ((combinedUnder12 || under14) && rows.length > 1 && entry.medals?.length) {
    const exact = rows.filter((row) => {
      const candidate = {group,event:row.event,category:row.category};
      return compare(entry,regionalEntries(db,candidate),candidate).state === 'match';
    });
    return exact.length === 1 ? {group,event:exact[0].event,category:exact[0].category}
      : {group,event:null,category:null};
  }
  if (rows.length === 1) return {group,event:rows[0].event,category:rows[0].category};
  if (!rows.length && ['lang','tag','mittel','sprint','nacht','ski'].includes(group)) {
    // Some historical ANNE exports omit the minus sign ("H 14"). It is
    // ambiguous by itself, so accept it only with the exact complete podium
    // and rank order from the dated WOLV classification.
    const under14NoSign = rawCategory.match(/^(H|D)-14$/);
    if (under14NoSign && entry.medals?.length === 3) {
      const aliases = allRows.filter((row) => categoryKey(row.category) === `${under14NoSign[1]}14`);
      const chosen = uniqueExactPodiumCandidate(entry,aliases,(row) =>
        regionalEntries(db,{group,event:row.event,category:row.category}));
      if (chosen) return {group,event:chosen.event,category:chosen.category};
    }
    const ageRange = rawCategory.match(/^(H|D)15-18$/);
    if (ageRange) {
      const aliases = allRows.filter((row) => categoryKey(row.category) === `${ageRange[1]}-18`);
      const chosen = uniqueAgeRangeCandidate(entry,aliases,(row) =>
        regionalEntries(db,{group,event:row.event,category:row.category}));
      if (chosen) return {group,event:chosen.event,category:chosen.category};
    }
  }
  return {group,event:null,category:null};
}
function regionalEntries(db, mapping) {
  if (!mapping.event || !mapping.category) return null;
  const instances = query(db, `SELECT ci.id,ci.category,ci.detection_basis FROM championship_instance ci
    JOIN stage s ON s.id=ci.stage_id WHERE s.event_id=? AND ci.jurisdiction='WIEN'
    AND ci.championship_type='LMS' AND ci.state='confirmed'`,[mapping.event])
    .filter((r) => categoryKey(r.category) === categoryKey(mapping.category));
  if (instances.length !== 1) return null;
  const id = instances[0].id;
  if (query(db,'SELECT 1 FROM regional_award_hold WHERE championship_instance_id=?',[id]).length) return null;
  const n = query(db, `SELECT COUNT(*) AS n FROM championship_entry ce WHERE ce.championship_instance_id=?
    AND ce.eligibility_state='eligible' AND EXISTS (SELECT 1 FROM championship_entry_result cer
    JOIN result r ON r.id=cer.result_id WHERE cer.championship_entry_id=ce.id
    AND r.out_of_competition=0 AND r.status IN ('ok','dnf','mp','dsq'))`,[id])[0].n;
  if (n < 3) return null;
  const rows = query(db, `SELECT ce.id,ce.regional_rank AS rank,r.official_club AS club,
      COALESCE(p.name,r.observed_name) AS name,r.observed_name,r.team_number,r.time_s
    FROM championship_entry ce JOIN championship_entry_result cer ON cer.championship_entry_id=ce.id
    JOIN result r ON r.id=cer.result_id LEFT JOIN person p ON p.id=r.person_id
    WHERE ce.championship_instance_id=? AND ce.eligibility_state='eligible' AND ce.regional_rank BETWEEN 1 AND 3`,[id]);
  const byEntry = new Map();
  for (const row of rows) {
    if (!row.name || !row.club) return null;
    if (!byEntry.has(row.id)) byEntry.set(row.id,{rank:row.rank,club:row.club,names:new Set(),times:new Set(),
      wolvAgeSplit:instances[0].detection_basis === 'wolv-age-split'});
    if (byEntry.get(row.id).club !== row.club) return null;
    byEntry.get(row.id).names.add(row.name);
    if (row.time_s != null) byEntry.get(row.id).times.add(row.time_s);
  }
  return [...byEntry.values()];
}

const clubs = {
  'Naturfreunde Wien':['NF Wien','Naturfreunde Wien'],
  'WAT-OL':['WAT-OL','WAT OL','WAT'],
  'OLC Wienerwald':['OLC Wienerwald'],
  'OLT Transdanubien':['OLT Transdanubien'],
  'OLG Ströck Wien':['OLG Ströck Wien'],
  'MTV Hernals':['MTV Hernals'],
};
function sourceMatches(source, entry) {
  const variants = clubs[entry.club] || [entry.club];
  const names = [...entry.names].join(' ');
  // User-confirmed alias, restricted to the confirmed person and club. WOLV
  // still uses the older name even after results point to ÖFOL-ID 2314.
  const nameVariants = [names];
  // WOLV's annual list calls the ANNE-result Natalia Machold Natalie.
  // Match this confirmed person only, without changing either source name.
  if (entry.club === 'Naturfreunde Wien' && [...entry.names].some((name) => bag(name) === bag('Natalia Machold'))) {
    nameVariants.push(names.replace(/\bNatalia\s+Machold\b/i, 'Natalie Machold'));
  }
  // In the 2020 Wiener middle-distance results ANNE identifies Anika
  // Gassner; WOLV's podium spells her given name Annika.
  if (entry.club === 'Naturfreunde Wien' && [...entry.names].some((name) => bag(name) === bag('Anika Gassner'))) {
    nameVariants.push(names.replace(/\bAnika\s+Gassner\b/i, 'Annika Gassner'));
  }
  // The 2019 WOLV relay table appends a stray i to Jasmina's name. Do not
  // rewrite the source: compare only the complete confirmed club roster.
  if (entry.club === 'Naturfreunde Wien' && [...entry.names].some((name) => bag(name) === bag('Jasmina Gassner'))) {
    nameVariants.push(names.replace(/\bJasmina\s+Gassner\b/i, 'Jasminai Gassner'));
  }
  if (entry.club === 'WAT-OL' && [...entry.names].some((name) => bag(name) === bag('Franz Wareyka-Glaner'))) {
    nameVariants.push(names.replace(/\bFranz\s+Wareyka-Glaner\b/i, 'Franz Glaner'));
  }
  // ANNE's event roster and ÖFOL ID 1299 use Ginzel; WOLV's 2023
  // mixed-relay annual list spells this same OLT team member Gintzel.
  if (entry.club === 'OLT Transdanubien' && [...entry.names].some((name) => bag(name) === bag('Marianne Ginzel'))) {
    nameVariants.push(names.replace(/\bMarianne\s+Ginzel\b/i, 'Marianne Gintzel'));
  }
  // ANNE's 2023 source prints Robert Misko; the linked ANNE registry
  // identity (ÖFOL-ID 9679) includes his second given name, Viktor.
  if (entry.club === 'Naturfreunde Wien' && [...entry.names].some((name) => bag(name) === bag('Robert Viktor Misko'))) {
    nameVariants.push(names.replace(/\bRobert\s+Viktor\s+Misko\b/i, 'Robert Misko'));
  }
  // 2022 relay ANNE spells this runner Rikki, the WOLV roster Riki. The
  // alias is comparison-only and still requires the complete roster and club.
  if (entry.club === 'Naturfreunde Wien' && [...entry.names].some((name) => bag(name) === bag('Rikki Tiefenböck'))) {
    nameVariants.push(names.replace(/\bRikki\s+Tiefenböck\b/i, 'Riki Tiefenböck'));
  }
  // The optional number before ':' is WOLV's team label, not a person's
  // name. Identical rosters are still rejected by the ambiguity check below.
  const withoutTeamLabel = source.replace(/^(.*?)(?:\s+\d+)\s*:/,'$1:');
  return variants.some((club) => nameVariants.some((variant) => bag(withoutTeamLabel) === bag(`${club} ${variant}`)));
}
function candidateDescription(candidate) {
  return `OLResults Platz ${candidate.rank}: ${[...candidate.names].join(' / ')} (${candidate.club})`;
}
function knownSourceConflict(entry, candidates, mapping) {
  // Independently documented, source-pinned conflicts: do not collapse these
  // different people into a guessed match. The ANNE roster remains binding.
  const conflicts = [
    {event:5391, category:'H-14', wolv:'IMRISKA Adam NF Wien', anne:'IMRISKA Albert NF Wien',
      description:'WOLV: Adam Imriska; OLResults/ANNE: Albert Imriska'},
    {event:5495, category:'D15', wolv:'NF Wien: TIEFENBÖCK Tina - RADON Livia',
      anne:'NF Wien: TIEFENBÖCK Tina - URBANEK Annina',
      description:'WOLV: Tina Tiefenböck / Livia Radon; OLResults/ANNE: Tina Tiefenböck / Annina Urbanek'},
  ];
  return conflicts.find((conflict) => mapping?.event === conflict.event
    && categoryKey(mapping.category) === categoryKey(conflict.category)
    && entry.medals.length === 3 && candidates.length === 3
    && bag(entry.medals.find((m) => m.rank === 1)?.text) === bag(conflict.wolv)
    && candidates.some((c) => c.rank === 1 && sourceMatches(conflict.anne,c))
    && [2,3].every((rank) => {
      const source = entry.medals.find((m) => m.rank === rank);
      return source && candidates.some((c) => c.rank === rank && sourceMatches(source.text,c));
    }));
}
function compare(entry, candidates, mapping = null) {
  if (!entry.medals.length) return {state:'unknown', label:'Nicht vergleichbar'};
  if (!candidates) return {state:'unknown', label:'Nicht vergleichbar', differences:[mapping?.event
    ? 'Die zugeordnete OLResults-Wertung ist für den automatischen Vergleich nicht verfügbar.'
    : 'Bewerb oder Kategorie konnten noch nicht eindeutig einer OLResults-Wertung zugeordnet werden.']};
  if (!candidates.length) return {state:'unknown', label:'Nicht vergleichbar',
    differences:['Für diese zugeordnete OLResults-Wertung wurden keine vergleichbaren Medaillenplätze gefunden.']};
  const conflict = knownSourceConflict(entry,candidates,mapping);
  if (conflict) return {state:'mismatch',label:'Abweichende Personen',differences:[conflict.description]};
  // Names, team roster and club must all match before a mismatch is asserted.
  // Unmatched OCR, abbreviations, missing source places and ambiguous teams
  // remain unknown rather than manufacturing a red cross.
  const matches = entry.medals.map((m) => ({source:m, matches:candidates.filter((c) => sourceMatches(m.text,c))}));
  if (matches.some((m) => m.matches.length !== 1)) {
    const differences = matches.filter((m) => m.matches.length !== 1).flatMap((m) => {
      const options = m.matches.length ? m.matches : candidates.filter((c) => c.rank === m.source.rank);
      return [`WOLV Platz ${m.source.rank}: ${m.source.text}`,
        ...(options.length ? options.map(candidateDescription) : ['OLResults: Kein vergleichbarer Eintrag auf diesem Rang.'])];
    });
    return {state:'unknown', label:'Nicht eindeutig zuordenbar',differences};
  }
  const seen = new Set(matches.map((m) => m.matches[0]));
  if (seen.size !== matches.length) return {state:'unknown', label:'Nicht eindeutig zuordenbar',
    differences:['Mehrere WOLV-Einträge passen zum selben OLResults-Eintrag; die Zuordnung ist nicht eindeutig.']};
  const differences = matches.filter((m) => m.source.rank !== m.matches[0].rank)
    .map((m) => `${m.source.text}: WOLV Platz ${m.source.rank}, OLResults Platz ${m.matches[0].rank}`);
  if (differences.length) return {state:'mismatch',label:'Abweichender Rang',differences};
  if (candidates.length !== entry.medals.length) {
    // Only treat extra OLResults medals as a definite difference if the
    // WOLV source explicitly includes its complete podium through place 3.
    if (candidates.length > entry.medals.length && /^\s*3\s*\)/m.test(entry.source_text || '')) {
      const extras = candidates.filter((candidate) => !seen.has(candidate));
      return {state:'mismatch', label:'Zusätzliche ANNE-Medaille', differences:extras.map((candidate) =>
        `Nur OLResults: Platz ${candidate.rank} · ${[...candidate.names].join(' / ')} (${candidate.club})`)};
    }
    return {state:'unknown',label:'Unvollständig vergleichbar',differences:candidates.filter((c) => !seen.has(c)).map(candidateDescription)};
  }
  return {state:'match',label:'Übereinstimmung'};
}

// A separate, source-verified verdict does not erase the WOLV/ANNE conflict.
// Pin it to both podiums and times so changes invalidate the confirmation.
function anneCorrectEvidence(entry, candidates, mapping, comparison) {
  if (comparison.state !== 'mismatch' || comparison.label !== 'Abweichender Rang'
      || entry.medals.length !== 3 || !candidates || candidates.length !== 3) return null;
  const cases = [
    {event:3953,category:'D15',url:'https://anne-cdn.oefol.at/public/legacy/event_3953_2023-09-02-wiener-und-n-sprint-ergebnisse.html',
      explanation:'ANNE bestätigt Tina Tiefenböck auf Rang 2 (15:25) und Anna Simkovics auf Rang 3 (15:34).',
      podium:[{wolv:1,anne:1,source:'GASSNER Jasmina NF Wien',time:897},
        {wolv:2,anne:2,source:'TIEFENBÖCK Tina NF Wien',time:925},{wolv:2,anne:3,source:'SIMKOVICS Anna OLC Wienerwald',time:934}]},
    {event:3847,category:'H45',url:'https://anne-cdn.oefol.at/public/legacy/event_3847_erg081022.pdf',
      explanation:'ANNE und die Wiener Tageswertung bestätigen Hartmut Schneider auf Rang 2 (46:57) und Günther Kroupa auf Rang 3 (49:32).',
      podium:[{wolv:1,anne:1,source:'WALDHÄUSL Wolfgang NF Wien',time:2294},
        {wolv:2,anne:2,source:'SCHNEIDER Hartmut MTV Hernals',time:2817},{wolv:2,anne:3,source:'KROUPA Günther NF Wien',time:2972}]},
    {event:3812,category:'D45',url:'https://anne-cdn.oefol.at/public/legacy/event_3812_20220918-n-w-b-ju.html',
      explanation:'ANNE-Zeiten bestätigen Herlinde Hinterplattner auf Rang 2 (58:58) und Elisabeth Gaudernak auf Rang 3 (1:03:07); die WOLV-Jahresliste weist beide als Rang 2 aus.',
      podium:[{wolv:1,anne:1,source:'SKERN Marina NF Wien',time:2752},
        {wolv:2,anne:2,source:'HINTERPLATTNER Herlinde MTV Hernals',time:3538},{wolv:2,anne:3,source:'GAUDERNAK Elisabeth OLT Transdanubien',time:3787}]},
    {event:3401,category:'H45',url:'https://anne-cdn.oefol.at/public/legacy/event_3401_ergebnisliste-wr-nacht-ms-gpl-grossmittel-2021-10-08.pdf',
      explanation:'Die Wiener Tageswertung auf ANNE bestätigt Alexander Srb auf Rang 2 (50:52) und Martin Marksteiner auf Rang 3 (52:24).',
      podium:[{wolv:1,anne:1,source:'WALDHÄUSL Wolfgang NF Wien',time:2535},
        {wolv:2,anne:2,source:'SRB Alexander WAT-OL',time:3052},{wolv:2,anne:3,source:'MARKSTEINER Martin WAT-OL',time:3144}]},
    {event:3054,category:'D55',url:'https://anne-cdn.oefol.at/public/legacy/event_3054_ergebnis.pdf',
      explanation:'ANNE bestätigt Herta Pamlitschka auf Rang 2 (28:32) und Roswitha Micko auf Rang 3 (29:45).',
      podium:[{wolv:1,anne:1,source:'GOLLMANN Birgit NF Wien',time:1523},
        {wolv:2,anne:2,source:'PAMLITSCHKA Herta WAT-OL',time:1712},{wolv:2,anne:3,source:'MICKO Roswitha OLC Wienerwald',time:1785}]},
  ];
  const verified = cases.find((item) => mapping?.event === item.event && categoryKey(mapping.category) === item.category);
  if (!verified || !verified.podium.every((v) => entry.medals.some((m) => m.rank === v.wolv && bag(m.text) === bag(v.source))
      && candidates.some((c) => c.rank === v.anne && sourceMatches(v.source,c) && c.times?.has(v.time)))) return null;
  return {url:verified.url, explanation:verified.explanation};
}

// User confirmed that this exact annual-list cell was copied from 2018.
// It is not an ANNE ranking match; keep the WOLV source error visible.
function userConfirmedWolvError(entry, year) {
  if (year !== 2019 || sourceGroup(entry) !== 'lang'
      || categoryKey(entry.source_category) !== 'H40'
      || !String(entry.event_heading || '').includes('31.3.2019')
      || entry.medals?.length !== 2) return null;
  const names = [[1,'VEITSBERGER Martin OLT Transdanubien'],
                 [2,'BRETTNER-MESSLER Robert NF Wien']];
  if (!names.every(([rank,name]) => entry.medals.some((m) => m.rank === rank && bag(m.text) === bag(name)))) return null;
  return {url:'https://anne-cdn.oefol.at/public/legacy/event_2522_ergebnis-langdistanz.pdf',
    explanation:'Nutzerbestätigung: Die H40-Zeile wurde aus der WOLV-Jahresliste 2018 übernommen und gilt 2019 nicht. Der WOLV-Originaltext bleibt sichtbar; keine H40-Medaille wird daraus erzeugt.'};
}

function markReconstructedComparison(comparison, candidates) {
  if (comparison.state !== 'match' || !candidates?.some((candidate) => candidate.wolvAgeSplit)) return comparison;
  return {...comparison,label:'WOLV-Podium · Altersrekonstruktion'};
}

function render(data, db) {
  const grouped = new Map(groups.map((g) => [g.key,[]]));
  for (const entry of data.entries) {
    let map = sourceMapping(entry,data.year || 2026);
    if (data.year && data.year !== 2026) map = historicalMapping(db,entry,data.year,map.group);
    const readable = entry.source_text.replace(/n\s*i\s*c\s*h\s*t\s+a\s*u\s*s\s*g\s*e\s*t\s*r\s*a\s*g\s*e\s*n/ig, 'nicht ausgetragen');
    const noAward = !entry.medals.length && /\b(?:keine\s+(?:eigene\s+)?wertung|nicht\s+ausgetragen)\b/i.test(readable);
    const combinedReference = !entry.medals.length && /\bzusammen\s+mit\b[\s\S]*\bals\b[\s\S]*\bgewertet\b/i.test(readable);
    const emptySource = !entry.medals.length && /^\s*[-–—](?:[\s-–—])*\s*$/.test(readable);
    // Some annual lists state cancellation once in the event heading and
    // leave every category empty. Never mask medals or call this an ANNE match.
    const cancelledEvent = !entry.medals.length && /\b(?:abgesagt|storniert|nicht\s+ausgetragen)\b/i.test(entry.event_heading || '');
    const sourceNote = noAward || combinedReference || emptySource || cancelledEvent;
    const candidates = db ? regionalEntries(db,map) : null;
    let comparison = data.year && data.year < 2013 ? {state:'unknown',label:'Kein ANNE-Vergleich vor 2013'}
      : db ? compare(entry,candidates,map) : {state:'unknown',label:'Vergleich nicht verfügbar'};
    comparison = markReconstructedComparison(comparison,candidates);
    const userConfirmedError = userConfirmedWolvError(entry,data.year);
    if (userConfirmedError) comparison = {state:'mismatch',label:'WOLV-Quellfehler bestätigt'};
    const verifiedAnne = userConfirmedError || anneCorrectEvidence(entry,candidates,map,comparison);
    const badge = {match:'✓',mismatch:'✕',unknown:'?'}[comparison.state];
    const medalNames = {1:'Gold',2:'Silber',3:'Bronze'};
    const medals = entry.medals.length ? `<ol class="wolv-medals">${entry.medals.map((m) =>
      `<li><span class="wolv-place">${m.rank}.</span><span><strong>${medalNames[m.rank]}</strong> · ${esc(m.text)}</span></li>`).join('')}</ol>` : '';
    const original = `<p class="wolv-raw">${esc(readable)}</p>${entry.shared_notice ? `<p class="wolv-raw">Gemeinsamer Hinweis: ${esc(entry.shared_notice)}</p>` : ''}`;
    const details = entry.medals.length ? `<details><summary>Originaltext und Hinweise</summary>${original}</details>` : original;
    const differences = !sourceNote && comparison.differences?.length ? `<p class="${comparison.state === 'mismatch' ? 'wolv-conflict' : 'wolv-uncertain'}">${comparison.differences.map(esc).join('<br>')}</p>` : '';
    const actualHeading = entry.source_heading.replace(/^D\s+1\s+5\s*-/,'D 15-').replace(/:$/,'').trim();
    const category = actualHeading || entry.source_category.replace(/\n/g,' ');
    const comparisonBadge = sourceNote ? '<span class="wolv-badge wolv-match" title="Hinweis aus der WOLV-Quelle; kein ANNE-Abgleich" aria-label="Hinweis aus der WOLV-Quelle; kein ANNE-Abgleich">✓</span>' : `<span class="wolv-badge wolv-${comparison.state}" title="${esc(comparison.label)}">${badge} ${esc(comparison.label)}</span>`;
    const verifiedBadge = verifiedAnne ? `<span class="wolv-badge wolv-match" title="${esc(verifiedAnne.explanation)}">✓ ANNE korrekt</span>` : '';
    const verifiedSource = verifiedAnne ? `<p class="wolv-raw">${esc(verifiedAnne.explanation)} <a href="${esc(verifiedAnne.url)}" target="_blank" rel="noopener">ANNE-Original ↗</a></p>` : '';
    const reconstructionNote = comparison.label === 'WOLV-Podium · Altersrekonstruktion'
      ? '<p class="wolv-raw">Das WOLV-Podium ist Grundlage der Altersaufteilung; weitere Plätze sind rekonstruiert. Kein unabhängiger Podiumsvergleich.</p>' : '';
    grouped.get(map.group).push({heading:entry.event_heading || '',html:`<article class="wolv-card"><div class="wolv-card-header"><div><h3>${esc(category)}</h3></div><div class="wolv-badges">${comparisonBadge}${verifiedBadge}</div></div>${medals}${differences}${verifiedSource}${reconstructionNote}${details}${map.event ? `<p class="wolv-raw"><a href="../index.html#/event/${map.event}/lm/WIEN">ANNE-Auswertung ansehen ↗</a></p>` : ''}</article>`});
  }
  return groups.filter((g) => grouped.get(g.key).length).map((g) => {
    const rows=grouped.get(g.key), dates=[...new Set(rows.map((r)=>r.heading).filter(Boolean))];
    return `<section class="wolv-group"><h2>${esc(g.title)}</h2><p class="sub dim">${dates.map((d)=>esc(d.replace(/\s*\n\s*/g,' · '))).join(' · ')}</p><div class="wolv-grid">${rows.map((r)=>r.html).join('')}</div></section>`;
  }).join('');
}

async function bootWolv() {
  const target = document.getElementById('wolv-archive');
  try {
    const response = await fetch('data.json', {cache:'no-cache'});
    if (!response.ok) throw new Error('WOLV-Quelldaten fehlen');
    const data = await response.json();
    if (data.schema_version !== 1 || !Array.isArray(data.years)) throw new Error('Unerwarteter Quellenstand');
    let db = null;
    try {
      const SQL = await initSqlJs({locateFile:(f) => `https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.10.3/${f}`});
      const result = await fetch('../data/results.db.gz', {cache:'no-cache'});
      if (!result.ok) throw new Error('OLResults-Datenbank nicht verfügbar');
      const bytes = await new Response(result.body.pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
      db = new SQL.Database(new Uint8Array(bytes));
    } catch (error) { console.warn('ANNE-Vergleich nicht verfügbar',error); }
    const select = document.getElementById('wolv-year');
    const source = document.getElementById('wolv-source');
    const external = document.getElementById('wolv-external');
    const note = document.getElementById('wolv-year-note');
    select.innerHTML = [...data.years].reverse().map((item) => `<option value="${item.year}">${item.year}</option>`).join('');
    const requested = Number(new URLSearchParams(location.search).get('year'));
    if (data.years.some((item) => item.year === requested)) select.value = String(requested);
    const showYear = () => {
      const item = data.years.find((row) => row.year === Number(select.value));
      source.href = item.source_file;
      source.textContent = `Original-Medaillenliste ${item.year} ansehen ↗`;
      external.href = item.source_url;
      external.textContent = `WOLV-Medaillenliste ${item.year}`;
      note.textContent = item.year < 2013 ? '· Historisches WOLV-Archiv; kein ANNE-Vergleich vor 2013.' : '';
      target.innerHTML = render(item,db);
      const url = new URL(location.href); url.searchParams.set('year',item.year); history.replaceState(null,'',url);
    };
    select.addEventListener('change',showYear);
    showYear();
  } catch (error) { target.textContent = `Die WOLV-Liste konnte nicht geladen werden: ${error.message}`; }
}
if (typeof module !== 'undefined' && module.exports) module.exports = {sourceMapping,sourceGroup,historicalMapping,categoryKey,mixedCategoryKey,uniqueAgeRangeCandidate,uniqueExactPodiumCandidate,regionalEntries,sourceMatches,compare,anneCorrectEvidence,userConfirmedWolvError,markReconstructedComparison,render};
if (typeof document !== 'undefined') bootWolv();
