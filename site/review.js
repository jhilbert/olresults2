"use strict";

let db;
let lists = [];
let visible = [];
let decisions = [];
let selectedId = null;
let selectedEventId = null;
let writable = false;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

function query(sql, params = []) {
  const stmt = db.prepare(sql);
  stmt.bind(params);
  const rows = [];
  while (stmt.step()) rows.push(stmt.getAsObject());
  stmt.free();
  return rows;
}

function decisionMap(listId) {
  return new Map(decisions.filter((a) => a.scope_key === listId)
    .map((a) => [a.dimension, a]));
}

function requiredDimensions(list) {
  const base = ["completeness", "parsing", "identity", "ranking"];
  if (list.is_national || list.is_regional) base.push("rules");
  return base;
}

function isAutomaticallyConfirmed(list) {
  // A clean result of the deterministic quality gates is itself a
  // reproducible verification decision. Do not inflate the local review
  // overlay with tens of thousands of synthetic click assertions. A manual
  // flag always wins, and any later audit finding reopens the category.
  return !list.blockers && !list.warnings && !list.regional_candidates && !isFlagged(list);
}

async function saveChampionshipDecision(instanceId, fingerprint, state, button) {
  if (!writable) return;
  button.disabled = true;
  const old = button.textContent;
  button.textContent = "speichert …";
  try {
    const response = await fetch("/api/championship", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: instanceId, input_fingerprint: fingerprint, state,
        reviewer: "local-review", reviewed_at: new Date().toISOString() }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Speichern fehlgeschlagen");
    button.textContent = state === "confirmed" ? "✓ Zuordnung vorgemerkt" : "✓ Ablehnung vorgemerkt";
    $("review-save-status").textContent = "Meisterschaftsentscheid gespeichert · wird beim nächsten DB-Build wirksam";
  } catch (error) {
    button.disabled = false;
    button.textContent = old;
    $("review-save-status").textContent = error.message;
  }
}

function isConfirmed(list) {
  if (isAutomaticallyConfirmed(list)) return true;
  const byDim = decisionMap(list.id);
  return requiredDimensions(list).every((d) =>
    ["confirmed", "not_applicable"].includes(byDim.get(d)?.state) &&
    byDim.get(d)?.input_fingerprint === list.input_fingerprint);
}

function isFlagged(list) {
  return [...decisionMap(list.id).values()].some((a) => a.state === "flagged");
}

function isRegional(list, jurisdiction = null) {
  if (!list.is_regional) return false;
  if (!jurisdiction) return true;
  return String(list.regional_jurisdictions || "").split(",").includes(jurisdiction);
}

function matchesQueueState(list, state) {
  if (state === "open") return !isConfirmed(list);
  if (state === "quality") return Boolean(list.parser_blockers || list.ranking_warnings);
  if (state === "issues") return Boolean(list.blockers || list.warnings);
  if (state === "confirmed") return isConfirmed(list);
  return true;
}

function filteredLists(includeState = true) {
  const campaign = $("campaign").value;
  const state = $("queue-state").value;
  const date = $("queue-date").value;
  const needle = $("queue-search").value.trim().toLocaleLowerCase("de");
  return lists.filter((list) => {
    if (campaign === "national" && !list.is_national) return false;
    if (campaign === "regional" && !isRegional(list)) return false;
    if (campaign.startsWith("regional:") && !isRegional(list, campaign.slice(9))) return false;
    const dateFrom = String(list.date || "").slice(0, 10);
    const dateTo = String(list.date_to || list.date || "").slice(0, 10);
    if (date && (!dateFrom || date < dateFrom || date > dateTo)) return false;
    if (includeState && !matchesQueueState(list, state)) return false;
    if (needle && !`${list.event_id} ${list.event_title} ${list.stage_title} ${list.category}`
      .toLocaleLowerCase("de").includes(needle)) return false;
    return true;
  });
}

function eventGroups(rows) {
  const groups = new Map();
  for (const list of rows) {
    const key = String(list.event_id);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(list);
  }
  return [...groups.entries()].sort(([, a], [, b]) =>
    String(b[0]?.date || "").localeCompare(String(a[0]?.date || "")) ||
    String(a[0]?.event_title || "").localeCompare(String(b[0]?.event_title || ""), "de"));
}

