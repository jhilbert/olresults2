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
    if (state === "invitation" && c.invitation.state === "category_reviewed") return false;
    if (state === "crosscheck" && c.crosscheck.state === "winner_matched_only") return false;
    if (state === "result" && c.result.completeness === "human_confirmed") return false;
    if (state === "rule" && !(c.rule_findings || []).length) return false;
    return true;
  });
}

function classHtml(c) {
  const inv = c.invitation, cross = c.crosscheck, r = c.result;
  const invLabel = {category_reviewed:"Klasse anhand Ausschreibung hinterlegt", category_conflict:"Widerspruch: Kategorie laut Ausschreibung kein Meisterschaftsbewerb", found_unreviewed:"Ausschreibung gefunden – Klasse noch nicht geprüft", not_found:"Keine Ausschreibung im abgefragten ANNE-Verzeichnis", not_checked:"Ausschreibung noch nicht inventarisiert"}[inv.state] || inv.state;
  const crossLabel = {winner_matched_only:"Sieger abgeglichen; weitere Plätze offen", winner_partially_matched:"Sieger teilweise abgeglichen", winner_suggested:"Sieger in ÖFOL-Jahresliste vorgeschlagen – Eventzuordnung noch prüfen", conflict:"Quellenwiderspruch – klären", not_compared:"Noch nicht mit Meisterliste abgeglichen", year_unavailable:"Meisterliste für dieses Jahr noch nicht im Bestand"}[cross.state] || cross.state;
  const invLinks = [...new Set([inv.reviewed_url, ...inv.urls].filter(Boolean))].map((u) => link(u, "Ausschreibung")).join(" · ");
  const resultLinks = r.source_urls.map((u) => link(u, "Ergebnisquelle")).join(" · ");
  const sourceRow = cross.source_row ? ` · Quellzeile: ${esc(cross.source_row.name)} (${esc(cross.source_row.club)}), ${esc(cross.source_row.category)}, ${esc(cross.source_row.table_id)} / ${esc(cross.source_row.source_row)}` : "";
  const listUrl = `review.html?event=${encodeURIComponent(c.event_id)}${r.list_ids.length ? `&list=${encodeURIComponent(r.list_ids[0])}` : ""}`;
  const findings = r.issues.reduce((sum, issue) => sum + issue.count, 0);
  const ruleFindings = (c.rule_findings || []).map((f) => `<p class="champ-status champ-open">Regelhinweis: ${esc(f.message)} (${f.known_started_entries} bekannte Starteinheiten; ${esc(f.known_clubs.join(", "))}${f.other_entries_open ? "; weitere Berechtigung oder Status offen" : ""})</p>`).join("");
  return `<div class="champ-class"><b>${esc(c.category)}</b> <span class="champ-small">${esc(c.championship_type)} · ${esc(c.source_categories.join(", "))}</span>
    <p><span class="champ-status ${inv.state === "category_reviewed" ? "champ-ok" : "champ-open"}">Ausschreibung: ${esc(invLabel)}</span>${invLinks ? ` · ${invLinks}` : ""}${inv.classification_source ? ` · ${link(inv.classification_source.url, "Offizielle Klassenwertung")}` : ""}</p>
    <p>Ergebnis: ${r.row_count} Zeilen · Vollständigkeit ${r.completeness === "human_confirmed" ? "manuell bestätigt" : "nicht manuell bestätigt"} · ${r.identity_open}/${r.identity_applicable} Identitäten offen · ${findings} Prüfhinweise · <a href="${esc(listUrl)}">im Prüftool öffnen</a>${resultLinks ? ` · ${resultLinks}` : ""}</p>
    <p><span class="champ-status ${cross.state === "conflict" ? "champ-conflict" : cross.state === "winner_matched_only" ? "champ-ok" : "champ-open"}">Meisterliste: ${esc(crossLabel)}</span>${sourceRow}${cross.source_url ? ` · ${link(cross.source_url, "Quelle")}` : ""}</p>
    ${ruleFindings}<p class="champ-small">Medaillenregeln und eventbezogene Berechtigung bleiben eine eigenständige Prüfung.</p></div>`;
}

