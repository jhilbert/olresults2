/* The Meistertafel contains champions only, not silver or bronze results. */
(function (root) {
  const nameKey = name => name.trim().replace(/\s+/g, ' ').normalize('NFC').toLocaleLowerCase('de-AT');
  // A surname by itself is not a person identifier, even within one event.
  const personKey = member => {
    const key = nameKey(member.name);
    if (['helga kolb','helga kromp-kolb','helg kromp-kolb'].includes(key)) return 'confirmed:1727';
    if (['katja bonek','katja gassner'].includes(key)) return 'confirmed:katja-bonek-gassner';
    if (['claudia bonek','claudia künzel','claudia künzl','claudia künzel-bonek'].includes(key)) return 'confirmed:claudia-kuenzel-bonek';
    if (['ursula kadan','ursi kadan','ursula fesselhofer','ursi fesselhofer'].includes(key)) return 'confirmed:ursula-kadan-fesselhofer';
    if (['lucie böhm','lucie rothauer'].includes(key)) return 'confirmed:lucie-boehm-rothauer';
    if (['elisabeth knapp','elisabeth nagy','sissy knapp','sissy nagy','sissi knapp','sissi nagy'].includes(key)) return 'confirmed:elisabeth-nagy-knapp';
    if (['gernot kerschbaumer','gernot ymsén','gernot ymsen','gernot ymsén-kerschbaumer','gernot ymsen-kerschbaumer'].includes(key)) return 'confirmed:gernot-kerschbaumer-ymsen';
    if (['regina mandl','regina habenicht'].includes(key)) return 'confirmed:regina-mandl-habenicht';
    if (['traude fesselhofer','waltraud fesselhofer','waltraud köpl','waltraud köppl'].includes(key)) return 'confirmed:waltraud-koepl-fesselhofer';
    if (['andrea eisl','andrea strasser'].includes(key)) return 'confirmed:andrea-eisl-strasser';
    if (['christine gaterhcole','christine gathercole','christine müller'].includes(key)) return 'confirmed:christine-mueller-gathercole';
    if (['bati tobler','bati fuchs'].includes(key)) return 'confirmed:bati-tobler-fuchs';
    if (key === 'barbara tobler' && ['tablepress-36:row-59','tablepress-38:row-109'].includes(member.source_row)) return `unresolved:${member.source_row}:barbara-tobler`;
    if (['sonja zinkl','sonja reisinger-zinkl'].includes(key)) return 'confirmed:sonja-zinkl-reisinger';
    if (['lisi hohenwarter','lisis sandrisser','lisi sandrisser','elisabeth sandrisser','elisabeth hohenwarter'].includes(key)) return 'confirmed:elisabeth-hohenwarter-sandrisser';
    if (['thea lillehof','thea lillehov','thea schneder-lillehov','thea schneider-lillehov'].includes(key)) return 'confirmed:thea-lillehov-schneider';
    if (['michaela gigon','michaela gigo','michale gigo','michaela gigon olt transdanubien'].includes(key) || (key === 'michael gigon' && member.source_row === 'tablepress-39:row-29')) return 'confirmed:michaela-gigon';
    if (['wolfgang kradischnig','kradischnig wolfgang'].includes(key)) return 'confirmed:wolfgang-kradischnig';
    if (['günther kradischnig','günter kradischnig'].includes(key)) return 'confirmed:guenter-kradischnig';
    return /\s/.test(member.name.trim()) ? key : `unresolved:${member.source_row || ''}:${key}`;
  };
  function membersForGroup(award, view, groupName, clubLabel, groupKey) {
    return award.members.filter(member => view === 'people'
      ? (groupKey ? personKey(member) === groupKey : nameKey(member.name) === nameKey(groupName))
      : (clubLabel(member.club || award.club, award.year) || 'Verein unbekannt') === groupName);
  }
  function aggregate(awards, view, clubLabel, selectedClub = '') {
    const groups = new Map();
    function add(key, name, award) {
      if (!key) return;
      if (!groups.has(key)) groups.set(key, {name, key, awards: new Map()});
      groups.get(key).awards.set(award.id, award);
    }
    for (const award of awards) {
      for (const member of award.members) {
        const club = clubLabel(member.club || award.club, award.year);
        if (selectedClub && club !== selectedClub) continue;
        if (view === 'people') {
          const name = member.name.trim().replace(/\s+/g, ' ');
          // Preserve accents and distinct spellings; no unverified identity merge.
          const id = personKey(member);
          const confirmedNames = {'confirmed:1727':'Helga KOLB / Helga KROMP-KOLB', 'confirmed:katja-bonek-gassner':'Katja BONEK / Katja GASSNER', 'confirmed:claudia-kuenzel-bonek':'Claudia KÜNZEL / Claudia BONEK', 'confirmed:ursula-kadan-fesselhofer':'Ursula KADAN / Ursula FESSELHOFER', 'confirmed:lucie-boehm-rothauer':'Lucie BÖHM / Lucie ROTHAUER', 'confirmed:elisabeth-nagy-knapp':'Elisabeth NAGY / Elisabeth KNAPP', 'confirmed:gernot-kerschbaumer-ymsen':'Gernot KERSCHBAUMER / Gernot YMSÉN', 'confirmed:regina-mandl-habenicht':'Regina MANDL / Regina HABENICHT', 'confirmed:waltraud-koepl-fesselhofer':'Waltraud KÖPL / Traude FESSELHOFER', 'confirmed:andrea-eisl-strasser':'Andrea EISL / Andrea STRASSER', 'confirmed:christine-mueller-gathercole':'Christine MÜLLER / Christine GATHERCOLE', 'confirmed:bati-tobler-fuchs':'Bati TOBLER / Bati FUCHS', 'confirmed:sonja-zinkl-reisinger':'Sonja ZINKL / Sonja REISINGER-ZINKL', 'confirmed:elisabeth-hohenwarter-sandrisser':'Elisabeth HOHENWARTER / Lisi SANDRISSER', 'confirmed:thea-lillehov-schneider':'Thea LILLEHOV / Thea SCHNEIDER-LILLEHOV', 'confirmed:michaela-gigon':'Michaela GIGON', 'confirmed:wolfgang-kradischnig':'Wolfgang KRADISCHNIG', 'confirmed:guenter-kradischnig':'Günter KRADISCHNIG'};
          const display = confirmedNames[id] || (id.includes(':barbara-tobler') ? 'Barbara TOBLER (Zuordnung offen)' : null) || member.display_name || (/\s/.test(name) ? name : `${name} (Vorname fehlt)`);
          add(id, display, award);
        } else {
          add(club || '(Verein unbekannt)', club || 'Verein unbekannt', award);
        }
      }
    }
    return [...groups.values()].map(g => {
      const entries = [...g.awards.values()];
      const oestm = entries.filter(a => a.title_type === 'ÖSTM').length;
      return {name:g.name, key:g.key, entries, oestm, oem:entries.length-oestm, total:entries.length};
    }).sort((a,b) => b.total-a.total || b.oestm-a.oestm || a.name.localeCompare(b.name,'de'));
  }
  root.MeistertafelTitles = {aggregate, membersForGroup, personKey};
  if (typeof module !== 'undefined') module.exports = {aggregate, membersForGroup, personKey};
})(typeof globalThis === 'undefined' ? this : globalThis);
