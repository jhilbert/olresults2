"use strict";

let db;
let lists = [];
let visible = [];
let decisions = [];
let selectedId = null;
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
  if (list.is_national) base.push("rules");
  return base;
}

function isConfirmed(list) {
  const byDim = decisionMap(list.id);
  return requiredDimensions(list).every((d) =>
    ["confirmed", "not_applicable"].includes(byDim.get(d)?.state) &&
    byDim.get(d)?.input_fingerprint === list.input_fingerprint);
}

function isFlagged(list) {
  return [...decisionMap(list.id).values()].some((a) => a.state === "flagged");
}

function isVienna(list) {
  return Boolean(list.is_vienna_candidate);
}

function applyFilters() {
  const campaign = $("campaign").value;
  const state = $("queue-state").value;
  const needle = $("queue-search").value.trim().toLocaleLowerCase("de");
  visible = lists.filter((list) => {
    if (campaign === "national" && !list.is_national) return false;
    if (campaign === "vienna" && !isVienna(list)) return false;
    if (state === "open" && isConfirmed(list)) return false;
    if (state === "quality" && !(list.parser_blockers || list.ranking_warnings)) return false;
    if (state === "issues" && !(list.blockers || list.warnings)) return false;
    if (state === "confirmed" && !isConfirmed(list)) return false;
    if (needle && !`${list.event_title} ${list.stage_title} ${list.category}`
      .toLocaleLowerCase("de").includes(needle)) return false;
    return true;
  });
  visible.sort((a, b) =>
    Number(isConfirmed(a)) - Number(isConfirmed(b)) ||
    b.parser_blockers - a.parser_blockers || b.ranking_warnings - a.ranking_warnings ||
    b.blockers - a.blockers || b.warnings - a.warnings ||
    b.is_national - a.is_national || String(b.date || "").localeCompare(String(a.date || "")) ||
    String(a.category).localeCompare(String(b.category), "de"));
  renderQueue();
  if (!visible.some((l) => l.id === selectedId)) selectedId = visible[0]?.id || null;
  renderDetail();
}

