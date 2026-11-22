"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const controls = ['year', 'discipline', 'format', 'title', 'category', 'club', 'search'];
  const perPage = 120;
  let data, visible = perPage, view = 'awards';
  const fold = text => String(text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('de-AT').replace(/ß/g, 'ss');
  const aliases = new Map();
  function clubs(canonical, names) { for (const n of [canonical, ...names]) aliases.set(fold(n).replace(/[^a-z0-9]/g, ''), canonical); }
  function configureClubs(registry) {
    const byCode = new Map(registry.map(c => [c.code, c.name]));
    for (const c of registry) clubs(c.name, []);
    function official(code, names) {
      if (!byCode.has(code)) throw Error(`Verein ${code} fehlt im offiziellen Register`);
      clubs(byCode.get(code), names);
    }
    official('NFWien', ['NF Wien', 'NF WIEN', 'Natufreunde Wien', 'TVN Wien']);
    official('AHDO', ['ASKÖ Henndorf', 'AKSÖ Henndorf']);
    official('FOL', ['FUN-OL NÖ', 'Fun-OL NÖ']);
    official('GO_', ['GO Harzberg', 'GO Harzberg Bad Vöslau', 'GO_Harzberg/Bad Vöslau', 'GO_Harzberg/Bad_Vöslau', 'GO-Harzberg Bad Vöslau']);
    official('HAM', ['HSV Absam']);
    official('HVI', ['HSV Villach']);
    official('HSVOLWN', ['HSV Wr.Neustadt', 'HSV Wr. Neustadt', 'HSV Wiener Neustadt', 'HSV OL Wr. Neustadt', 'HSV OL Wr.Neustadt', 'HSV Wr.Neu+A1:F123+B106:F123stadt']);
    official('HPF', ['HSV PInkafeld', 'HSV Pinkafled']);
    official('HSD', ['HSV Spittal', 'HSV Spittal/Drau']);
    official('OrK', ['OK Gittis Klosterneuburg', 'Orienteering Kloserneuburg']);
    official('LOM', ['LZ Omaha']);
    official('NOK', ['NF Kitzbühel', 'NF OLG Kitzbühel', 'Natufreunde Kitzbühel', 'HF Kitzbühel']);
    official('NLZ', ['NF Linz', 'TVN Linz']);
    official('NFS', ['NF Steiermark']);
    official('NVI', ['NF Villach', 'NF Villach Orienteering', 'Naturfreunde Villach', 'Naturfreunde Villach Orienteering', 'Naturfreunde Villach - Orienteering', 'Naturfreunde Villach-Orienteering', 'Natufreunde Villach Orienteering', 'Naturfreunde Villa Orienteering', 'Naturfreunde Villach-Orienteering - Oriente', 'Naturfreunde Villach-Orienteering-Orienteering']);
    official('OVI', ['OLC Union Viktring']);
    official('OLK', ['OLC Kufstein', 'OLK Kufstein']);
    official('OLC', ['OC Wienerwald', 'Wienerwald']);
    official('WAT', ['WAT OL', 'WAT']);
    official('LAC', ['Leibnitzer AC', 'LAC']);
    official('OSW', ['OLG Ströck']);
    official('SKV', ['SKV OLG D.Kaltenbrunn', 'SKV Deutsch Kaltenbrunn', 'OLG D.Kaltenbrunn', 'OLG Deutsch Kaltenbrunn']);
    official('SUK', ['SU Klagenfurtt']);
    official('SUS', ['SU Schöckl Orienteerig']);
    official('OTD', ['OLT Tansdanubien']);
    official('LKI', ['LK Innsbruck', 'LK Kompass Innsbruck', 'LKI Innsbruck']);
    official('ORIO', ['Orienteering Innsbruck Imst', 'Orienteering Innsbruck Im', 'Orienteering Innsbruck', 'Laufklub Kompass Innsbruck Imst']);
    clubs('SU Abteigymnasium Seckau', ['SU Abteigymn. Seckau', 'SU Seckau']);
    clubs('SU RLV Sparkasse Aspang', ['SU RLV Spark. Aspang', 'SU RLV Aspang']);
    clubs('NF Veitsch', ['TVN Veitsch', 'TVN Tyrolia Veitsch']);
    clubs('SC Austrian Airlines', ['SC AUA']);
    clubs('Snowsports', ['www.snowsports.at']);
    clubs('HSV Klagenfurt', ['HSV Klagenfurt Helvetia', 'HSV Klagenfurt - Helvetia']);
  }
  const clubLabel = name => aliases.get(fold(name).replace(/[^a-z0-9]/g, '')) || name || '';
  function formatLabel(a) {
    if (!a.format) return '';
    const original = a.format.trim();
    let f = original.replace(/^(?:ski[ -]?ol?|mtb[ -]?o)[ -]*/i, '').trim();
    const k = fold(f).replace(/\s+/g, ' ').replace(/-ol$/,'').trim();
    if (/^(?:ol[ -])?lang(?:distanz|strecken)?$/.test(k)) f = 'Lang';
    else if (/^mittel(?:distanz)?$/.test(k)) f = 'Mittel';
    else if (/^sprint(?:distanz)?$/.test(k)) f = 'Sprint';
    else if (/^nacht$/.test(k)) f = 'Nacht';
    else if (/^(?:ol[ -])?marathon$/.test(k)) f = 'Marathon';
    else if (/^mixed[ -]sprint[ -]staffel$/.test(k)) f = 'Mixed Sprint-Staffel';
    else if (/^knock[ -]?out(?:[ -]sprint)?$/.test(k)) f = 'Knock-out-Sprint';
    else if (/^(?:staffel|mannschaft)$/.test(k)) f = k === 'staffel' ? 'Staffel' : 'Mannschaft';
    else if (!f) return a.discipline === 'OL' ? original : a.discipline;
    if (a.discipline === 'OL') return /^ol$/i.test(f) ? 'OL' : `OL ${f.replace(/^ol[ -]+/i, '')}`;
    return a.discipline === 'Ski-O' || a.discipline === 'MTBO' ? `${a.discipline} ${f}` : f;
  }
  function categoryLabel(a) {
    const gender = ({female:'D', male:'H', mixed:'Mixed'})[a.gender] || '';
    const cat = a.category || '';
    if (!cat) return gender || '–';
    if (/^elite\s+(?:damen|herren)$/i.test(cat)) return `${/damen/i.test(cat) ? 'D' : 'H'} Elite`;
    if (/^(?:Damen|Herren|Mixed|D\s|H\s|D\d|H\d|D-|H-)/i.test(cat)) return cat.replace(/^Damen\b/i,'D').replace(/^Herren\b/i,'H');
    const compact = cat.replace(/^bis\s*/i,'-').replace(/^ab\s*(\d+)$/i,'$1-');
    return [gender, compact].filter(Boolean).join(' ');
  }
  const node = (tag, value, cls) => { const e = document.createElement(tag); if (value != null) e.textContent = value; if (cls) e.className = cls; return e; };
  function options(id, values, label) {
    const old = $(id).value;
    $(id).replaceChildren(new Option(label, ''), ...values.map(v => new Option(v, v)));
    if (values.includes(old)) $(id).value = old;
  }
  const sort = vals => [...new Set(vals.filter(Boolean))].sort((a,b) => a.localeCompare(b,'de', {numeric:true}));
  function available() {
    const y = $('year').value, disc = $('discipline').value;
    const rows = data.awards.filter(a => (!y || String(a.year) === y) && (!disc || a.discipline === disc));
    options('format', sort(rows.map(formatLabel)), 'Alle Bewerbe');
    const scoped = rows.filter(a => !$('format').value || formatLabel(a) === $('format').value);
    options('category', sort(scoped.map(categoryLabel).filter(c => c !== '–')), 'Alle Kategorien');
    options('club', sort(scoped.flatMap(a => [a.club, ...a.members.map(m => m.club)]).map(clubLabel)), 'Alle Vereine');
  }
  function row(a) {
    const tr = node('tr');
    const cell = (value, cls) => { const td = node('td', value, cls); tr.append(td); return td; };
    cell(String(a.year), 'col-year');
    const fmt = cell(formatLabel(a) || '–', 'col-format');
    if (a.format && formatLabel(a) !== a.format) fmt.title = `Original: ${a.format}`;
    const cat = cell(categoryLabel(a), 'col-category');
    if (!a.category) cat.title = 'Altersklasse in der Quelle nicht angegeben';
    const names = cell(null, 'names-cell'), club = cell(null, 'club-cell');
    const memberClubs = a.members.map(m => m.club || a.club);
    const sharedClub = memberClubs.every(c => clubLabel(c) === clubLabel(memberClubs[0]));
    for (const [i, m] of a.members.entries()) {
      const nameLine = node('div', null, 'name-line');
      if (a.title_type === 'ÖSTM') nameLine.append(node('span', 'ÖSTM', 'type-tag'));
      const name = node('span', m.display_name || m.name);
      if (m.source_name) name.title = `ÖFOL-Quelle: ${m.source_name}${m.oefol_id ? ` · ÖFOL-ID ${m.oefol_id}` : ''} · ${m.identity_basis}`;
      nameLine.append(name);
      names.append(nameLine);
      if (sharedClub && i > 0) continue;
      const raw = m.club || a.club;
      const line = node('div', clubLabel(raw) || '–');
      if (a.source_club && !m.club) line.title = `ÖFOL-Quelle: ${a.source_club} · Vereinszuordnung ergänzt`;
      else if (raw && clubLabel(raw) !== raw) line.title = `Original: ${raw}`;
      club.append(line);
    }
    if (a.notes.some(n => n.startsWith('section_year_conflict:')) || a.notes.includes('name_or_club_ambiguous')) {
      const warning = node('span', ' ⓘ', 'source-warning');
      warning.title = a.notes.includes('name_or_club_ambiguous') ? 'Name und Verein in der Quelle nicht eindeutig getrennt.' : 'Die Abschnittsjahreszahl in der Quelle weicht vom Tabellenjahr ab.';
      warning.setAttribute('aria-label', warning.title); names.append(warning);
    }
    return tr;
  }
  function header() {
    const labels = view === 'awards'
      ? [['Jahr','col-year'],['Bewerb','col-format'],['Kategorie','col-category'],['Name',''],['Verein','']]
      : [[view === 'people' ? 'Läufer:in' : 'Verein',''],['ÖSTM','number'],['ÖM','number'],['Gold gesamt','number total-column']];
    const tr = node('tr');
    for (const [label,cls] of labels) tr.append(node('th',label,cls));
    $('table-head').replaceChildren(tr);
  }
  function summaryRows(group) {
    const tr = node('tr',null,'summary-row');
    const label = node('td'), button = node('button',group.name,'expand-title');
    button.type='button'; button.setAttribute('aria-expanded','false');
    label.append(button); tr.append(label);
    tr.append(node('td',String(group.oestm || '–'),'number'),node('td',String(group.oem || '–'),'number'),node('td',String(group.total),'number total-column'));
    const detail = node('tr',null,'detail-row'); detail.hidden=true;
    const content=node('td'); content.colSpan=4;
    const describe = a => `${a.year} · ${a.title_type} · ${formatLabel(a) || 'Bewerb unbekannt'} · ${categoryLabel(a)}`;
    if (view === 'clubs') {
      const list=node('ul');
      for (const a of group.entries) {
        const names = MeistertafelTitles.membersForGroup(a,view,group.name,clubLabel).map(m=>m.display_name || m.name).join(', ');
        list.append(node('li',`${describe(a)} · ${names || 'Name unbekannt'}`));
      }
      content.append(list);
    } else {
      const byClub = new Map();
      for (const a of group.entries) {
        const members = MeistertafelTitles.membersForGroup(a,view,group.name,clubLabel,group.key);
        const clubs = [...new Set(members.map(m=>clubLabel(m.club || a.club) || 'Verein unbekannt'))];
        for (const club of clubs) {
          if (!byClub.has(club)) byClub.set(club, []);
          byClub.get(club).push(a);
        }
      }
      for (const [club, entries] of byClub) {
        content.append(node('div',club,'detail-club'));
        const list=node('ul');
        for (const a of entries) list.append(node('li',describe(a)));
        content.append(list);
      }
    }
    detail.append(content);
    button.addEventListener('click',()=>{detail.hidden=!detail.hidden;button.setAttribute('aria-expanded',String(!detail.hidden));});
    return [tr,detail];
  }
  function render() {
    const y=$('year').value, disc=$('discipline').value, fmt=$('format').value, title=$('title').value, cat=$('category').value, club=$('club').value, q=fold($('search').value.trim());
    const filtered = data.awards.filter(a => (!y || String(a.year)===y) && (!disc || a.discipline===disc) && (!fmt || formatLabel(a)===fmt) && (!title || a.title_type===title) && (!cat || categoryLabel(a)===cat) && (!club || [a.club,...a.members.map(m=>m.club)].some(c=>clubLabel(c)===club)) && (!q || fold([a.category,a.club,clubLabel(a.club),a.format,formatLabel(a),...a.members.flatMap(m=>[m.name,m.club,clubLabel(m.club)])].join(' ')).includes(q)));
    const n=filtered.reduce((sum,a)=>sum+a.members.length,0);
    let groups = view === 'awards' ? null : MeistertafelTitles.aggregate(filtered,view,clubLabel,club);
    if (view === 'people' && q) {
      const nameMatches = groups.filter(g => fold(g.name).includes(q));
      // A name hit should not turn every teammate into a person-search hit.
      // Preserve searches by club, category or event when no name matches.
      if (nameMatches.length) groups = nameMatches;
    }
    $('count').textContent=groups ? `${groups.length.toLocaleString('de-AT')} ${view === 'people' ? 'Namensgruppen' : 'Vereine'} · ${filtered.length.toLocaleString('de-AT')} Quellwertungen` : `${filtered.length.toLocaleString('de-AT')} Wertungen · ${n.toLocaleString('de-AT')} namentliche Einträge`;
    $('results-title').textContent=({awards:'Meistertafel',people:'Titel nach Läufer:in',clubs:'Titel nach Verein'})[view];
    $('view-note').hidden=view==='awards';
    $('view-note').textContent=view==='people' ? 'Nur Goldtitel. Ergänzungen sind anhand der Quelle, zusätzlicher Belege oder Bestätigung dokumentiert; Namensgruppen sind nicht durchgängig über Personen-IDs geprüft.' : 'Nur Goldtitel. Pro Verein und Teamwertung wird einmal gezählt; bei gemeinsamen Teams einmal je beteiligtem Verein.';
    const results = document.querySelector('.results');
    results.dataset.filterYear = view==='awards' && Boolean(y); results.dataset.filterFormat = view==='awards' && Boolean(fmt); results.dataset.filterCategory = view==='awards' && Boolean(cat);
    header();
    const out=document.createDocumentFragment();
    if (groups) for (const g of groups.slice(0,visible)) out.append(...summaryRows(g));
    else for (const a of filtered.slice(0,visible)) out.append(row(a));
    const length=groups ? groups.length : filtered.length;
    $('rows').replaceChildren(out); $('more').hidden=length<=visible; $('empty').hidden=Boolean(length);
  }
  async function init() {
    try {
      const [response, displayResponse]=await Promise.all([fetch('meistertafel.json'),fetch('person-display.json')]);
      if (!response.ok || !displayResponse.ok) throw Error(`HTTP ${response.ok ? displayResponse.status : response.status}`);
      data=await response.json();
      const display=await displayResponse.json();
      const confirmed=new Map(display.confirmations.map(c=>[`${c.source_row}\u0000${c.source_name}`,c]));
      for (const award of data.awards) for (const member of award.members) {
        const c=confirmed.get(`${member.source_row}\u0000${member.name}`);
        if (c && member.name === c.source_name) {
          member.source_name=member.name; member.name=`${c.first_name} ${c.last_name}`; member.oefol_id=c.oefol_id; member.identity_basis=c.basis;
        }
        const parts=c || display.formats[member.name];
        if (parts) member.display_name=`${parts.first_name} ${parts.last_name.toLocaleUpperCase('de-AT')}`;
      }
      configureClubs(data.official_clubs);
      options('year', sort(data.awards.map(a=>String(a.year))).reverse(), 'Alle Jahre');
      options('discipline', ['OL','MTBO','Ski-O'], 'Alle Disziplinen');
      $('year').value=String(Math.max(...data.awards.map(a=>a.year))); available(); render();
      for (const id of controls) $(id).addEventListener(id==='search'?'input':'change',()=>{visible=perPage;if(['year','discipline','format'].includes(id))available();render();});
      $('reset').addEventListener('click',()=>{for(const id of controls)$(id).value='';visible=perPage;available();render();});
      $('more').addEventListener('click',()=>{visible+=perPage;render();});
      document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{
        view=button.dataset.view; visible=perPage;
        document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
        render();
      }));
    } catch(error) { $('count').textContent='Meistertafel konnte nicht geladen werden.'; $('error').textContent=`Bitte später erneut versuchen (${error.message}).`; $('error').hidden=false; }
  }
  init();
})();