function renderEventSelect(groups, baseRows) {
  const baseByEvent = new Map();
  for (const list of baseRows) {
    const key = String(list.event_id);
    if (!baseByEvent.has(key)) baseByEvent.set(key, []);
    baseByEvent.get(key).push(list);
  }
  $("event-select").innerHTML = groups.map(([eventId, rows]) => {
    const all = baseByEvent.get(eventId) || rows;
    const confirmed = all.filter(isConfirmed).length;
    const event = rows[0];
    const dateFrom = String(event.date || "").slice(0, 10);
    const dateTo = String(event.date_to || "").slice(0, 10);
    const dateLabel = dateTo && dateTo !== dateFrom ? `${dateFrom}–${dateTo}` : dateFrom;
    return `<option value="${esc(eventId)}">${esc(dateLabel)} · #${esc(eventId)} · ${esc(event.event_title)} · ${confirmed}/${all.length}</option>`;
  }).join("") || `<option value="">Keine Wettkämpfe</option>`;
  $("event-select").value = selectedEventId || "";
}

function applyFilters() {
  const baseRows = filteredLists(false);
  const stateRows = filteredLists(true);
  const groups = eventGroups(stateRows);
  if (!groups.some(([eventId]) => eventId === selectedEventId)) {
    selectedEventId = groups[0]?.[0] || null;
  }
  renderEventSelect(groups, baseRows);
  visible = stateRows.filter((list) => String(list.event_id) === selectedEventId);
  visible.sort((a, b) =>
    String(a.stage_title || "").localeCompare(String(b.stage_title || ""), "de") ||
    String(a.source_file_name || a.source_type || "").localeCompare(
      String(b.source_file_name || b.source_type || ""), "de") ||
    String(a.source_document_id || "").localeCompare(String(b.source_document_id || "")) ||
    String(a.category).localeCompare(String(b.category), "de"));
  if (!visible.some((l) => l.id === selectedId)) selectedId = visible[0]?.id || null;
  renderQueue(baseRows.filter((list) => String(list.event_id) === selectedEventId));
  renderDetail();
}

function renderQueue(eventLists = filteredLists(false).filter(
  (list) => String(list.event_id) === selectedEventId)) {
  const confirmed = eventLists.filter(isConfirmed).length;
  const blockers = eventLists.filter((l) => l.blockers).length;
  $("progress").innerHTML = `<b>${confirmed}/${eventLists.length}</b> Kategorien dieses Events bestätigt` +
    (blockers ? ` · <span class="review-blocker">${blockers} mit Blockern</span>` : "");
  let priorSource = null;
  $("queue").innerHTML = visible.map((list) => {
    const state = isConfirmed(list) ? "confirmed" : isFlagged(list) ? "flagged" :
      list.blockers ? "blocked" : list.warnings ? "warning" : "clean";
    const automatic = isAutomaticallyConfirmed(list);
    const label = { confirmed: "✓", flagged: "⚑", blocked: "!", warning: "!", clean: "○" }[state];
    const sourceChanged = list.source_document_id !== priorSource;
    priorSource = list.source_document_id;
    const sourceLabel = [list.stage_title, list.source_type, list.source_file_name]
      .filter(Boolean).join(" · ");
    return `${sourceChanged ? `<div class="queue-source">${esc(sourceLabel || "Ergebnisquelle")}</div>` : ""}<button class="queue-item ${state} ${list.id === selectedId ? "active" : ""}"
      data-id="${esc(list.id)}">
      <span class="queue-state">${label}</span><span><b>${esc(list.category)}</b>
      <small>${automatic ? "automatisch bestätigt" : list.parser_blockers ? `${list.parser_blockers} Zeitfehler` : list.ranking_warnings ? "Rangprüfung" : list.blockers || list.warnings ? `${list.blockers || list.warnings} Hinweise` : "ohne automatischen Befund"}</small></span>
      <span class="queue-count">${list.blockers || list.warnings || ""}</span></button>`;
  }).join("") || `<p class="queue-empty">Keine Kategorien dieses Events in diesem Filter.</p>`;
  $("queue").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
    selectedId = button.dataset.id;
    renderQueue();
    renderDetail();
  }));
}

