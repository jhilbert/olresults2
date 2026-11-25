"use strict";
(() => {
  const $ = id => document.getElementById(id);
  function setupDataNotice() {
    const notice = $('data-notice');
    try { notice.hidden = sessionStorage.getItem('oefol-data-notice-closed') === '1'; }
    catch (_err) { /* Keep the notice visible if storage is unavailable. */ }
    $('data-notice-close').addEventListener('click', () => {
      notice.hidden = true;
      try { sessionStorage.setItem('oefol-data-notice-closed', '1'); }
      catch (_err) { /* Closing still works for this page. */ }
    });
  }
  setupDataNotice();
  const controls = ['year', 'discipline', 'format', 'title', 'category', 'club', 'search'];
  const perPage = 120;
  let data, visible = perPage, view = 'awards';
  let loadObserver, hasMore = false;
  const viewStates = Object.fromEntries(['awards','people','clubs'].map(key => [key, {expanded:new Set()}]));
  const fold = text => String(text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('de-AT').replace(/ß/g, 'ss');
  const aliases = new Map();
  // Only documented or explicitly confirmed former names; spelling variants
  // and abbreviations are not presented as historical legal names.
  const formerNames = new Map();
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
    official('HPF', ['HSV PInkafeld', 'HSV Pinkafled', 'HSV Südburgenland', 'HSV S-Bgld.']);
    official('HSD', ['HSV Spittal', 'HSV Spittal/Drau']);
    official('HWS', ['HSV Salzburg']);
    official('OrK', ['OK Gittis Klosterneuburg', 'Orienteering Kloserneuburg']);
    official('LOM', ['LZ Omaha']);
    official('NOK', ['NF Kitzbühel', 'NF OLG Kitzbühel', 'Natufreunde Kitzbühel', 'HF Kitzbühel']);
    official('NLZ', ['NF Linz', 'TVN Linz']);
    official('NFS', ['NF Steiermark']);
    official('NVI', ['NF Villach', 'NF Villach Orienteering', 'Naturfreunde Villach', 'Naturfreunde Villach Orienteering', 'Naturfreunde Villach - Orienteering', 'Naturfreunde Villach-Orienteering', 'Natufreunde Villach Orienteering', 'Naturfreunde Villa Orienteering', 'Naturfreunde Villach-Orienteering - Oriente', 'Naturfreunde Villach-Orienteering-Orienteering', 'TVN Villach']);
    official('OCFF', ['TV Fürstenfeld']);
    official('OVI', ['OLC Union Viktring']);
    official('OLK', ['OLC Kufstein', 'OLK Kufstein']);
    official('OLC', ['OC Wienerwald', 'Wienerwald', 'OLC Wien', 'OLC Tyrolia', 'OLC-Tyrolia-Wienerwald']);
    official('WAT', ['WAT OL', 'WAT']);
    official('LAC', ['Leibnitzer AC', 'LAC']);
    official('OSW', ['OLG Ströck']);
    official('SKV', ['SKV OLG D.Kaltenbrunn', 'SKV Deutsch Kaltenbrunn', 'OLG D.Kaltenbrunn', 'OLG Deutsch Kaltenbrunn']);
    official('SUK', ['SU Klagenfurtt', 'Union Klagenfurt']);
    official('SUS', ['SU Schöckl Orienteerig']);
    official('OTD', ['OLT Tansdanubien']);
    official('LKI', ['LK Innsbruck', 'LK Kompass Innsbruck', 'LKI Innsbruck']);
    official('ORIO', ['Orienteering Innsbruck Imst', 'Orienteering Innsbruck Im', 'Orienteering Innsbruck', 'Laufklub Kompass Innsbruck Imst']);
    clubs('SU Abteigymnasium Seckau', ['SU Abteigymn. Seckau', 'SU Seckau']);
    clubs('SU RLV Sparkasse Aspang', ['SU RLV Spark. Aspang', 'SU RLV Aspang']);
    clubs('NF Veitsch', ['TVN Veitsch', 'TVN Tyrolia Veitsch']);
    clubs('NF Sandl', ['TVN Sandl', 'NF Sandl OL']);
    clubs('SC Austrian Airlines', ['SC AUA']);
    clubs('Snowsports', ['www.snowsports.at']);
    clubs('HSV Klagenfurt', ['HSV Klagenfurt Helvetia', 'HSV Klagenfurt - Helvetia']);
    for (const [code, names] of [
      ['NFWien', ['TVN Wien']],
      ['NLZ', ['TVN Linz']],
      ['NVI', ['TVN Villach']],
      ['OCFF', ['TV Fürstenfeld']],
      ['OLC', ['OLC Wien', 'OLC Tyrolia']],
      ['OrK', ['OK Gittis Klosterneuburg']],
      ['HPF', ['HSV Südburgenland']],
      ['HWS', ['HSV Salzburg']],
      ['ORIO', ['Orienteering Innsbruck Imst', 'Laufklub Kompass Innsbruck Imst', 'Laufklub Kompass Innsbruck (bis 2024)']],
    ]) formerNames.set(byCode.get(code), names);
    formerNames.set('NF Veitsch', ['TVN Veitsch', 'TVN Tyrolia Veitsch']);
    formerNames.set('NF Sandl', ['TVN Sandl']);
  }
  const clubLabel = (name, year) => {
    const key = fold(name).replace(/[^a-z0-9]/g, '');
    // The old and the newly founded club share a name, not a legal identity.
    // Before 2025 this name belongs to today's Orienteering Imst Oberland.
    if (year && year < 2025 && ['laufklubkompassinnsbruck','lkkompassinnsbruck','lkinsbruck','lkiinnsbruck'].includes(key)) {
      return aliases.get('orienteeringinnsbruckimst');
    }
    return aliases.get(key) || name || '';
  };
  function formatLabel(a) {
    // The 1966 source heading specifies OL but no more specific format.
    if (!a.format) return a.year === 1966 && a.discipline === 'OL' ? 'OL' : '';
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
    let cat = (a.category || '').trim();
    if (!cat) return gender || '–';
    // A bare masters age in the source denotes the same class as "ab 50".
    // Do not infer a direction for youth or elite ages.
    cat = cat.replace(/^(D|H)\s*(35|40|45|50|55|60|65|70|75|80|85|90)\s*$/i, (_, g, age) => `${g.toUpperCase()} ${age}-`);
    cat = cat.replace(/^(D|H)\s*(\d+)\s*[-–]\s*$/i, (_, g, age) => `${g.toUpperCase()} ${age}-`);
    cat = cat.replace(/^(\d+)\s+bis\s+(\d+)$/i, '$1-$2');
    if (/^elite\s+(?:damen|herren)$/i.test(cat)) return `${/damen/i.test(cat) ? 'D' : 'H'} Elite`;
    if (/^(?:Damen|Herren|Mixed|D\s|H\s|D\d|H\d|D-|H-)/i.test(cat)) return cat.replace(/^Damen\b/i,'D').replace(/^Herren\b/i,'H');
    const compact = cat.replace(/^bis\s*/i,'-').replace(/^ab\s*(\d+)$/i,'$1-');
    return [gender, compact].filter(Boolean).join(' ');
  }
  function hasUnresolvedYearConflict(a) {
    const conflicts = a.notes.filter(n => n.startsWith('section_year_conflict:'));
    // ÖFOL's 2022/23 season review confirms the Sprint and Middle champions:
    // https://www.oefol.at/wp-content/uploads/2023/11/Orientierung_2023_ansicht_einzelseiten-1.pdf
    // The year in the 2023 table is correct; only its "Ski-O 2022" heading is wrong.
    const confirmed = a.year === 2023 && a.discipline === 'Ski-O' &&
      /^tablepress-8:row-(?:22[5-9]|23[0-9]|24[2-9]|25[0-4])$/.test(a.id);
    return conflicts.some(n => !(confirmed && n === 'section_year_conflict:Ski-O 2022'));
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
    options('club', sort(scoped.flatMap(a => [a.club, ...a.members.map(m => m.club)].map(c => clubLabel(c, a.year)))), 'Alle Vereine');
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
    const sharedClub = memberClubs.every(c => clubLabel(c, a.year) === clubLabel(memberClubs[0], a.year));
    for (const [i, m] of a.members.entries()) {
      const nameLine = node('div', null, 'name-line');
      if (a.title_type === 'ÖSTM') nameLine.append(node('span', 'ÖSTM', 'type-tag'));
      const name = node('span', m.display_name || m.name);
      if (m.source_name) name.title = `ÖFOL-Quelle: ${m.source_name}${m.oefol_id ? ` · ÖFOL-ID ${m.oefol_id}` : ''} · ${m.identity_basis}`;
      nameLine.append(name);
      names.append(nameLine);
      if (sharedClub && i > 0) continue;
      const raw = m.club || a.club;
      const line = node('div', clubLabel(raw, a.year) || '–');
      if (Object.hasOwn(a, 'source_club') && !m.club) line.title = `ÖFOL-Quelle: ${a.source_club || 'kein Verein angegeben'} · Vereinszuordnung ergänzt`;
      else if (raw && clubLabel(raw, a.year) !== raw) line.title = `Original: ${raw}`;
      club.append(line);
    }
    if (hasUnresolvedYearConflict(a) || a.notes.includes('name_or_club_ambiguous')) {
      const warning = node('span', ' ⓘ', 'source-warning');
      warning.title = a.notes.includes('name_or_club_ambiguous') ? 'Name und Verein in der Quelle nicht eindeutig getrennt.' : 'Die Abschnittsjahreszahl in der Quelle weicht vom Tabellenjahr ab.';
      warning.setAttribute('aria-label', warning.title); names.append(warning);
    }
    return tr;
  }
  function header() {
    const labels = view === 'awards'
      ? [['Jahr','col-year'],['Bewerb','col-format'],['Kategorie','col-category'],['Name',''],['Verein','']]
      : [[view === 'people' ? 'Läufer:in' : 'Verein',''],['ÖSTM','number'],['ÖM','number'],['Gesamt','number total-column']];
    const tr = node('tr');
    for (const [label,cls] of labels) tr.append(node('th',label,cls));
    $('table-head').replaceChildren(tr);
  }
  function summaryRows(group, rank) {
    const tr = node('tr',null,'summary-row');
    const label = node('td'), button = node('button',group.name,'expand-title');
    const previous = view === 'clubs' ? formerNames.get(group.name) : null;
    if (previous?.length) button.append(node('span', ` (ehemals ${previous.join(', ')})`, 'former-club'));
    button.type='button'; button.setAttribute('aria-expanded','false');
    const rankLabel = node('span', `#${rank}`, 'summary-rank');
    rankLabel.setAttribute('aria-label', `Rang ${rank}`);
    label.append(rankLabel, button); tr.append(label);
    tr.append(node('td',String(group.oestm || '–'),'number'),node('td',String(group.oem || '–'),'number'),node('td',String(group.total),'number total-column'));
    const detail = node('tr',null,'detail-row'); detail.hidden=true;
    const content=node('td'); content.colSpan=4;
    const describe = a => `${a.year} · ${a.title_type} · ${formatLabel(a) || 'Bewerb unbekannt'} · ${categoryLabel(a)}`;
    const rowView = view;
    let detailsBuilt = false;
    const buildDetails = () => {
      if (detailsBuilt) return;
      detailsBuilt = true;
    if (rowView === 'clubs') {
      const list=node('ul');
      for (const a of group.entries) {
        const names = MeistertafelTitles.membersForGroup(a,rowView,group.name,clubLabel).map(m=>m.display_name || m.name).join(', ');
        list.append(node('li',`${describe(a)} · ${names || 'Name unbekannt'}`));
      }
      content.append(list);
    } else {
      const byClub = new Map();
      for (const a of group.entries) {
        const members = MeistertafelTitles.membersForGroup(a,rowView,group.name,clubLabel,group.key);
        const clubs = [...new Set(members.map(m=>clubLabel(m.club || a.club, a.year) || 'Verein unbekannt'))];
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
    };
    detail.append(content);
    const expanded = viewStates[rowView].expanded;
    if (expanded.has(group.key)) { buildDetails(); detail.hidden=false; button.setAttribute('aria-expanded','true'); }
    button.addEventListener('click',()=>{
      detail.hidden=!detail.hidden;
      if (!detail.hidden) buildDetails();
      button.setAttribute('aria-expanded',String(!detail.hidden));
      if (detail.hidden) expanded.delete(group.key); else expanded.add(group.key);
    });
    // On a phone the whole summary row is a comfortable touch target, not
    // just the relatively narrow name button. Keyboard access stays on it.
    tr.addEventListener('click', event => {
      if (!button.contains(event.target)) button.click();
    });
    return [tr,detail];
  }
  function render(appendFrom = 0) {
    const y=$('year').value, disc=$('discipline').value, fmt=$('format').value, title=$('title').value, cat=$('category').value, club=$('club').value, q=fold($('search').value.trim());
    const memberMatches = m => [m.name, m.display_name, m.source_name].some(name => fold(name).includes(q));
    const filtered = data.awards.filter(a => (!y || String(a.year)===y) && (!disc || a.discipline===disc) && (!fmt || formatLabel(a)===fmt) && (!title || a.title_type===title) && (!cat || categoryLabel(a)===cat) && (!club || [a.club,...a.members.map(m=>m.club)].some(c=>clubLabel(c,a.year)===club)) && (!q || view === 'people' || a.members.some(memberMatches)));
    let groups = view === 'awards' ? null : MeistertafelTitles.aggregate(filtered,view,clubLabel,club);
    if (view === 'people' && q) {
      // Team results match the award, but only the searched athlete belongs
      // in the person view; teammates must not become search hits.
      groups = groups.filter(g => fold(g.name).includes(q) || g.entries.some(a => MeistertafelTitles.membersForGroup(a,'people',g.name,clubLabel,g.key).some(memberMatches)));
    }
    const results = document.querySelector('.results');
    const selected = controls.map(id => $(id).value.trim()).filter(Boolean);
    $('filter-summary').textContent = selected.length ? `· ${selected.join(' · ')}` : '· Alle Ergebnisse';
    results.setAttribute('aria-label', ({awards:'Meistertafel',people:'Titel nach Läufer:in',clubs:'Titel nach Verein'})[view]);
    results.dataset.view = view;
    results.dataset.filterYear = view==='awards' && Boolean(y); results.dataset.filterFormat = view==='awards' && Boolean(fmt); results.dataset.filterCategory = view==='awards' && Boolean(cat);
    if (!appendFrom) header();
    const out=document.createDocumentFragment();
    if (groups) {
      let rank = 0;
      groups.forEach((g, i) => {
        if (!i || g.total !== groups[i-1].total) rank = i + 1;
        if (i >= appendFrom && i < visible) out.append(...summaryRows(g, rank));
      });
    }
    else for (const a of filtered.slice(appendFrom,visible)) out.append(row(a));
    const length=groups ? groups.length : filtered.length;
    if (appendFrom) $('rows').append(out); else $('rows').replaceChildren(out);
    hasMore=length>visible;
    $('more').hidden=!hasMore || Boolean(loadObserver);
    $('load-sentinel').hidden=!hasMore;
    $('empty').hidden=Boolean(length);
    if (loadObserver) {
      loadObserver.unobserve($('load-sentinel'));
      if (hasMore) loadObserver.observe($('load-sentinel'));
    }
  }
  async function init() {
    try {
      const [response, displayResponse]=await Promise.all([fetch('meistertafel.json'),fetch('person-display.json?v=20261123-1')]);
      if (!response.ok || !displayResponse.ok) throw Error(`HTTP ${response.ok ? displayResponse.status : response.status}`);
      data=await response.json();
      const display=await displayResponse.json();
      const confirmed=new Map(display.confirmations.map(c=>[`${c.source_row}\u0000${c.source_name}`,c]));
      for (const award of data.awards) for (const member of award.members) {
        const c=confirmed.get(`${member.source_row}\u0000${member.name}`);
        if (c && member.name === c.source_name) {
          member.source_name=member.name; member.name=`${c.first_name} ${c.last_name}`; member.oefol_id=c.oefol_id; member.identity_basis=c.basis;
          if (c.club && !member.club && !award.club) member.club=c.club;
        }
        const parts=c || display.formats[member.name];
        if (parts) member.display_name=`${parts.first_name} ${parts.last_name.toLocaleUpperCase('de-AT')}`;
      }
      configureClubs(data.official_clubs);
      options('year', sort(data.awards.map(a=>String(a.year))).reverse(), 'Alle Jahre');
      options('discipline', ['OL','MTBO','Ski-O'], 'Alle Disziplinen');
      const loadMore = () => {if (!hasMore) return;const previous=visible;visible+=perPage;render(previous);};
      if ('IntersectionObserver' in window) {
        loadObserver = new IntersectionObserver(entries => {
          if (entries.some(entry => entry.isIntersecting)) loadMore();
        }, {rootMargin:'600px 0px'});
      }
      $('year').value=String(Math.max(...data.awards.map(a=>a.year))); available(); render();
      const resetViewStates = () => {
        for (const state of Object.values(viewStates)) state.expanded.clear();
      };
      const showTableStart = () => {
        const table = document.querySelector('.table-wrap');
        const headerHeight = document.querySelector('.masthead').getBoundingClientRect().height + document.querySelector('.sticky-header').getBoundingClientRect().height;
        window.scrollTo({top:Math.max(0, table.getBoundingClientRect().top + window.scrollY - headerHeight), behavior:'instant'});
      };
      for (const id of controls) $(id).addEventListener(id==='search'?'input':'change',()=>{resetViewStates();visible=perPage;if(['year','discipline','format'].includes(id))available();render();showTableStart();});
      $('reset').addEventListener('click',()=>{for(const id of controls)$(id).value='';resetViewStates();visible=perPage;available();render();showTableStart();});
      $('filter-toggle').addEventListener('click', event => {
        const grid = $('filter-grid');
        grid.hidden = !grid.hidden;
        event.currentTarget.setAttribute('aria-expanded', String(!grid.hidden));
      });
      $('more').addEventListener('click',loadMore);
      document.querySelectorAll('.view-switch [data-view]').forEach(button=>button.addEventListener('click',()=>{
        view=button.dataset.view; visible=perPage;
        document.querySelectorAll('.view-switch [data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
        render();
        showTableStart();
      }));
    } catch(error) { $('error').textContent=`Meistertafel konnte nicht geladen werden. Bitte später erneut versuchen (${error.message}).`; $('error').hidden=false; }
  }
  init();
})();
