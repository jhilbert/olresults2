"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dateText = (d) => /^\d{4}-\d\d-\d\d/.test(d || "") ? `${d.slice(8,10)}.${d.slice(5,7)}.${d.slice(0,4)}` : (d || "");
const link = (url, label) => url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)} ↗</a>` : "";
let report;

function selectedRows() {
  const year = $("year").value, jurisdiction = $("jurisdiction").value;
  const state = $("state").value, search = $("search").value.trim().toLocaleLowerCase("de");
  return report.classes.filter((c) => {
    if (year !== "all" && c.stage_date.slice(0,4) !== year) return false;
    if (jurisdiction !== "all" && c.jurisdiction !== jurisdiction) return false;
    if (search && !`${c.event_id} ${c.event_title} ${c.stage_title || ""} ${c.category}`.toLocaleLowerCase("de").includes(search)) return false;
    if (state === "conflict" && c.crosscheck.state !== "conflict" && c.invitation.state !== "category_conflict") return false;
    return true;
  });
}

function classHtml(c) {
  const inv = c.invitation, cross = c.crosscheck, r = c.result;
  const invLabel = {category_reviewed:"Klasse anhand Ausschreibung belegt", category_conflict:"Widerspruch zur Ausschreibung", found_unreviewed:"Ausschreibung vorhanden", not_found:"Keine Ausschreibung im ANNE-Anhang gefunden", not_checked:"Keine Ausschreibung inventarisiert"}[inv.state] || inv.state;
  const crossLabel = {official_classification:"Offizielle Meisterschaftswertung als Quelle", winner_matched_only:"Sieger mit Meisterliste abgeglichen", winner_name_resolved_by_anne:"Name laut ANNE-Ergebnis übernommen", winner_partially_matched:"Teilweise mit Meisterliste abgeglichen", winner_suggested:"Möglicher Treffer in ÖFOL-Jahresliste", conflict:"Quellenwiderspruch", not_compared:"Kein dokumentierter Siegerabgleich", year_unavailable:"Jahresliste nicht im Bestand"}[cross.state] || cross.state;
  const invLinks = [...new Set([inv.reviewed_url, ...inv.urls].filter(Boolean))].map((u) => link(u, "Ausschreibung")).join(" · ");
  const resultLinks = r.source_urls.map((u) => link(u, "Ergebnisquelle")).join(" · ");
  const sourceRow = cross.source_row ? ` · Quellzeile: ${esc(cross.source_row.name)} (${esc(cross.source_row.club)}), ${esc(cross.source_row.category)}, ${esc(cross.source_row.table_id)} / ${esc(cross.source_row.source_row)}` : "";
  const listUrl = `review.html?event=${encodeURIComponent(c.event_id)}${r.list_ids.length ? `&list=${encodeURIComponent(r.list_ids[0])}` : ""}`;
  return `<div class="champ-class"><b>${esc(c.category)}</b> <span class="champ-small">${esc(c.championship_type)} · ${esc(c.source_categories.join(", "))}</span>
    <p>Ausschreibung: ${esc(invLabel)}${invLinks ? ` · ${invLinks}` : ""}${inv.classification_source ? ` · ${link(inv.classification_source.url, "Offizielle Klassenwertung")}` : ""}</p>
    <p>Ergebnis: ${r.row_count} Zeilen · <a href="${esc(listUrl)}">im Prüftool öffnen</a>${resultLinks ? ` · ${resultLinks}` : ""}</p>
    <p><span class="${cross.state === "conflict" ? "champ-conflict" : ""}">Meisterliste: ${esc(crossLabel)}</span>${sourceRow}${cross.source_url ? ` · ${link(cross.source_url, "Quelle")}` : ""}</p></div>`;
}

function render() {
  const rows = selectedRows(), events = new Map();
  for (const c of rows) {
    const key = `${c.event_id}:${c.stage_id}:${c.jurisdiction}`;
    if (!events.has(key)) events.set(key, []);
    events.get(key).push(c);
  }
  const conflicts = rows.filter((r) => r.crosscheck.state === "conflict" || r.invitation.state === "category_conflict").length;
  $("summary").textContent = `${events.size} Event-/Wertungsetappen · ${rows.length} Klassen · ${conflicts} bekannte Quellenkonflikte`;
  const year = $("year").value, jurisdiction = $("jurisdiction").value, search = $("search").value.trim().toLocaleLowerCase("de");
  const claims = report.open_source_claims.filter((c) => (year === "all" || (c.date || "").startsWith(year)) &&
    (jurisdiction === "all" || jurisdiction === (c.jurisdiction || "WIEN")) && (!search || `${c.event_id} ${c.name} ${c.category}`.toLocaleLowerCase("de").includes(search)));
  $("claims").innerHTML = claims.length ? `<div class="champ-help champ-conflict"><b>Offene Quellenkonflikte und unvollständige Wertungen – nicht automatisch gezählt</b><ul>${claims.map((c) => `<li>${esc(dateText(c.date))} · ${esc(c.name)} · ${esc(c.category)}: ${esc(c.note)} · ${link(c.source_url,"Quelle")} · ${link(c.result_url,"Ergebnis")}</li>`).join("")}</ul></div>` : "";
  $("events").innerHTML = [...events.values()].map((group) => {
    const c = group[0], title = `${dateText(c.stage_date)} · #${c.event_id} ${c.event_title}${c.stage_title ? ` · ${c.stage_title}` : ""} · ${c.jurisdiction === "AUT" ? "ÖM/ÖSTM" : "Wiener MS"}`;
    return `<details class="champ-event"><summary>${esc(title)} · ${group.length} Klassen</summary>${group.map(classHtml).join("")}</details>`;
  }).join("") || "<p>Für diesen Filter sind keine Klassen im Bestand.</p>";
}

fetch("data/championship-audit.json", {cache:"no-store"}).then((r) => {
  if (!r.ok) throw new Error("Quelleninventur fehlt – bitte Datenbank/Quellencheck neu bauen");
  return r.json();
}).then((data) => {
  report = data;
  const years = [...new Set(data.classes.map((r) => (r.stage_date || "").slice(0,4)).filter((y) => /^\d{4}$/.test(y)))].sort().reverse();
  $("year").innerHTML = `<option value="all">Alle</option>${years.map((y) => `<option value="${y}">${y}</option>`).join("")}`;
  $("year").value = years.includes("2026") ? "2026" : (years[0] || "all");
  ["year", "jurisdiction", "state"].forEach((id) => $(id).addEventListener("change", render));
  $("search").addEventListener("input", render);
  render();
}).catch((error) => { $("summary").textContent = `Quellencheck konnte nicht geladen werden: ${error.message}`; });
