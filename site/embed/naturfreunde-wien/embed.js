"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const escape = s => String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const {seasonYear,clubMedalCounts,annualClubGoldSeries} = window.OLRDomainRules;
  const modes = [["om","ÖM/ÖSTM"],["ostm","ÖSTM"],["wien","Wiener MS"],["success","Erfolgsstatistik"]];
  let data, mode="om", year="all", sport="all";
  function competitionLabel(r) {
    const findFormat = text => {
      const t = String(text || '').toLocaleLowerCase('de-AT');
      if (/mixed[\s-]*(?:sprint[\s-]*)?staffel|mix[\s-]*sprint/.test(t)) return 'Mixed Sprint-Staffel';
      if (/staffel/.test(t)) return 'Staffel';
      if (/mannschaft/.test(t)) return 'Mannschaft';
      if (/knock[\s-]*out/.test(t)) return 'Knock-out-Sprint';
      if (/marathon/.test(t)) return 'Marathon';
      if (/nacht/.test(t)) return 'Nacht';
      if (/mittel/.test(t)) return 'Mittel';
      if (/lang(?:distanz)?/.test(t)) return 'Lang';
      if (/sprint/.test(t)) return 'Sprint';
      return '';
    };
    const format = findFormat(r.stage_title) || findFormat(r.event_title);
    const discipline = ({footOrienteering:'OL',skiOrienteering:'Ski-O',mountainbikeOrienteering:'MTBO',trailOrienteering:'Trail-O'})[r.sport_type];
    if (format) return [discipline,format].filter(Boolean).join(' ');
    return String(r.event_title || r.stage_title || 'Bewerb unbekannt');
  }
  function categoryLabel(r) {
    return String(r.category || r.category_full || '').replace(/^([DH])\s*(\d)/i, '$1 $2').replace(/^([DH])\s*-(\d)/i, '$1 -$2');
  }
  const bySport = rows => rows.filter(r => sport === "all" || !r.sport_type || r.sport_type === sport);
  const headers = `<th class="gold">Gold</th><th class="silver">Silber</th><th class="bronze">Bronze</th>`;
  const cells = counts => `<td class="gold">${counts.gold}</td><td class="silver">${counts.silver}</td><td class="bronze">${counts.bronze}</td>`;
  const counts = rows => clubMedalCounts(rows);
  function summary(rows) {
    const sum = mode === "om" ? [["ÖSTM",rows.filter(r=>r.championship==="ÖSTM")],["ÖM",rows.filter(r=>r.championship==="ÖM")],["Gesamt",rows]] : [["Vereinssumme",rows]];
    return `<div class="summary"><table><thead><tr><th>Wertung</th>${headers}</tr></thead><tbody>${sum.map(([label,rs],i)=>`<tr class="${i===sum.length-1?"total":""}"><th scope="row">${label}</th>${cells(counts(rs))}</tr>`).join("")}</tbody></table></div><p class="hint">Vereinssumme: Teammedaillen zählen einmal pro Team. In der Personenliste erhält jedes Teammitglied seine Medaille.</p>`;
  }
  function peopleTable(rows) {
    const grouped = new Map();
    for (const r of rows) { if (r.person_id == null || r.identity_basis === "source-team-only") continue; const id=String(r.person_id); if(!grouped.has(id)) grouped.set(id,{name:r.person_name,rows:[],i:[0,0,0],t:[0,0,0]}); const p=grouped.get(id); p.rows.push(r); const n=r.national_rank; if(n>=1&&n<=3) p[["team","relay","pair"].includes(r.result_kind)?"t":"i"][n-1]++; }
    const people=[...grouped.values()].map(p=>({...p,sum:p.i.map((x,i)=>x+p.t[i])})).sort((a,b)=>b.sum[0]-a.sum[0]||b.sum[1]-a.sum[1]||b.sum[2]-a.sum[2]||a.name.localeCompare(b.name,"de-AT"));
    const split=mode==="ostm"; const medalCells=(a,team=false)=>a.map((v,i)=>`<td class="${["gold","silver","bronze"][i]}${team?" team":""}">${v||"–"}</td>`).join("");
    let rank=0; people.forEach((p,i)=>{if (!i || p.sum.some((v,j)=>v!==people[i-1].sum[j])) rank=i+1; p.rank=rank;});
    const detail = r => [ `<b>${["","Gold","Silber","Bronze"][r.national_rank]}</b>`, escape((r.date||"").slice(0,10).split("-").reverse().join(".")), mode==='om' ? escape(r.championship) : '', escape(competitionLabel(r)), escape(categoryLabel(r)) ].filter(Boolean).join(' · ');
    return `<p class="hint">Person auswählen, um die Medaillenbewerbe zu sehen.${split?" Auf kleinen Bildschirmen die Tabelle seitlich wischen.":""}</p><div class="scroll"><table class="${split?"breakdown":""}"><thead>${split?`<tr><th rowspan="2">Läufer:in</th><th colspan="3">Einzel</th><th colspan="3" class="team-heading">Team</th><th colspan="3">Summe</th></tr><tr>${headers}${headers}${headers}</tr>`:`<tr><th>Läufer:in</th>${headers}<th>Summe</th></tr>`}</thead><tbody>${people.map((p,i)=>`<tr><td><span class="rank" aria-label="Rang ${p.rank}">#${p.rank}</span><button class="runner" aria-expanded="false" aria-controls="details-${i}">${escape(p.name)} ▸</button></td>${split?medalCells(p.i)+medalCells(p.t,true)+medalCells(p.sum):medalCells(p.sum)+`<td class="total">${p.sum.reduce((a,b)=>a+b,0)}</td>`}</tr><tr class="details" id="details-${i}" hidden><td colspan="${split?10:5}"><ul>${p.rows.slice().sort((a,b)=>a.national_rank-b.national_rank||String(b.date).localeCompare(String(a.date))).map(r=>`<li>${detail(r)}</li>`).join("")}</ul></td></tr>`).join("")||`<tr><td colspan="${split?10:5}">Keine Medaillen erfasst.</td></tr>`}</tbody></table></div>`;
  }
  function success(national,vienna) {
    const hist=sport==="all"?data.history:null, series=annualClubGoldSeries(national,vienna,{startYear:hist?2000:null,history:hist});
    if(!series.length) return `<p>Keine Medaillen für diese Auswahl erfasst.</p>`;
    const max=Math.max(1,...series.flatMap(r=>[r.national||0,r.vienna||0]));
    const bar=(value,kind,year,label)=>`<div class="bar-wrap" role="img" aria-label="${year}, ${label}: ${value == null ? "keine Daten" : `${value} Gold`}" style="height:${value == null || value === 0 ? 0 : value/max*160}px"><b class="bar-value ${kind}-value">${value ?? "–"}</b>${value ? `<div class="bar ${kind}"></div>` : ""}</div>`;
    return `<h2>Goldmedaillen im Verlauf</h2><p class="hint">Eine Medaille pro Team · alle Altersklassen · Ski-OL nach Saison</p><div class="legend"><span><i class="national"></i>ÖM/ÖSTM</span><span><i class="vienna"></i>Wiener MS</span></div><p class="chart-scroll-hint">↔ Für weitere Jahre seitlich wischen oder scrollen</p><div class="scroll chart-scroll" tabindex="0" role="region" aria-label="Jahresverlauf: horizontal scrollen"><div class="chart">${series.map(r=>`<div class="chart-year">${bar(r.national,"national",r.year,"ÖM/ÖSTM")}${bar(r.vienna,"vienna",r.year,"Wiener MS")}<span>${r.year}</span></div>`).join("")}</div></div><p class="hint">${hist?"2000–2012: vereinsinterne Statistik (Kalenderjahr, alle Disziplinen).":"Vereinszahlen 2000–2012 sind nur für alle Disziplinen gemeinsam verfügbar."} Ab 2013: OLResults; – bedeutet keine erfasste Podestwertung.</p><details><summary>Jahreswerte anzeigen</summary><table><thead><tr><th>Jahr</th><th>ÖM/ÖSTM</th><th>Wiener MS</th></tr></thead><tbody>${series.map(r=>`<tr><td>${r.year}</td><td>${r.national??"–"}</td><td>${r.vienna??"–"}</td></tr>`).join("")}</tbody></table></details>`;
  }
  function render() {
    const national=bySport(data.national), vienna=bySport(data.vienna);
    const available=mode==="wien"?vienna:mode==="ostm"?national.filter(r=>r.championship==="ÖSTM"):national;
    const years=[...new Set(available.map(r=>seasonYear(r.date,r.sport_type)).filter(Boolean))].sort((a,b)=>b.localeCompare(a));
    if(year!=="all"&&!years.includes(year)) year="all";
    $("modes").innerHTML=modes.map(([id,label])=>`<button type="button" data-mode="${id}" aria-pressed="${mode===id}">${label}</button>`).join("");
    $("year-control").hidden=mode==="success";
    $("year").innerHTML=`<option value="all">Alle Jahre</option>${years.map(y=>`<option value="${y}" ${year===y?"selected":""}>${y}</option>`).join("")}`;
    const selected=year==="all"?available:available.filter(r=>seasonYear(r.date,r.sport_type)===year);
    $("content").innerHTML=mode==="success"?success(national,vienna):summary(selected)+peopleTable(selected);
    // Show the newest seasons first when the chart is wider than its frame.
    const chartScroll = $("content").querySelector?.(".chart-scroll");
    if (chartScroll) chartScroll.scrollLeft = chartScroll.scrollWidth;
    $("data-status").textContent=`Daten: OLResults · Auswertung erstellt am ${new Date(data.generated_at).toLocaleDateString("de-AT")} · automatisiert aus Ergebnislisten; Fehler sind möglich.`;
  }
  $("modes").addEventListener("click",e=>{const b=e.target.closest("button[data-mode]"); if(b){mode=b.dataset.mode;render();}});
  $("year").addEventListener("change",e=>{year=e.target.value;render();});
  $("sport").addEventListener("change",e=>{sport=e.target.value;render();});
  $("content").addEventListener("click",e=>{const b=e.target.closest("button[aria-controls]");if(!b)return;const panel=document.getElementById(b.getAttribute("aria-controls"));panel.hidden=!panel.hidden;b.setAttribute("aria-expanded",String(!panel.hidden));});
  fetch(`data.json?v=${encodeURIComponent(window.OLR_BUILD||"dev")}`,{cache:"no-cache"}).then(r=>{if(!r.ok)throw Error("HTTP "+r.status);return r.json();}).then(d=>{if(d.schema_version!==1||d.club!=="Naturfreunde Wien")throw Error("Unerwarteter Datenstand");data=d;const years=d.national.map(r=>seasonYear(r.date,r.sport_type)).filter(Boolean).sort();year=years.at(-1)||"all";render();}).catch(()=>{$("content").innerHTML='<p class="error">Die Medaillenstatistik konnte nicht geladen werden. Bitte später erneut versuchen.</p>';});
})();