function render() {
  const rows = selectedRows(), events = new Map();
  for (const c of rows) {
    const key = `${c.event_id}:${c.stage_id}:${c.jurisdiction}`;
    if (!events.has(key)) events.set(key, []);
    events.get(key).push(c);
  }
  const inv = rows.filter((r) => r.invitation.state === "category_reviewed").length;
  const matched = rows.filter((r) => r.crosscheck.state === "winner_matched_only").length;
  const conflicts = rows.filter((r) => r.crosscheck.state === "conflict" || r.invitation.state === "category_conflict").length;
  $("summary").textContent = `${events.size} Event-/Wertungsetappen · ${rows.length} Klassen · ${inv} mit belegter Ausschreibung · ${matched} Sieger abgeglichen · ${conflicts} Klassen mit Quellenkonflikt`;
  const year = $("year").value, jurisdiction = $("jurisdiction").value, search = $("search").value.trim().toLocaleLowerCase("de");
  const claims = report.open_source_claims.filter((c) => (year === "all" || (c.date || "").startsWith(year)) &&
    (jurisdiction === "all" || jurisdiction === (c.jurisdiction || "WIEN")) && (!search || `${c.event_id} ${c.name} ${c.category}`.toLocaleLowerCase("de").includes(search)));
  $("claims").innerHTML = claims.length ? `<div class="champ-help champ-conflict"><b>Offene Quellenkonflikte und unvollständige Wertungen – nicht automatisch gezählt</b><ul>${claims.map((c) => `<li>${esc(dateText(c.date))} · ${esc(c.name)} · ${esc(c.category)}: ${esc(c.note)} · ${link(c.source_url,"Quelle")} · ${link(c.result_url,"Ergebnis")}</li>`).join("")}</ul></div>` : "";
  const missingClasses = (report.national_classes_without_instance || []).filter((c) =>
    (jurisdiction === "all" || jurisdiction === "AUT") && (year === "all" || (c.date || "").startsWith(year)) &&
    (!search || `${c.event_id} ${c.category}`.toLocaleLowerCase("de").includes(search)));
  $("missing-classes").innerHTML = missingClasses.length ? `<details class="champ-help champ-open"><summary>${missingClasses.length} ausgeschriebene ÖM-/ÖSTM-Klassen ohne angelegte Wertung</summary><p class="champ-small">Prüfen, ob tatsächlich Ergebnisse oder Starts vorliegen; eine ausgeschriebene Klasse muss nicht stattgefunden haben.</p><ul>${missingClasses.map((c) => `<li>${esc(dateText(c.date))} · <a href="index.html#/event/${encodeURIComponent(c.event_id)}">#${esc(c.event_id)}</a> · Etappe ${c.stage_number} · ${esc(c.category)} · ${c.result_rows} Ergebniszeilen · ${link(c.source_url,"Ausschreibung")}</li>`).join("")}</ul></details>` : "";
  const candidates = report.candidate_events_without_instance.filter((c) =>
    (year === "all" || (c.date || "").startsWith(year)) &&
    (!search || `${c.event_id} ${c.title}`.toLocaleLowerCase("de").includes(search)));
  $("candidates").innerHTML = candidates.length ? `<details class="champ-help"><summary>${candidates.length} mögliche Meisterschafts-Events ohne angelegte Wertung</summary><p class="champ-small">Nur nach Veranstaltungsnamen gefunden: Auch Begleitläufe und Meldeseiten können dabei sein. Ob eine Meisterschaft fehlt, muss anhand der Quelle geprüft werden.</p><ul>${candidates.map((c) => `<li>${esc(dateText(c.date))} · <a href="index.html#/event/${encodeURIComponent(c.event_id)}">#${esc(c.event_id)} ${esc(c.title)}</a>${(c.attachments || []).filter((a) => a.type === "invitation").map((a) => ` · ${link(a.url,"Ausschreibung")}`).join("")}</li>`).join("")}</ul></details>` : "";
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