function fmtTime(seconds) {
  if (seconds == null) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` :
    `${m}:${String(s).padStart(2, "0")}`;
}

function resultTimeHtml(timeS, observedTime, status) {
  // ANNE's API exposes its duration as a bare numeric value.  The database
  // keeps that raw value in observed_time for provenance, but the review UI
  // must display the normalized seconds just like the public result list.
  const time = fmtTime(timeS);
  const normalizedStatus = status && !["ok", "unknown"].includes(status)
    ? status.toUpperCase() : "";
  if (time && normalizedStatus) {
    return `${esc(time)} <b class="review-leg-status">${esc(normalizedStatus)}</b>`;
  }
  if (time) return esc(time);
  if (normalizedStatus) return `<b class="review-leg-status">${esc(normalizedStatus)}</b>`;
  return esc(observedTime || (status === "unknown" ? "unknown" : ""));
}

function identityMappingHtml(r) {
  if (r.identity_state === "not_applicable") {
    return `<small class="review-mapping">Family · keine Personen-ID</small>`;
  }
  const basis = {
    "club-book-of-record": "Zuordnung: verifizierte Vereinsliste",
    "source-oefol-id": "Zuordnung: ÖFOL-ID aus Ergebnisquelle",
    "anne-registry-name-yob": "Zuordnung: ANNE-Register · Name + Geburtsjahr",
    "anne-registry-name-club": "Zuordnung: ANNE-Register · Name + aktueller Verein",
    "legacy-name-yob": "Zuordnung: Name + Jahrgang",
    "legacy-name": "Zuordnung: Name",
  }[r.identity_basis] || "Zuordnung: ungeklärt";
  const state = {
    resolved: "Identitätsstatus: aufgelöst",
    candidate: "Identitätsstatus: Kandidat – prüfen",
    unresolved: "Identitätsstatus: nicht aufgelöst",
    conflict: "Identitätsstatus: Konflikt",
  }[r.identity_state] || "Identitätsstatus: ungeklärt";
  const oefolIdentity = r.registry_oefol_ids
    ? `ÖFOL-ID aus ANNE-Register: ${r.registry_oefol_ids}`
    : r.observed_oefol_ids
      ? `ÖFOL-ID aus Ergebnisquelle: ${r.observed_oefol_ids}`
      : "keine ÖFOL-ID im Index";
  const registryIdentity = r.independently_confirmed_oefol_ids
    ? `ÖFOL-ID im Vereinsregister bestätigt: ${r.independently_confirmed_oefol_ids}`
    : "Vereinslisten-Bestätigung: keine";
  const championship = r.championship
    ? `ÖM/ÖSTM-Wertung: berücksichtigt (${r.championship})`
    : "";
  const eligibilityState = {
    eligible: "Eligibility: bestätigt",
    ineligible: "Eligibility: nicht berechtigt",
    provisional: "Eligibility: vorläufig",
    unknown: "Eligibility: ungeklärt",
  }[r.championship_eligibility_state] || "";
  const eligibilityBasis = {
    official_anne_championship: "ANNE-Ergebnisfeld",
    anne_aut_nationality: "ÖFOL-ID + Nationalität AUT",
    anne_foreign_override: "ANNE championshipEligibility=true",
    anne_foreign_no_override: "ausländisch, keine ANNE-Freigabe",
    official_championship_ranking: "offizielle Meisterschaftswertung",
    official_champion_annotation: "Meistertitel in der Quelle",
    official_championship_field: "offizielle ÖM/ÖSTM-Ergebnisliste",
    oefol_club_inference: "ÖFOL-Verein (nur vorläufig)",
    champion_boundary_inference: "ab Meisterrang abgeleitet",
    title_category_inference: "aus Bewerb + Kategorie abgeleitet",
    no_verified_eligibility_evidence: "kein verifizierter Nachweis",
  }[r.championship_eligibility_basis] || r.championship_eligibility_basis;
  const sourceScope = {
    full_field: "Quelle: vollständiges Feld",
    medal_places_only: "Quelle: nur Medaillenplätze",
    winner_only: "Quelle: nur Meisterzeile",
    inferred: "Quellenumfang: abgeleitet",
  }[r.championship_source_scope] || "";
  const eligibility = eligibilityState
    ? `${eligibilityState}${eligibilityBasis && eligibilityBasis !== "none" ? ` · ${eligibilityBasis}` : ""}${sourceScope ? ` · ${sourceScope}` : ""}`
    : "";
  const regional = r.regional_values
    ? `<small class="review-mapping championship">Landeswertung: ${esc(r.regional_values)}</small>`
    : "";
  return `<small class="review-mapping identity-state ${esc(r.identity_state)}">${esc(state)}</small>` +
    `<small class="review-mapping">${esc(basis)}</small>` +
    `<small class="review-mapping id">${esc(oefolIdentity)}</small>` +
    `<small class="review-mapping registry ${r.independently_confirmed_oefol_ids ? "verified" : ""}">${esc(registryIdentity)}</small>` +
    (championship ? `<small class="review-mapping championship">${esc(championship)}</small>` : "") +
    (eligibility ? `<small class="review-mapping championship ${esc(r.championship_eligibility_state)}">${esc(eligibility)}</small>` : "") + regional;
}

function clubMappingHtml(r) {
  const sourceClub = r.club || r.observed_club || "";
  if (!sourceClub) return "";
  if (!r.official_club) {
    return `${esc(sourceClub)}<small class="review-mapping unmapped">keine sichere ANNE-Vereinszuordnung</small>`;
  }
  const mapping = r.official_club === sourceClub
    ? "✓ ANNE-Verein"
    : `→ ${r.official_club}`;
  return `${esc(sourceClub)}<small class="review-mapping club">${esc(mapping)}</small>`;
}

function renderDetail() {
  const list = lists.find((l) => l.id === selectedId);
  if (!list) {
    $("review-detail").innerHTML = `<div class="review-placeholder">Filter abgeschlossen – oder links einen anderen Bereich wählen.</div>`;
    return;
  }
  const rows = query(`
    SELECT r.id, r.rank, r.status, r.out_of_competition, r.time_s, r.observed_time,
           r.person_id, r.observed_name, r.observed_club, r.club, r.official_club, r.result_kind,
           r.identity_basis, r.identity_state, r.observed_user_id, r.championship,
           r.championship_eligibility_state, r.championship_eligibility_basis,
           r.championship_source_scope,
           r.team_number, r.team_name, r.leg_number, r.leg_count,
           r.individual_status, r.team_status, r.team_time_s, r.observed_team_time,
           COALESCE(p.name, r.observed_name) AS mapped_name, r.national_rank,
           (SELECT GROUP_CONCAT(cj.short_name || CASE WHEN ce.regional_rank IS NOT NULL
                    THEN ' Rang ' || ce.regional_rank ELSE '' END, ', ')
              FROM championship_entry_result cer
              JOIN championship_entry ce ON ce.id = cer.championship_entry_id
              JOIN championship_instance ci ON ci.id = ce.championship_instance_id
              JOIN championship_jurisdiction cj ON cj.code = ci.jurisdiction
             WHERE cer.result_id = r.id) AS regional_values,
           (SELECT GROUP_CONCAT(pi.identifier, ', ') FROM person_identifier pi
            WHERE pi.person_id = r.person_id AND pi.scheme = 'oefol_id'
              AND pi.identifier_state = 'authoritative' AND pi.source = 'anne-user-registry') AS registry_oefol_ids,
           (SELECT GROUP_CONCAT(pi.identifier, ', ') FROM person_identifier pi
            WHERE pi.person_id = r.person_id AND pi.scheme = 'oefol_id'
              AND pi.identifier_state = 'authoritative' AND pi.source = 'result-observation') AS observed_oefol_ids,
           (SELECT GROUP_CONCAT(pi.identifier, ', ') FROM person_identifier pi
            WHERE pi.person_id = r.person_id AND pi.scheme = 'oefol_id'
              AND pi.identifier_state = 'independently_confirmed') AS independently_confirmed_oefol_ids,
           GROUP_CONCAT(ai.code, ', ') AS issue_codes
    FROM result r LEFT JOIN person p ON p.id = r.person_id
    LEFT JOIN audit_issue ai ON ai.result_id = r.id
    WHERE r.result_list_id = ? GROUP BY r.id
    ORDER BY r.rank IS NULL, r.rank, COALESCE(r.team_number, r.team_name, r.club),
             r.leg_number, r.time_s`, [list.id]);
  const issues = query(`SELECT severity, code, message FROM audit_issue
    WHERE result_list_id = ? ORDER BY CASE severity WHEN 'blocker' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, code`, [list.id]);
  const regionalMappings = query(`
    SELECT m.jurisdiction, cj.short_name, m.canonical_category, m.state AS mapping_state,
           m.evidence_kind, m.evidence_text, m.partition_required,
           ci.id AS instance_id, ci.state AS instance_state,
           ci.input_fingerprint AS instance_fingerprint,
           (SELECT COUNT(*) FROM championship_entry ce
             WHERE ce.source_result_list_id = m.result_list_id
               AND ce.championship_instance_id = ci.id) AS entries
      FROM regional_category_mapping m
      JOIN championship_jurisdiction cj ON cj.code = m.jurisdiction
      JOIN result_list rl ON rl.id = m.result_list_id
      JOIN championship_instance ci ON ci.jurisdiction = m.jurisdiction
       AND ci.stage_id = rl.stage_id AND ci.category_key = m.category_key
       AND ci.championship_type = 'LMS'
     WHERE m.result_list_id = ? ORDER BY m.jurisdiction, m.category_key`, [list.id]);
  const byDim = decisionMap(list.id);
  const dimensions = requiredDimensions(list);
  const automaticallyConfirmed = isAutomaticallyConfirmed(list);
  const cleanInSource = visible.filter((candidate) =>
    candidate.source_document_id === list.source_document_id &&
    !candidate.blockers && !candidate.warnings && !isConfirmed(candidate));
  const issueHtml = issues.length ? `<div class="review-issues">${issues.map((i) =>
    `<div class="review-issue ${esc(i.severity)}"><b>${i.severity === "blocker" ? "Blocker" : "Hinweis"}</b> ${esc(i.message)}</div>`).join("")}</div>` :
    `<div class="review-clean">Automatische Prüfungen ohne Befund · automatisch bestätigt.</div>`;
  const sourceUrl = list.snapshot_path ? `/review-source?id=${encodeURIComponent(list.id)}` : list.source_url;
  const parsedUnits = [];
  const parsedUnitIndex = new Map();
  for (const row of rows) {
    const isTeam = ["relay", "team", "pair"].includes(row.result_kind);
    const key = !isTeam ? null : row.team_number
      ? `${row.result_kind}:number:${row.team_number}`
      : row.result_kind === "pair"
        ? `${row.result_kind}:rank:${row.rank ?? ""}:status:${row.status}:time:${row.time_s ?? ""}:club:${row.club || ""}`
        : `${row.result_kind}:name:${row.team_name || row.club || row.id}`;
    if (!key) parsedUnits.push({ team: false, rows: [row] });
    else if (!parsedUnitIndex.has(key)) {
      parsedUnitIndex.set(key, parsedUnits.length);
      parsedUnits.push({ team: true, rows: [row] });
    } else parsedUnits[parsedUnitIndex.get(key)].rows.push(row);
  }
  const parsedRowsHtml = parsedUnits.map((unit) => {
    if (!unit.team) {
      const r = unit.rows[0];
      return `<tr class="${r.issue_codes ? "review-row-issue" : ""}">
        <td>${r.out_of_competition ? "AK" : r.rank ?? ""}</td><td></td>
        <td><b>${r.person_id == null ? esc(r.observed_name) : `<a href="index.html#/runner/${r.person_id}" target="_blank">${esc(r.observed_name)}</a>`}</b>${r.mapped_name !== r.observed_name ? `<small>→ ${esc(r.mapped_name)}</small>` : ""}${identityMappingHtml(r)}</td>
        <td>${esc(r.status)}${r.issue_codes ? `<small>${esc(r.issue_codes)}</small>` : ""}</td>
        <td>${resultTimeHtml(r.time_s, r.observed_time, r.status)}</td><td>${clubMappingHtml(r)}</td></tr>`;
    }
    const team = unit.rows[0];
    const label = team.result_kind === "pair"
      ? unit.rows.map((r) => r.observed_name).join(" + ")
      : `${team.team_number ? `#${team.team_number} ` : ""}${team.team_name || team.club || "Team"}`;
    const total = resultTimeHtml(
      team.team_time_s, team.observed_team_time, team.team_status || team.status);
    return `<tr class="review-team-row"><td>${team.out_of_competition ? "AK" : team.rank ?? ""}</td>
      <td colspan="2">${esc(label)}${team.result_kind === "pair" ? " <small>Paar</small>" : ""}</td><td>${esc(team.team_status || team.status)}</td>
      <td>${total}</td><td>${clubMappingHtml(team)}</td></tr>` +
      unit.rows.filter((r) => r.person_id != null || r.observed_name).map((r) => `<tr class="review-team-member ${r.issue_codes ? "review-row-issue" : ""}">
        <td></td><td>${r.result_kind === "relay" ? `Leg ${r.leg_number || "?"}/${r.leg_count || unit.rows.length}` : ""}</td>
        <td><b>${r.person_id == null ? esc(r.observed_name) : `<a href="index.html#/runner/${r.person_id}" target="_blank">${esc(r.observed_name)}</a>`}</b>${r.mapped_name !== r.observed_name ? `<small>→ ${esc(r.mapped_name)}</small>` : ""}${identityMappingHtml(r)}</td>
        <td>${r.issue_codes ? `<small>${esc(r.issue_codes)}</small>` : ""}</td>
        <td>${r.result_kind === "relay" ? resultTimeHtml(r.time_s, r.observed_time, r.individual_status || r.status) : ""}</td>
        <td></td></tr>`).join("");
  }).join("");
  $("review-detail").innerHTML = `
    <div class="review-titlebar">
      <div><p class="review-kicker">${esc(list.date || "")} · ${esc(list.source_type)}</p>
      <h1>${esc(list.category)}</h1><p>${esc(list.event_title)}${list.stage_title ? ` · ${esc(list.stage_title)}` : ""}</p></div>
      <a href="index.html#/event/${list.event_id}" target="_blank" class="chip">Öffentliche Ansicht ↗</a>
    </div>
    <div class="review-facts">
      <span><b>${list.declared_starters ?? "–"}</b> Quelle</span>
      <span><b>${list.parsed_entries}</b> geparst</span>
      <span><b>${rows.length}</b> Datenzeilen</span>
      <span><b>${list.timed_rows}</b> Zeiten</span>
      <span><b>${list.ranked_rows}</b> Ränge</span>
      ${list.is_national ? `<span class="badge champ-badge">ÖM/ÖSTM</span>` : ""}
      ${list.is_regional ? `<span class="badge champ-badge">${esc(list.regional_jurisdictions)} Landes-MS</span>` : ""}
      ${list.family_rows ? `<span class="badge">Family · Identität n/a</span>` : ""}
    </div>
    ${regionalMappings.length ? `<div class="review-issues regional-mappings">${regionalMappings.map((m) =>
      `<div class="review-issue ${m.instance_state === "confirmed" ? "info" : "warning"}"><b>${esc(m.short_name)} · ${esc(m.canonical_category)}</b> · ${m.instance_state === "confirmed" ? "Quellenzuordnung bestätigt" : "Kandidat"} · ${esc(m.entries)} zugeordnete Ergebnisse<br><small>${esc(m.evidence_kind)}: ${esc(m.evidence_text)}${m.partition_required ? " · nach Landesverein getrennt" : ""}</small>${m.instance_state === "candidate" ? `<div class="regional-decision"><button data-championship-id="${esc(m.instance_id)}" data-fingerprint="${esc(m.instance_fingerprint)}" data-state="confirmed" ${!writable ? "disabled" : ""}>Landeszuordnung bestätigen</button><button data-championship-id="${esc(m.instance_id)}" data-fingerprint="${esc(m.instance_fingerprint)}" data-state="rejected" ${!writable ? "disabled" : ""}>Keine Landeswertung</button></div>` : ""}</div>`).join("")}</div>` : ""}
    ${issueHtml}
    <div class="review-dimensions">${dimensions.map((d) => {
      const a = byDim.get(d);
      const state = a?.state || (automaticallyConfirmed ? "confirmed" : "open");
      const label = { completeness: "vollständig", parsing: "Parsing/Status", identity: "Identitäten",
        ranking: "Rang/Kategorie", rules: "Medaillenregeln" }[d];
      return `<span class="dimension ${esc(state)}">${state === "confirmed" ? "✓" : state === "flagged" ? "⚑" : "○"} ${label}${automaticallyConfirmed && !a ? " · automatisch" : ""}</span>`;
    }).join("")}</div>
    <div class="review-actions">
      <button id="confirm-list" class="review-primary ${list.blockers ? "review-confirm-blocked" : ""}" ${!writable ? "disabled" : ""}>${list.blockers ? `Trotz ${list.blockers} Blocker bestätigen` : "Bestätigen &amp; weiter"} <kbd>A</kbd></button>
      ${cleanInSource.length > 1 ? `<button id="confirm-source" class="review-primary review-batch" ${!writable ? "disabled" : ""}>${cleanInSource.length} saubere Klassen dieser Quelle <kbd>⇧A</kbd></button>` : ""}
      <button id="flag-list" class="review-secondary" ${!writable ? "disabled" : ""}>Zur Nacharbeit markieren <kbd>F</kbd></button>
      <button id="previous-list" class="review-secondary">←</button><button id="next-list" class="review-secondary">→</button>
      <span id="review-save-status"></span>
    </div>
    <div class="review-workspace">
      <div class="review-source">
        <div class="pane-head">Originalquelle ${sourceUrl ? `<a href="${esc(sourceUrl)}" target="_blank">separat öffnen ↗</a>` : ""}</div>
        ${sourceUrl ? `<iframe src="${esc(sourceUrl)}" title="Originalquelle"></iframe>` : `<div class="review-placeholder">Kein lokaler Snapshot; Quellenlink fehlt.</div>`}
      </div>
      <div class="review-parsed"><div class="pane-head">Geparstes Ergebnis</div>
        <div class="review-table-wrap"><table><thead><tr><th>Pl</th><th>Team / Leg</th><th>Name / Identität</th><th>Status Team</th><th>Zeit / Leg-Status</th><th>Verein / Mapping</th></tr></thead>
        <tbody>${parsedRowsHtml}</tbody></table></div>
      </div>
    </div>`;
  $("confirm-list").addEventListener("click", () => saveCurrent("confirmed"));
  document.querySelectorAll("[data-championship-id]").forEach((button) =>
    button.addEventListener("click", () => saveChampionshipDecision(
      button.dataset.championshipId, button.dataset.fingerprint,
      button.dataset.state, button)));
  $("confirm-source")?.addEventListener("click", () => saveSourceBatch(cleanInSource));
  $("flag-list").addEventListener("click", () => saveCurrent("flagged"));
  $("previous-list").addEventListener("click", () => move(-1));
  $("next-list").addEventListener("click", () => move(1));
}

async function saveAssertion(list, dimension, state) {
  const assertion = {
    scope_type: "result_list", scope_key: list.id, dimension, state,
    input_fingerprint: list.input_fingerprint, reviewer: "local-admin",
    reviewed_at: new Date().toISOString(), note: state === "flagged" ? "Manuell zur Nacharbeit markiert" : "",
  };
  const response = await fetch("/api/review", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(assertion) });
  if (!response.ok) throw new Error((await response.json()).error || "Speichern fehlgeschlagen");
  decisions = decisions.filter((a) => !(a.scope_key === list.id && a.dimension === dimension));
  decisions.push(assertion);
}

async function saveCurrent(state) {
  const list = lists.find((l) => l.id === selectedId);
  if (!list || !writable) return;
  const status = $("review-save-status");
  status.textContent = "speichert …";
  try {
    if (state === "confirmed") {
      for (const dimension of requiredDimensions(list)) {
        const dimState = dimension === "identity" && list.family_rows === list.parsed_rows ? "not_applicable" : "confirmed";
        await saveAssertion(list, dimension, dimState);
      }
    } else {
      await saveAssertion(list, "parsing", "flagged");
    }
    const currentIndex = visible.findIndex((candidate) => candidate.id === list.id);
    const next = [...visible.slice(currentIndex + 1), ...visible.slice(0, currentIndex)]
      .find((candidate) => candidate.id !== list.id &&
        matchesQueueState(candidate, $("queue-state").value));
    if (next) {
      selectedId = next.id;
    } else {
      const nextEvent = eventGroups(filteredLists(true))
        .find(([eventId]) => eventId !== String(list.event_id));
      selectedEventId = nextEvent?.[0] || null;
      selectedId = null;
    }
    applyFilters();
  } catch (error) {
    status.textContent = error.message;
  }
}

async function saveSourceBatch(batch) {
  if (!writable || !batch.length) return;
  const status = $("review-save-status");
  status.textContent = `${batch.length} Listen werden gespeichert …`;
  try {
    for (const list of batch) {
      for (const dimension of requiredDimensions(list)) {
        const dimState = dimension === "identity" && list.family_rows === list.parsed_rows ? "not_applicable" : "confirmed";
        await saveAssertion(list, dimension, dimState);
      }
    }
    status.textContent = `${batch.length} Listen gespeichert ✓`;
    applyFilters();
  } catch (error) {
    status.textContent = error.message;
  }
}

function move(delta, preferOpen = false) {
  let idx = visible.findIndex((l) => l.id === selectedId);
  if (preferOpen && delta > 0) {
    const next = visible.slice(idx + 1).find((l) => !isConfirmed(l)) || visible.find((l) => !isConfirmed(l));
    if (next) idx = visible.indexOf(next) - 1;
  }
  const target = visible[idx + delta];
  if (!target) return;
  selectedId = target.id;
  renderQueue();
  renderDetail();
  document.querySelector(`.queue-item[data-id="${CSS.escape(selectedId)}"]`)?.scrollIntoView({ block: "nearest" });
}

async function boot() {
  const SQL = await initSqlJs({ locateFile: (file) => `https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.10.3/${file}` });
  db = new SQL.Database(new Uint8Array(await (await fetch("data/results.db")).arrayBuffer()));
  lists = query(`
    SELECT rl.*, e.id AS event_id, e.title AS event_title, e.date_from AS date,
           e.date_to,
           s.title AS stage_title, sd.source_type, sd.source_url, sd.snapshot_path,
           sd.file_name AS source_file_name,
           COALESCE(ai.blockers, 0) AS blockers, COALESCE(ai.warnings, 0) AS warnings,
           COALESCE(rr.is_national, 0) AS is_national, COALESCE(rr.family_rows, 0) AS family_rows,
           COALESCE(rr.timed_rows, 0) AS timed_rows, COALESCE(rr.ranked_rows, 0) AS ranked_rows,
           COALESCE(ai.parser_blockers, 0) AS parser_blockers,
           COALESCE(ai.ranking_warnings, 0) AS ranking_warnings,
           COALESCE(reg.is_regional, 0) AS is_regional,
           COALESCE(reg.regional_jurisdictions, '') AS regional_jurisdictions,
           COALESCE(reg.regional_confirmed, 0) AS regional_confirmed,
           COALESCE(reg.regional_candidates, 0) AS regional_candidates,
           COALESCE(reg.regional_entries, 0) AS regional_entries
    FROM result_list rl JOIN stage s ON s.id = rl.stage_id JOIN event e ON e.id = s.event_id
    JOIN source_document sd ON sd.id = rl.source_document_id
    LEFT JOIN (SELECT result_list_id, SUM(severity='blocker') AS blockers,
                      SUM(severity='warning') AS warnings,
                      SUM(code='time_text_unparsed') AS parser_blockers,
                      SUM(code='partial_ranking_coverage') AS ranking_warnings
               FROM audit_issue GROUP BY result_list_id) ai
      ON ai.result_list_id = rl.id
    LEFT JOIN (SELECT result_list_id, MAX(championship IS NOT NULL) AS is_national,
                      SUM(result_kind='family') AS family_rows,
                      SUM(status='ok' AND time_s IS NOT NULL) AS timed_rows,
                      SUM(rank IS NOT NULL) AS ranked_rows
               FROM result GROUP BY result_list_id) rr
      ON rr.result_list_id = rl.id
    LEFT JOIN (
      SELECT m.result_list_id, 1 AS is_regional,
             GROUP_CONCAT(DISTINCT m.jurisdiction) AS regional_jurisdictions,
             SUM(m.state='confirmed') AS regional_confirmed,
             SUM(m.state='candidate') AS regional_candidates,
             (SELECT COUNT(*) FROM championship_entry ce
               WHERE ce.source_result_list_id = m.result_list_id) AS regional_entries
        FROM regional_category_mapping m WHERE m.state!='rejected'
       GROUP BY m.result_list_id) reg ON reg.result_list_id = rl.id
    WHERE rl.parsed_rows > 0`);
  try {
    const response = await fetch("/api/review");
    if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) throw new Error();
    decisions = (await response.json()).assertions || [];
    writable = true;
    $("save-mode").textContent = "lokal · Änderungen werden gespeichert";
    $("save-mode").classList.add("writable");
  } catch {
    decisions = query("SELECT * FROM verification_assertion");
    $("save-mode").textContent = "nur lesen · lokal mit site/serve.py öffnen";
  }
  ["campaign", "queue-state", "queue-date"].forEach((id) => $(id).addEventListener("change", applyFilters));
  $("event-select").addEventListener("change", () => {
    selectedEventId = $("event-select").value || null;
    selectedId = null;
    applyFilters();
  });
  $("queue-search").addEventListener("input", applyFilters);
  applyFilters();
}

document.addEventListener("keydown", (event) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  if (event.shiftKey && event.key.toLowerCase() === "a") {
    event.preventDefault();
    const list = lists.find((l) => l.id === selectedId);
    const batch = visible.filter((candidate) => list && candidate.source_document_id === list.source_document_id &&
      !candidate.blockers && !candidate.warnings && !isConfirmed(candidate));
    saveSourceBatch(batch);
    return;
  }
  if (event.key.toLowerCase() === "a") saveCurrent("confirmed");
  if (event.key.toLowerCase() === "f") saveCurrent("flagged");
  if (event.key.toLowerCase() === "j" || event.key === "ArrowDown") move(1);
  if (event.key.toLowerCase() === "k" || event.key === "ArrowUp") move(-1);
});

boot().catch((error) => { $("review-detail").innerHTML = `<p>Prüfoberfläche konnte nicht geladen werden: ${esc(error.message)}</p>`; });