function renderQueue() {
  const confirmed = visible.filter(isConfirmed).length;
  const blockers = visible.filter((l) => l.blockers).length;
  $("progress").innerHTML = `<b>${confirmed}/${visible.length}</b> bestätigt` +
    (blockers ? ` · <span class="review-blocker">${blockers} mit Blockern</span>` : "");
  $("queue").innerHTML = visible.map((list) => {
    const state = isConfirmed(list) ? "confirmed" : isFlagged(list) ? "flagged" :
      list.blockers ? "blocked" : list.warnings ? "warning" : "clean";
    const label = { confirmed: "✓", flagged: "⚑", blocked: "!", warning: "!", clean: "○" }[state];
    return `<button class="queue-item ${state} ${list.id === selectedId ? "active" : ""}"
      data-id="${esc(list.id)}">
      <span class="queue-state">${label}</span><span><b>${esc(list.category)}</b>
      <small>${esc(list.date || "")} · ${esc(list.event_title)}${list.parser_blockers ? ` · ${list.parser_blockers} Zeitfehler` : list.ranking_warnings ? " · Rangprüfung" : ""}</small></span>
      <span class="queue-count">${list.blockers || list.warnings || ""}</span></button>`;
  }).join("") || `<p class="queue-empty">Keine Listen in diesem Filter.</p>`;
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
  return `<small class="review-mapping identity-state ${esc(r.identity_state)}">${esc(state)}</small>` +
    `<small class="review-mapping">${esc(basis)}</small>` +
    `<small class="review-mapping id">${esc(oefolIdentity)}</small>` +
    `<small class="review-mapping registry ${r.independently_confirmed_oefol_ids ? "verified" : ""}">${esc(registryIdentity)}</small>` +
    (championship ? `<small class="review-mapping championship">${esc(championship)}</small>` : "");
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
           r.team_number, r.team_name, r.leg_number, r.leg_count,
           r.individual_status, r.team_status, r.team_time_s, r.observed_team_time,
           COALESCE(p.name, r.observed_name) AS mapped_name, r.national_rank,
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
  const byDim = decisionMap(list.id);
  const dimensions = requiredDimensions(list);
  const cleanInSource = visible.filter((candidate) =>
    candidate.source_document_id === list.source_document_id &&
    !candidate.blockers && !candidate.warnings && !isConfirmed(candidate));
  const issueHtml = issues.length ? `<div class="review-issues">${issues.map((i) =>
    `<div class="review-issue ${esc(i.severity)}"><b>${i.severity === "blocker" ? "Blocker" : "Hinweis"}</b> ${esc(i.message)}</div>`).join("")}</div>` :
    `<div class="review-clean">Automatische Prüfungen ohne Befund.</div>`;
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
        <td>${esc(r.observed_time || fmtTime(r.time_s))}</td><td>${clubMappingHtml(r)}</td></tr>`;
    }
    const team = unit.rows[0];
    const label = team.result_kind === "pair"
      ? unit.rows.map((r) => r.observed_name).join(" + ")
      : `${team.team_number ? `#${team.team_number} ` : ""}${team.team_name || team.club || "Team"}`;
    const total = team.observed_team_time || fmtTime(team.team_time_s) ||
      (team.team_status !== "ok" ? team.team_status : "");
    return `<tr class="review-team-row"><td>${team.out_of_competition ? "AK" : team.rank ?? ""}</td>
      <td colspan="2">${esc(label)}${team.result_kind === "pair" ? " <small>Paar</small>" : ""}</td><td>${esc(team.team_status || team.status)}</td>
      <td>${esc(total)}</td><td>${clubMappingHtml(team)}</td></tr>` +
      unit.rows.map((r) => `<tr class="review-team-member ${r.issue_codes ? "review-row-issue" : ""}">
        <td></td><td>${r.result_kind === "relay" ? `Leg ${r.leg_number || "?"}/${r.leg_count || unit.rows.length}` : ""}</td>
        <td><b>${r.person_id == null ? esc(r.observed_name) : `<a href="index.html#/runner/${r.person_id}" target="_blank">${esc(r.observed_name)}</a>`}</b>${r.mapped_name !== r.observed_name ? `<small>→ ${esc(r.mapped_name)}</small>` : ""}${identityMappingHtml(r)}</td>
        <td>${r.issue_codes ? `<small>${esc(r.issue_codes)}</small>` : ""}</td>
        <td>${r.result_kind === "relay" ? (r.individual_status && r.individual_status !== "ok" ? `<b>${esc(r.individual_status)}</b>` : esc(r.observed_time || fmtTime(r.time_s))) : ""}</td>
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
      ${list.family_rows ? `<span class="badge">Family · Identität n/a</span>` : ""}
    </div>
    ${issueHtml}
    <div class="review-dimensions">${dimensions.map((d) => {
      const a = byDim.get(d);
      const label = { completeness: "vollständig", parsing: "Parsing/Status", identity: "Identitäten",
        ranking: "Rang/Kategorie", rules: "Medaillenregeln" }[d];
      return `<span class="dimension ${esc(a?.state || "open")}">${a?.state === "confirmed" ? "✓" : a?.state === "flagged" ? "⚑" : "○"} ${label}</span>`;
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
    status.textContent = "gespeichert ✓";
    renderQueue();
    move(1, true);
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
           s.title AS stage_title, sd.source_type, sd.source_url, sd.snapshot_path,
           COALESCE(ai.blockers, 0) AS blockers, COALESCE(ai.warnings, 0) AS warnings,
           COALESCE(rr.is_national, 0) AS is_national, COALESCE(rr.family_rows, 0) AS family_rows,
           COALESCE(rr.timed_rows, 0) AS timed_rows, COALESCE(rr.ranked_rows, 0) AS ranked_rows,
           COALESCE(ai.parser_blockers, 0) AS parser_blockers,
           COALESCE(ai.ranking_warnings, 0) AS ranking_warnings,
           COALESCE(ci.is_vienna_candidate, 0) AS is_vienna_candidate
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
    LEFT JOIN (SELECT stage_id, category, MAX(jurisdiction='WIEN' AND state!='rejected') AS is_vienna_candidate
               FROM championship_instance GROUP BY stage_id, category) ci
      ON ci.stage_id = rl.stage_id AND ci.category = rl.category
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
  ["campaign", "queue-state"].forEach((id) => $(id).addEventListener("change", applyFilters));
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
