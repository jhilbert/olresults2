/* OL Results — static frontend over site/data/results.db via sql.js */
"use strict";

let db = null;
const app = document.getElementById("app");

/* ---------- helpers ---------- */

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// A standalone "Bahn X" ("Bahn 3", "BAHN A") groups every category that ran
// the same physical course into one list with no age/gender split at all -
// there's no official ranking behind it, just a shared route. This is
// distinct from a combined name like "H 19-, Bahn A" or "Damen Bahn A",
// where the leading part *is* a real category and "Bahn" just records
// which course that category happened to run - those stay normal results.
const isBahn = (cat) => /^bahn/i.test((cat || "").trim());

// Knock-out sprint qualification/consolation rounds ("H21-E - Viertelfinale
// 5", "... Halbfinale B", "H55- - B-Finale") aren't a final ranking - only
// the event's own "... - Finale" category is - so a heat placement must
// never count as a medal/podium/win anywhere on the site. Mirrors the
// viertelfinale|halbfinale|b-finale part of EXCLUDE_CAT_RE in build_db.py.
const isKoHeat = (cat) => /viertelfinale|halbfinale|b-finale/i.test((cat || "").trim());

// Rough age-group read of a category name, for the event page's ÖM/ÖSTM
// "Jugend"/"Senioren" badge only - a display hint, not authoritative
// (the real eligibility computation lives in build_db.py and is already
// baked into which categories actually carry a championship tag). Both a
// spelled-out floor ("ab 45") and the compact "NN-" trailing-dash form
// ("H45-") mean "and older" - a LEADING dash ("H-12") means the opposite,
// an age ceiling, so the dash's side of the number is what disambiguates
// them, not just its presence.
const categoryAgeGroup = (cat) => {
  const c = cat || "";
  const m = c.match(/\d{1,3}/);
  if (!m) return null;
  const age = +m[0];
  if (/\bab\s*\d+/i.test(c) || /\d\s*-\s*(?!\d)/.test(c)) return age >= 21 ? "senior" : null;
  return age <= 20 ? "youth" : null;
};

// Ski-O's competition calendar runs Nov/Dec-of-the-prior-year through the
// following year (a "winter season"), not the plain calendar year every
// other discipline uses - confirmed by ANNE's own event titles, e.g. "ÖM/
// ÖSM Staffel 2013" actually held on 2012-12-22. The club calls this a
// runner's/event's "Wertungsjahr" (scoring year) - a November or December
// Ski-O race counts toward NEXT year's Wertungsjahr, not the calendar year
// it was actually run in. Every place on the site that groups or filters
// results by year needs this, not just the Medaillenspiegel, or the same
// Nov/Dec race would land in a different "year" on a runner's own profile
// than on their club's medal count.
const seasonYear = (dateStr, sportType) => {
  if (!dateStr) return "";
  const y = +dateStr.slice(0, 4), m = +dateStr.slice(5, 7);
  return String(sportType === "skiOrienteering" && (m === 11 || m === 12) ? y + 1 : y);
};

function fmtTime(s) {
  if (s == null) return "";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
           : `${m}:${String(sec).padStart(2, "0")}`;
}

function fmtDate(d) {
  if (!d) return "";
  const [y, m, day] = d.split("-");
  return `${day}.${m}.${y}`;
}

function fmtPct(behind, winner) {
  if (behind == null || !winner) return "";
  const pct = (behind / winner) * 100;
  const cls = pct === 0 ? "pct-good" : "";
  return `<span class="${cls}">+${pct.toFixed(1)}%</span>`;
}

function query(sql, params = []) {
  const stmt = db.prepare(sql);
  stmt.bind(params);
  const rows = [];
  while (stmt.step()) rows.push(stmt.getAsObject());
  stmt.free();
  return rows;
}

function rankCell(r) {
  if (r.out_of_competition) return `<span class="status ooc" title="außer Konkurrenz">AK</span>`;
  if (r.status !== "ok") return `<span class="status">${esc(r.status)}</span>`;
  if (r.rank == null) return "";
  return `<span class="rank ${r.rank === 1 ? "rank-1" : ""}">${r.rank}</span>` +
         (r.starters ? `<span class="of">/${r.classified}</span>` : "");
}

/* ---------- discipline filter (OL / SkiO / MTBO) ---------- */

// event.sport_type, as ANNE reports it, is the only per-event discipline
// signal in the schema - there's no separate per-stage field, so every
// stage of a multi-day event shares its event's one sport_type. A legacy
// event ANNE never classified (sport_type NULL) or the rare
// trailOrienteering pass every filter state unfiltered rather than
// disappearing just because they don't map onto one of the three buttons.
const DISCIPLINES = [
  ["footOrienteering", "OL"],
  ["skiOrienteering", "SkiO"],
  ["mountainbikeOrienteering", "MTBO"],
];
const DISCIPLINE_STORAGE_KEY = "olr-disciplines";

let disciplineFilter = new Set(DISCIPLINES.map(([v]) => v));
try {
  const saved = JSON.parse(localStorage.getItem(DISCIPLINE_STORAGE_KEY) || "null");
  if (Array.isArray(saved) && saved.length) disciplineFilter = new Set(saved);
} catch { /* malformed storage - fall back to all enabled */ }

// A SQL fragment excluding only the currently-disabled disciplines, built
// so every list query can apply the filter itself (rather than fetching
// everything and discarding rows in JS) - several of these queries already
// have a LIMIT or feed a count shown to the user, both of which need the
// filter applied before that point, not after.
function disciplineWhere(col) {
  const disabled = DISCIPLINES.map(([v]) => v).filter((v) => !disciplineFilter.has(v));
  if (!disabled.length) return { sql: "", params: [] };
  return { sql: ` AND (${col} IS NULL OR ${col} NOT IN (${disabled.map(() => "?").join(",")}))`,
           params: disabled };
}

function saveDisciplineFilter() {
  localStorage.setItem(DISCIPLINE_STORAGE_KEY, JSON.stringify([...disciplineFilter]));
}

function setupDisciplineFilter() {
  const btn = document.getElementById("discipline-filter-btn");
  const overlay = document.getElementById("discipline-overlay");

  const renderChecks = () => {
    overlay.innerHTML = DISCIPLINES.map(([value, label]) => `
      <label class="discipline-check">
        <input type="checkbox" value="${value}" ${disciplineFilter.has(value) ? "checked" : ""}>
        ${label}
      </label>`).join("");
  };
  const updateBtnState = () => btn.classList.toggle("active", disciplineFilter.size < DISCIPLINES.length);

  renderChecks();
  updateBtnState();

  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    overlay.hidden = !overlay.hidden;
  });
  overlay.addEventListener("change", (ev) => {
    const cb = ev.target.closest("input[type=checkbox]");
    if (!cb) return;
    if (cb.checked) disciplineFilter.add(cb.value); else disciplineFilter.delete(cb.value);
    // never allow an empty filter - that would just hide everything, which
    // is strictly worse than not filtering at all
    if (disciplineFilter.size === 0) {
      disciplineFilter = new Set(DISCIPLINES.map(([v]) => v));
      renderChecks();
    }
    saveDisciplineFilter();
    updateBtnState();
    runnersCache = null;  // discipline-dependent, same as a fresh db load
    clubsCache = null;
    route();
  });
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest("#discipline-overlay") && !ev.target.closest("#discipline-filter-btn")) {
      overlay.hidden = true;
    }
  });
}

/* ---------- views ---------- */

function viewHome() {
  const [s] = query(`SELECT
    (SELECT COUNT(*) FROM result) AS results,
    (SELECT COUNT(*) FROM person) AS persons,
    (SELECT COUNT(DISTINCT event_id) FROM stage s JOIN result r ON r.stage_id = s.id) AS events`);
  const dw = disciplineWhere("e.sport_type");
  const recent = query(`
    SELECT e.id, e.title, e.date_from, e.location, COUNT(r.id) AS n
    FROM event e JOIN stage s ON s.event_id = e.id JOIN result r ON r.stage_id = s.id
    WHERE 1=1${dw.sql}
    GROUP BY e.id ORDER BY e.date_from DESC LIMIT 15`, dw.params);

  app.innerHTML = `
    <h1>Orientierungslauf-Ergebnisse</h1>
    <p class="sub">Ergebnisarchiv österreichischer OL-Wettkämpfe — Läuferprofile, Kategorien, Zeitrückstände.</p>
    <div class="stats">
      <div class="stat"><b>${s.results.toLocaleString("de-AT")}</b><span>Ergebnisse</span></div>
      <a class="stat" href="#/runners"><b>${s.persons.toLocaleString("de-AT")}</b><span>Läufer:innen</span></a>
      <a class="stat" href="#/events"><b>${s.events.toLocaleString("de-AT")}</b><span>Wettkämpfe</span></a>
    </div>
    <h2>Neueste Ergebnisse</h2>
    <table>
      <thead><tr><th>Datum</th><th>Wettkampf</th><th class="hide-sm">Ort</th><th class="num">Ergebnisse</th></tr></thead>
      <tbody>${recent.map((e) => `
        <tr>
          <td class="dim">${fmtDate(e.date_from)}</td>
          <td><a href="#/event/${e.id}">${esc(e.title)}</a></td>
          <td class="hide-sm dim">${esc(e.location || "")}</td>
          <td class="num">${e.n}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function viewRunner(id, year) {
  let [p] = query("SELECT * FROM person WHERE id = ?", [id]);
  if (!p) {
    const [redirect] = query("SELECT new_id FROM person_redirect WHERE old_id = ?", [id]);
    if (redirect) {
      id = redirect.new_id;
      [p] = query("SELECT * FROM person WHERE id = ?", [id]);
    }
  }
  if (!p) { app.innerHTML = "<h1>Nicht gefunden</h1>"; return; }

  const dw = disciplineWhere("e.sport_type");
  const allRows = query(`
    SELECT r.*, e.id AS event_id, e.title AS event_title, e.location, e.country,
           e.competition_type, e.sport_type, s.date AS stage_date, s.title AS stage_title,
           s.number AS stage_number, e.date_from,
           cs.starters, cs.classified, cs.winner_time_s,
           (SELECT COUNT(*) FROM result r2
            WHERE r2.stage_id = r.stage_id AND r2.category NOT LIKE 'bahn%') AS non_bahn_count
    FROM person_result r
    JOIN stage s ON s.id = r.stage_id
    JOIN event e ON e.id = s.event_id
    LEFT JOIN category_stats cs ON cs.stage_id = r.stage_id AND cs.category = r.category
    WHERE r.person_id = ?${dw.sql}
    ORDER BY COALESCE(s.date, e.date_from) DESC`, [id, ...dw.params]);
  // link straight to the specific race's own results page (same "event ·
  // stage" single click-target as the Wettkämpfe list) - only when this
  // runner's own results reveal the event actually has more than one stage.
  const eventStageCounts = new Map();
  for (const r of allRows) {
    if (!eventStageCounts.has(r.event_id)) eventStageCounts.set(r.event_id, new Set());
    eventStageCounts.get(r.event_id).add(r.stage_id);
  }
  for (const r of allRows) {
    const multiStage = eventStageCounts.get(r.event_id).size > 1;
    r.stage_label = multiStage ? (r.stage_title || `Etappe ${r.stage_number}`) : "";
    r.href = multiStage ? `#/event/${r.event_id}/stage/${r.stage_number}` : `#/event/${r.event_id}`;
  }

  const years = [...new Set(allRows.map((r) => seasonYear(r.stage_date || r.date_from, r.sport_type)).filter(Boolean))]
    .sort((a, b) => b - a);
  const rows = year ? allRows.filter((r) => seasonYear(r.stage_date || r.date_from, r.sport_type) === year) : allRows;

  const countable = rows.filter((r) => !(isBahn(r.category) && r.non_bahn_count > 0));
  const finished = countable.filter((r) => r.status === "ok" && r.rank != null && !isKoHeat(r.category));
  const wins = finished.filter((r) => r.rank === 1).length;
  const podiums = finished.filter((r) => r.rank <= 3).length;
  const clubs = [...new Set(allRows.map((r) => r.club).filter(Boolean))].slice(0, 3);

  const chip = (val, label) => `<a class="chip ${(!year && !val) || year === val ? "active" : ""}"
      href="#/runner/${id}${val ? "/" + val : ""}">${label}</a>`;

  app.innerHTML = `
    <h1>${esc(p.name)}</h1>
    <p class="sub">${clubs.map(esc).join(" · ")}${p.year_of_birth ? ` · Jg. ${p.year_of_birth}` : ""}</p>
    <div class="stats">
      <div class="stat"><b>${countable.length}</b><span>Starts</span></div>
      <div class="stat"><b>${wins}</b><span>Siege</span></div>
      <div class="stat"><b>${podiums}</b><span>Podestplätze</span></div>
    </div>
    <h2>Ergebnisse</h2>
    <div class="chips">
      ${chip(null, "Alle")}
      ${years.map((y) => chip(y, y)).join("")}
    </div>
    <table>
      <thead><tr>
        <th>Datum</th><th>Wettkampf</th><th class="hide-sm">Ort</th><th>Kategorie</th>
        <th class="num">Platz</th><th class="num">Zeit</th><th class="num">Diff</th><th class="num">%</th>
        <th class="hide-sm">Bemerkung</th>
      </tr></thead>
      <tbody>${rows.map((r) => `
        <tr class="${isBahn(r.category) && r.non_bahn_count > 0 ? "bahn-row" : ""}">
          <td class="dim">${fmtDate(r.stage_date || r.date_from)}</td>
          <td><a href="${r.href}">${esc(r.event_title)}${r.stage_label ? ` <span class="dim">· ${esc(r.stage_label)}</span>` : ""}</a></td>
          <td class="hide-sm dim">${esc(r.location || "")}</td>
          <td>${esc(r.category_full || r.category)}${r.result_kind && r.result_kind !== "individual" ? ` <span class="badge">${{ relay: "Staffel", pair: "Paar", team: "Mannschaft" }[r.result_kind] || r.result_kind}</span>` : ""}</td>
          <td class="num">${rankCell(r)}</td>
          <td class="num">${fmtTime(r.result_kind === "relay" && r.team_time_s != null ? r.team_time_s : r.time_s)}</td>
          <td class="num dim">${r.time_behind_s ? "+" + fmtTime(r.time_behind_s) : ""}</td>
          <td class="num">${r.status === "ok" ? fmtPct(r.time_behind_s ?? 0, r.winner_time_s) : ""}</td>
          <td class="hide-sm dim note-cell">${r.note ? esc(r.note) : ""}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function teamUnitKey(result) {
  if (!['relay', 'team', 'pair'].includes(result.result_kind)) return null;
  if (result.team_number) return `${result.result_kind}:number:${result.team_number}`;
  if (result.result_kind === 'pair') {
    return `pair:${result.rank ?? ''}:${result.status}:${result.time_s ?? ''}:${result.club || ''}`;
  }
  return `${result.result_kind}:name:${result.team_name || result.club || result.note || result.id}`;
}

// Explicit team identity and leg number are persisted in schema v4. Notes
// remain a compatibility fallback for old databases, but no longer decide
// whether differently-classified members belong to the same team.
function reorderTeamMembers(results) {
  const legOf = (note) => {
    const m = /Leg (\d+)\//.exec(note || "");
    return m ? +m[1] : 999;
  };
  let i = 0;
  while (i < results.length) {
    let j = i + 1;
    const key = teamUnitKey(results[i]);
    while (key && j < results.length && teamUnitKey(results[j]) === key) j++;
    if (j - i > 1) {
      const group = results.slice(i, j);
      if (group[0].result_kind === "relay") group.sort((a, b) =>
        (a.leg_number ?? legOf(a.note)) - (b.leg_number ?? legOf(b.note)));
      else if (group[0].result_kind === "team") group.sort((a, b) => a.person_name.localeCompare(b.person_name, "de-AT"));
      for (let k = i; k < j; k++) results[k] = group[k - i];
    }
    i = j;
  }
  return results;
}

function viewEvent(id, medalsOnly, stageNum, regionalCode = null) {
  const [e] = query("SELECT * FROM event WHERE id = ?", [id]);
  if (!e) { app.innerHTML = "<h1>Nicht gefunden</h1>"; return; }

  const allStages = query(
    `SELECT s.* FROM stage s WHERE s.event_id = ?
     AND EXISTS (SELECT 1 FROM result r WHERE r.stage_id = s.id)
     ORDER BY s.number`, [id]);
  const multiStage = allStages.length > 1;
  // a specific stage (=one race with its own results) can be viewed on its
  // own, keeping the results page clean per race - the default from the
  // Wettkämpfe list. Falls back to the whole event if the number is stale.
  const stages = stageNum != null && allStages.some((s) => s.number === stageNum)
    ? allStages.filter((s) => s.number === stageNum) : allStages;
  const onStage = stages.length === 1 && multiStage ? stages[0] : null;
  const stageParam = onStage ? `/stage/${onStage.number}` : "";
  const regionalViews = query(
    `SELECT DISTINCT ci.jurisdiction, cj.short_name
       FROM championship_instance ci
       JOIN championship_jurisdiction cj ON cj.code = ci.jurisdiction
       JOIN stage s ON s.id = ci.stage_id
      WHERE s.event_id = ? AND ci.championship_type = 'LMS'
        AND ci.state = 'confirmed'
        AND EXISTS (SELECT 1 FROM championship_entry ce
                     WHERE ce.championship_instance_id = ci.id)
        ${onStage ? "AND s.number = ?" : ""}
      ORDER BY cj.name`, onStage ? [id, onStage.number] : [id]);
  if (regionalCode && !regionalViews.some((view) => view.jurisdiction === regionalCode)) {
    regionalCode = null;
  }

  // Event-level ÖM/ÖSTM badge, with a "Jugend"/"Senioren" qualifier when
  // every championship-tagged category at this event falls cleanly on
  // one side of that split (see categoryAgeGroup) - left blank rather
  // than guessing when the event mixes both (or neither type extracts a
  // group at all), e.g. event 4315's combined youth+senior "ÖM Nacht".
  // "Senioren" is an ÖM-only qualifier: ÖSTM is by definition the Elite
  // title, not a masters-age one, so its "ab 21 Elite"-shaped categories
  // must never be labeled "Senioren" even though categoryAgeGroup's
  // generic age>=21 test alone can't tell "senior" and "Elite" apart.
  const champCats = query(
    `SELECT DISTINCT r.championship, r.category FROM result r
     JOIN stage s ON s.id = r.stage_id WHERE s.event_id = ? AND r.championship IS NOT NULL
       ${onStage ? "AND s.number = ?" : ""}`,
    onStage ? [id, onStage.number] : [id]);
  const hasChamp = champCats.length > 0;
  const toggleHref = `#/event/${id}${stageParam}${medalsOnly ? "" : "/om"}`;
  const champBadges = [...new Set(champCats.map((c) => c.championship))].map((champ) => {
    const groups = new Set(champCats.filter((c) => c.championship === champ)
      .map((c) => categoryAgeGroup(c.category)));
    const qualifier = groups.size === 1 && groups.has("youth") ? " Jugend"
      : champ === "ÖM" && groups.size === 1 && groups.has("senior") ? " Senioren" : "";
    return `<a href="${toggleHref}" class="badge champ-badge champ-toggle${medalsOnly ? " active" : ""}"
      title="${medalsOnly ? "Alle Ergebnisse anzeigen" : "Nur ÖM/ÖSTM-Medaillen anzeigen"}">
      ${medalsOnly ? "✓ " : ""}${esc(champ)}${qualifier}</a>`;
  }).join(" ");

  // when a single race of a multi-race meet is shown, its own name (the
  // ANNE stage title, or "Etappe N" when it has none) headlines the page and
  // a link back to the whole meet stays one tap away
  const stageLabel = onStage ? (onStage.title || `Etappe ${onStage.number}`) : "";
  const stageDate = onStage && onStage.date ? onStage.date : e.date_from;

  let html = `
    <h1>${esc(e.title)}</h1>
    <p class="sub">${fmtDate(stageDate)} · ${esc(e.location || "")}
      ${e.url ? `· <a href="${esc(e.url)}" target="_blank" rel="noopener">ANNE ↗</a>` : ""}</p>
    ${stageLabel ? `<p class="sub"><b>${esc(stageLabel)}</b> · <a href="#/event/${id}">alle Etappen (${allStages.length})</a></p>` : ""}
    ${champBadges ? `<p class="sub">${champBadges}</p>` : ""}
    ${regionalViews.length ? `<div class="chips championship-views">
      <a class="chip ${!regionalCode ? "active" : ""}" href="#/event/${id}${stageParam}">Gesamtergebnis</a>
      ${regionalViews.map((view) => `<a class="chip ${regionalCode === view.jurisdiction ? "active" : ""}"
        href="#/event/${id}${stageParam}/lm/${view.jurisdiction}">${esc(view.short_name)}</a>`).join("")}
    </div>` : ""}
    ${regionalCode ? `<p class="sub dim">Getrennte Landesmeisterschaftswertung desselben Laufs; die Gesamtleistung wird nicht dupliziert.</p>` : ""}
    ${medalsOnly && hasChamp ? `<p class="sub dim">Kompaktübersicht: nur ÖM/ÖSTM-Altersklassen, nur Medaillenränge.</p>` : ""}`;

  for (const st of stages) {
    const cats = regionalCode ? query(`
      SELECT ci.id AS regional_instance_id, ci.category, ci.category AS category_full,
             COUNT(DISTINCT ce.id) AS entries, NULL AS starters, NULL AS classified,
             NULL AS winner_time_s, NULL AS len, NULL AS climb, NULL AS ctrls
        FROM championship_instance ci
        JOIN championship_entry ce ON ce.championship_instance_id = ci.id
       WHERE ci.stage_id = ? AND ci.jurisdiction = ? AND ci.championship_type = 'LMS'
         AND ci.state = 'confirmed'
       GROUP BY ci.id, ci.category ORDER BY ci.category`, [st.id, regionalCode]) : query(`
      SELECT r.category, MAX(r.category_full) AS category_full,
             cs.starters, cs.classified, cs.winner_time_s,
             COUNT(DISTINCT CASE WHEN r.result_kind = 'pair'
                   THEN 'p:' || COALESCE(r.rank, '') || ':' || r.status || ':' ||
                        COALESCE(r.time_s, '') || ':' || COALESCE(r.club, '')
                   WHEN r.result_kind IN ('relay', 'team')
                   THEN COALESCE('n:' || r.team_number, 't:' || r.team_name,
                                 'c:' || r.club, 'r:' || r.id)
                   ELSE 'r:' || r.id END) AS entries,
             MAX(r.course_length_m) AS len, MAX(r.course_climb_m) AS climb,
             MAX(r.course_controls) AS ctrls
      FROM result r LEFT JOIN category_stats cs
        ON cs.stage_id = r.stage_id AND cs.category = r.category
      WHERE r.stage_id = ?${medalsOnly ? " AND r.championship IS NOT NULL" : ""}
      GROUP BY r.category ORDER BY r.category`, [st.id]);
    // in the compact medals-only view, a stage with no championship category
    // at all (a plain Austria-Cup leg of an otherwise-championship meet, e.g.
    // "6.AC Mittel" within "ÖM 3Tage-4Läufe") would otherwise render as a bare
    // heading with nothing under it - skip it entirely.
    if (medalsOnly && !cats.length) continue;
    if (stages.length > 1) {
      // each stage heading links to that race on its own for a clean, single-
      // race results page
      html += `<h2><a href="#/event/${id}/stage/${st.number}${medalsOnly ? "/om" : ""}">${esc(st.title || "Etappe " + st.number)}</a></h2>`;
    }
    const stageHasOfficial = cats.some((c) => !isBahn(c.category));
    for (const c of cats) {
      const results = reorderTeamMembers(regionalCode ? query(`
        SELECT r.*, COALESCE(p.name, r.observed_name) AS person_name,
               ce.regional_rank
          FROM championship_entry ce
          JOIN championship_entry_result cer ON cer.championship_entry_id = ce.id
          JOIN result r ON r.id = cer.result_id
          LEFT JOIN person p ON p.id = r.person_id
         WHERE ce.championship_instance_id = ?
         ORDER BY ce.regional_rank IS NULL, ce.regional_rank,
                  COALESCE(r.team_number, r.team_name, r.club), r.leg_number, r.time_s`,
        [c.regional_instance_id]) : query(`
        SELECT r.*, COALESCE(p.name, r.observed_name) AS person_name FROM result r
        LEFT JOIN person p ON p.id = r.person_id
        WHERE r.stage_id = ? AND r.category = ?
          ${medalsOnly ? "AND r.championship IS NOT NULL AND r.national_rank <= 3" : ""}
        ORDER BY CASE WHEN r.rank IS NULL THEN 1 ELSE 0 END, r.rank,
                 COALESCE(r.team_number, r.team_name, r.club), r.leg_number, r.time_s`,
        [st.id, c.category]));
      if (medalsOnly && !results.length) continue;
      const course = [
        c.len ? (c.len / 1000).toFixed(1).replace(".", ",") + " km" : null,
        c.climb ? c.climb + " Hm" : null,
        c.ctrls ? c.ctrls + " Posten" : null,
      ].filter(Boolean).join(" · ");
      const regionalLabel = regionalCode
        ? regionalViews.find((view) => view.jurisdiction === regionalCode)?.short_name : null;
      const catChamp = regionalLabel ? [regionalLabel]
        : [...new Set(results.map((r) => r.championship).filter(Boolean))];
      const medalTier = { 1: "gold", 2: "silver", 3: "bronze" };
      const medalName = { 1: "Gold", 2: "Silber", 3: "Bronze" };
      const units = [];
      const unitIndex = new Map();
      for (const result of results) {
        const key = teamUnitKey(result);
        if (!key) {
          units.push({ team: false, rows: [result] });
        } else if (!unitIndex.has(key)) {
          unitIndex.set(key, units.length);
          units.push({ team: true, rows: [result] });
        } else {
          units[unitIndex.get(key)].rows.push(result);
        }
      }
      // category_stats is person-based.  For Staffel/Mannschaft that would
      // advertise the number of classified members (27 in a 13-team
      // Mannschaft) instead of the competitor units actually shown below.
      const hasTeamUnits = units.some((unit) => unit.team);
      const displayedEntries = hasTeamUnits ? units.length : (c.starters ?? c.entries);
      const placementCell = (r, tier) => regionalCode
        ? rankCell({ ...r, rank: r.regional_rank, starters: null })
        : tier
        ? `<span class="rank-medal rank-medal-${tier}" title="${medalName[r.national_rank]} (ÖM/ÖSTM)">${r.rank ?? ""}</span>`
        : rankCell({ ...r, starters: null });
      const individualTime = (r) => r.individual_status && r.individual_status !== "ok"
        ? `<span class="status member-status">${esc(r.individual_status)}</span>`
        : fmtTime(r.time_s);
      html += `
        <div class="cat-block">
          <div class="cat-head">
            <h3>${esc(c.category_full || c.category)}${catChamp.map((ch) => ` <span class="badge champ-badge">${esc(ch)}</span>`).join("")}</h3>
            <span class="course">${course}${course ? " · " : ""}${displayedEntries} ${hasTeamUnits ? "Teams" : "Starter"}${isBahn(c.category) && stageHasOfficial ? " · inoffizielle Bahnwertung" : ""}</span>
          </div>
          <table>
            <thead><tr><th class="num">Pl</th><th>Name</th><th class="hide-sm">Verein</th>
              <th class="num">Zeit</th><th class="num">Diff</th></tr></thead>
            <tbody>${units.map((unit) => {
              if (!unit.team) {
                const r = unit.rows[0], tier = regionalCode ? null : medalTier[r.national_rank];
                return `<tr class="${isBahn(c.category) && stageHasOfficial ? "bahn-row" : ""} ${tier ? `medal-row-${tier}` : ""}">
                  <td class="num">${placementCell(r, tier)}</td>
                  <td>${r.person_id == null
                    ? `<span class="family-name">${esc(r.person_name || "Family")}</span>`
                    : `<a href="#/runner/${r.person_id}">${esc(r.person_name)}</a>`
                  }${r.result_kind === "family" ? ` <span class="badge">Family</span>` : ""}${r.note ? `<div class="note">${esc(r.note)}</div>` : ""}</td>
                  <td class="hide-sm dim">${esc(r.club || "")}</td>
                  <td class="num">${fmtTime(r.time_s)}</td>
                  <td class="num dim">${r.status === "ok" && r.time_behind_s ? "+" + fmtTime(r.time_behind_s) : ""}</td>
                </tr>`;
              }
              const team = unit.rows[0], tier = regionalCode ? null : medalTier[team.national_rank];
              const teamLabel = team.result_kind === "pair"
                ? unit.rows.map((r) => r.person_name).join(" + ")
                : `${team.team_number ? `#${team.team_number} ` : ""}${team.team_name || team.club || "Team"}`;
              const teamTime = team.team_time_s != null ? fmtTime(team.team_time_s)
                : team.team_status && team.team_status !== "ok" ? `<span class="status">${esc(team.team_status)}</span>` : "";
              return `<tr class="team-summary ${tier ? `medal-row-${tier}` : ""}">
                  <td class="num">${placementCell(team, tier)}</td>
                  <td><strong>${team.result_kind === "pair" ? unit.rows.map((r) => `<a href="#/runner/${r.person_id}">${esc(r.person_name)}</a>`).join(" + ") : esc(teamLabel)}</strong> <span class="badge">${team.result_kind === "relay" ? "Staffel" : team.result_kind === "pair" ? "Paar" : "Mannschaft"}</span></td>
                  <td class="hide-sm dim">${esc(team.official_club || team.club || "")}</td>
                  <td class="num">${teamTime}</td>
                  <td class="num dim">${team.status === "ok" && team.time_behind_s ? "+" + fmtTime(team.time_behind_s) : ""}</td>
                </tr>${team.result_kind === "pair" ? "" : unit.rows.filter((r) => r.person_id != null).map((r) => `<tr class="team-member">
                  <td class="num dim"></td>
                  <td><a href="#/runner/${r.person_id}">${esc(r.person_name)}</a>${r.result_kind === "relay" ? `<span class="leg-label">Leg ${r.leg_number || "?"}/${r.leg_count || unit.rows.length}</span>` : ""}</td>
                  <td class="hide-sm dim"></td><td class="num">${r.result_kind === "relay" ? individualTime(r) : ""}</td><td></td>
                </tr>`).join("")}`;
            }).join("")}
            </tbody>
          </table>
        </div>`;
    }
  }
  app.innerHTML = html;
}

/* ---------- search ---------- */

function setupSearch() {
  const input = document.getElementById("search");
  const dropdown = document.getElementById("search-results");
  let timer = null;

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const q = input.value.trim();
      if (q.length < 2) { dropdown.hidden = true; return; }
      // runners only: event names collide too often (many "WOLV Cup" etc.
      // across years) to be a useful match target here — use the Wettkämpfe
      // list/year filter to find a specific event instead
      const persons = query(
        `SELECT p.id, p.name, p.year_of_birth,
                (SELECT COUNT(*) FROM person_result r WHERE r.person_id = p.id) AS n
         FROM person p WHERE p.name LIKE ? ORDER BY n DESC LIMIT 10`, [`%${q}%`]);
      dropdown.innerHTML = persons.length
        ? persons.map((p) => `<a href="#/runner/${p.id}">${esc(p.name)}
            <span class="meta">${p.year_of_birth ? "Jg. " + p.year_of_birth + " · " : ""}${p.n} Starts</span></a>`).join("")
        : `<div class="group">Keine Treffer</div>`;
      dropdown.hidden = false;
    }, 150);
  });

  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".search")) dropdown.hidden = true;
  });
  dropdown.addEventListener("click", () => { dropdown.hidden = true; input.value = ""; });
}

/* ---------- all events (Wettkämpfe) ---------- */

// A category name's gender lives in wildly different shapes across 20+
// years of source data ("Herren ab 45", "H45", "D 15-17", "Damen21E",
// "M40 (CZE)") - matched against the full German/English words first (most
// reliable), then a bare leading D/H/M/W letter directly against a digit or
// hyphen (so it doesn't fire on an unrelated word starting with the same
// letter). "base" is what's left after stripping the gender token - used
// to pair the Herren/Damen side of the same age bracket onto one line.
const CHAMPION_GENDER_WORD_RE = [
  [/\bdamen\b/i, "D"], [/\bfrauen\b/i, "D"], [/\bwomen\b/i, "D"],
  [/\bherren\b/i, "H"], [/\bmänner\b/i, "H"], [/\bmen\b/i, "H"],
  [/\bmixed\b/i, "X"],
];
function parseCategoryGenderBase(cat) {
  const c = (cat || "").trim();
  for (const [re, g] of CHAMPION_GENDER_WORD_RE) {
    if (re.test(c)) return { gender: g, base: c.replace(re, "").trim().replace(/^[\s,./-]+|[\s,./-]+$/g, "") };
  }
  const m = c.match(/^([DHMW])\s?(?=[\d-])/);
  if (m) return { gender: m[1] === "D" || m[1] === "W" ? "D" : "H", base: c.slice(m[0].length).trim() };
  return { gender: null, base: c };
}
const categoryAgeSort = (base) => { const m = base.match(/\d{1,3}/); return m ? +m[0] : 999; };

// One line per age bracket, Herren/Damen of the same bracket combined -
// ÖSTM (always Elite) first, then ÖM brackets youngest to oldest.
function renderChampions(byCat) {
  const groups = new Map();
  for (const [cat, info] of byCat) {
    const { gender, base } = parseCategoryGenderBase(cat);
    const key = `${info.championship}||${base.toLowerCase()}`;
    if (!groups.has(key)) {
      groups.set(key, { championship: info.championship, base, sortAge: categoryAgeSort(base), D: null, H: null, other: [] });
    }
    const g = groups.get(key);
    const label = info.names.map((n) => `${esc(n.name)}${n.club ? ` <span class="dim">(${esc(n.club)})</span>` : ""}`).join(" / ");
    if (gender === "D" && !g.D) g.D = label;
    else if (gender === "H" && !g.H) g.H = label;
    else g.other.push(label);
  }
  const list = [...groups.values()].sort((a, b) =>
    (a.championship === "ÖSTM" ? 0 : 1) - (b.championship === "ÖSTM" ? 0 : 1) || a.sortAge - b.sortAge);
  return `<ul class="medal-events">${list.map((g) => `
    <li><span class="badge champ-badge">${g.championship}</span> <b>${esc(g.base)}</b>:
      ${[g.H && `H: ${g.H}`, g.D && `D: ${g.D}`, ...g.other].filter(Boolean).join(" · ")}</li>`).join("")}
  </ul>`;
}

function viewEvents(year, omOnly, meister) {
  // One row per STAGE, not per event: a multi-day event (e.g. a 3-day
  // festival with a separate Sprint/Middle/Long each day) is really 3
  // distinct competitions, each with its own date and its own results -
  // collapsing them into a single event-level row hid that (and, until a
  // build_db.py fix, an event like that could even silently lose stages
  // 2 and 3 to a stage-splitting bug entirely - see correct_legacy_stage_dates).
  const dw = disciplineWhere("e.sport_type");
  const stageRows = query(`
    SELECT e.id AS event_id, e.title, e.location, s.id AS stage_id, s.number, s.title AS stage_title,
           COALESCE(s.date, e.date_from) AS date, COUNT(r.id) AS n,
           MAX(r.championship IS NOT NULL) AS has_champ
    FROM event e JOIN stage s ON s.event_id = e.id JOIN result r ON r.stage_id = s.id
    WHERE 1=1${dw.sql}
    GROUP BY s.id ORDER BY date DESC, e.id, s.number`, dw.params);
  const stagesPerEvent = new Map();
  for (const r of stageRows) stagesPerEvent.set(r.event_id, (stagesPerEvent.get(r.event_id) || 0) + 1);
  const yearCounts = new Map();
  for (const r of stageRows) {
    const yr = (r.date || "").slice(0, 4);
    yearCounts.set(yr, (yearCounts.get(yr) || 0) + 1);
  }
  const years = [...yearCounts.entries()].sort((a, b) => b[0].localeCompare(a[0]));

  let shown = year ? stageRows.filter((r) => (r.date || "").startsWith(year)) : stageRows;
  const omCount = shown.filter((r) => r.has_champ).length;
  if (omOnly) shown = shown.filter((r) => r.has_champ);

  // Only the winner (national_rank = 1) of each ÖM/ÖSTM category - fetched
  // once for every stage regardless of the "Meister" toggle's current
  // state, since it's cheap next to stageRows and the toggle can be
  // flipped without a full requery.
  const champsByStage = new Map();
  for (const r of query(`
      SELECT r.stage_id, r.category, r.championship, p.name AS person_name, r.club
      FROM person_result r JOIN person p ON p.id = r.person_id
      WHERE r.championship IS NOT NULL AND r.national_rank = 1 AND r.status = 'ok'
      ORDER BY r.stage_id, r.category`)) {
    if (!champsByStage.has(r.stage_id)) champsByStage.set(r.stage_id, new Map());
    const byCat = champsByStage.get(r.stage_id);
    if (!byCat.has(r.category)) byCat.set(r.category, { championship: r.championship, names: [] });
    byCat.get(r.category).names.push({ name: r.person_name, club: r.club });
  }

  const yearHref = (val) => `#/events${val ? "/" + val : ""}${omOnly ? "/om" : ""}${meister ? "/meister" : ""}`;
  const chip = (val, label, n) =>
    `<a class="chip ${(!year && !val) || year === val ? "active" : ""}" href="${yearHref(val)}">
       ${label}${n != null ? ` <span>${n}</span>` : ""}</a>`;
  const toggleHref = (om2, meister2) =>
    `#/events${year ? "/" + year : ""}${om2 ? "/om" : ""}${meister2 ? "/meister" : ""}`;

  app.innerHTML = `
    <h1>Wettkämpfe</h1>
    <p class="sub">${stageRows.length.toLocaleString("de-AT")} Wettkämpfe mit Ergebnissen${year ? ` · ${shown.length} in ${year}` : ""}.</p>
    <div class="chips">
      ${chip(null, "Alle", stageRows.length)}
      ${years.map(([yr, n]) => chip(yr, yr, n)).join("")}
    </div>
    <div class="chips">
      <a class="badge champ-badge champ-toggle ${omOnly ? "active" : ""}" href="${toggleHref(!omOnly, meister)}">
        ${omOnly ? "✓ " : ""}ÖM/ÖSTM${!omOnly ? ` (${omCount})` : ""}</a>
      <a class="badge champ-badge champ-toggle ${meister ? "active" : ""}" href="${toggleHref(omOnly, !meister)}">
        ${meister ? "✓ " : ""}Meister</a>
    </div>
    <table>
      <thead><tr><th>Datum</th><th>Wettkampf</th><th class="hide-sm">Ort</th><th class="num">Ergebnisse</th></tr></thead>
      <tbody>${shown.map((r) => {
        // each row is one race; link straight to that race's own clean results
        // page. The stage name is only appended when the meet has more than one
        // race (otherwise the event title alone already names it).
        const multi = stagesPerEvent.get(r.event_id) > 1;
        const stageLabel = multi ? (r.stage_title || `Etappe ${r.number}`) : "";
        const href = multi ? `#/event/${r.event_id}/stage/${r.number}` : `#/event/${r.event_id}`;
        const champs = meister ? champsByStage.get(r.stage_id) : null;
        return `
        <tr>
          <td class="dim">${fmtDate(r.date)}</td>
          <td><a href="${href}">${esc(r.title)}${stageLabel ? ` <span class="dim">· ${esc(stageLabel)}</span>` : ""}</a></td>
          <td class="hide-sm dim">${esc(r.location || "")}</td>
          <td class="num">${r.n}</td>
        </tr>${champs ? `<tr class="detail-row"><td></td><td colspan="3">${renderChampions(champs)}</td></tr>` : ""}`;
      }).join("")}
      </tbody>
    </table>`;
}

/* ---------- all runners (Läufer:innen) ---------- */

let runnersCache = null;

function firstLetter(name) {
  const c = (name.trim()[0] || "").toUpperCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "");  // fold diacritics: Š→S, Á→A
  return /[A-Z]/.test(c) ? c : "#";
}

function viewRunners(letter) {
  if (!runnersCache) {
    const dw = disciplineWhere("e.sport_type");
    runnersCache = query(`
      SELECT p.id, p.name, p.year_of_birth, COUNT(r.id) AS n
      FROM person p JOIN person_result r ON r.person_id = p.id
      JOIN stage s ON s.id = r.stage_id JOIN event e ON e.id = s.event_id
      WHERE r.result_kind != 'team'${dw.sql}   -- team rosters aren't individual runners
      GROUP BY p.id ORDER BY p.name COLLATE NOCASE`, dw.params);
    for (const r of runnersCache) r.letter = firstLetter(r.name);
  }
  const letters = [...new Set(runnersCache.map((r) => r.letter))]
    .sort((a, b) => (a === "#") - (b === "#") || a.localeCompare(b));  // "#" last
  const active = letter && letters.includes(letter) ? letter : letters[0];
  const list = runnersCache.filter((r) => r.letter === active);

  const rowsHtml = (rows) => rows.map((r) => `
    <tr>
      <td><a href="#/runner/${r.id}">${esc(r.name)}</a></td>
      <td class="num dim">${r.year_of_birth || ""}</td>
      <td class="num">${r.n}</td>
    </tr>`).join("");

  app.innerHTML = `
    <h1>Läufer:innen</h1>
    <p class="sub">${runnersCache.length.toLocaleString("de-AT")} Läufer:innen. Nach Name suchen oder Anfangsbuchstaben wählen.</p>
    <input id="runner-filter" class="filter" type="search" placeholder="Name filtern …" autocomplete="off">
    <div class="chips letters">
      ${letters.map((l) => `<a class="chip ${l === active ? "active" : ""}" href="#/runners/${l}">${l}</a>`).join("")}
    </div>
    <table>
      <thead><tr><th>Name</th><th class="num">Jg</th><th class="num">Starts</th></tr></thead>
      <tbody id="runner-rows">${rowsHtml(list)}</tbody>
    </table>`;

  const input = document.getElementById("runner-filter");
  const tbody = document.getElementById("runner-rows");
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { tbody.innerHTML = rowsHtml(list); return; }
    const matches = runnersCache.filter((r) => r.name.toLowerCase().includes(q)).slice(0, 300);
    tbody.innerHTML = matches.length ? rowsHtml(matches)
      : `<tr><td colspan="3" class="dim">Keine Treffer</td></tr>`;
  });
}

/* ---------- all clubs (Vereine) ---------- */

let clubsCache = null;

function viewClubs() {
  if (!clubsCache) {
    const dw = disciplineWhere("e.sport_type");
    clubsCache = query(`
      SELECT r.official_club AS name, COUNT(*) AS n, COUNT(DISTINCT r.person_id) AS runners
      FROM person_result r JOIN stage s ON s.id = r.stage_id JOIN event e ON e.id = s.event_id
      WHERE r.official_club IS NOT NULL${dw.sql}
      GROUP BY r.official_club ORDER BY r.official_club COLLATE NOCASE`, dw.params);
  }

  const rowsHtml = (rows) => rows.map((c) => `
    <tr>
      <td><a href="#/club/${encodeURIComponent(c.name)}">${esc(c.name)}</a></td>
      <td class="num dim">${c.runners}</td>
      <td class="num">${c.n}</td>
    </tr>`).join("");

  app.innerHTML = `
    <h1>Vereine</h1>
    <p class="sub">${clubsCache.length.toLocaleString("de-AT")} offizielle Vereine (laut ANNE).</p>
    <input id="club-filter" class="filter" type="search" placeholder="Verein filtern …" autocomplete="off">
    <table>
      <thead><tr><th>Verein</th><th class="num">Läufer:innen</th><th class="num">Ergebnisse</th></tr></thead>
      <tbody id="club-rows">${rowsHtml(clubsCache)}</tbody>
    </table>`;

  const input = document.getElementById("club-filter");
  const tbody = document.getElementById("club-rows");
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    const matches = q ? clubsCache.filter((c) => c.name.toLowerCase().includes(q)) : clubsCache;
    tbody.innerHTML = matches.length ? rowsHtml(matches)
      : `<tr><td colspan="3" class="dim">Keine Treffer</td></tr>`;
  });
}

function viewClub(name, year, medalType) {
  const info = query(`
    SELECT COUNT(*) AS n, COUNT(DISTINCT person_id) AS runners
    FROM person_result WHERE official_club = ?`, [name])[0];
  if (!info || !info.n) { app.innerHTML = "<h1>Nicht gefunden</h1>"; return; }

  // national_rank is placement among only championship-eligible (Austrian)
  // finishers - it can differ from the overall race `rank` when a foreign/
  // ineligible competitor placed ahead, so the ÖM/ÖSTM view needs rows that
  // wouldn't otherwise make the top-3 by raw rank alone.
  const dw = disciplineWhere("e.sport_type");
  const allPodiums = query(`
    SELECT r.rank, r.national_rank, r.category, r.category_full, r.result_kind, r.championship,
           e.id AS event_id, e.title AS event_title, e.sport_type, s.title AS stage_title,
           s.id AS stage_id, s.number AS stage_number,
           COALESCE(s.date, e.date_from) AS date, p.id AS person_id, p.name AS person_name
    FROM person_result r
    JOIN stage s ON s.id = r.stage_id
    JOIN event e ON e.id = s.event_id
    JOIN person p ON p.id = r.person_id
    WHERE r.official_club = ? AND r.status = 'ok'
      AND (r.rank <= 3 OR (r.championship IS NOT NULL AND r.national_rank <= 3))
      AND NOT (r.category LIKE 'bahn%' AND EXISTS (
        SELECT 1 FROM result r2
        WHERE r2.stage_id = r.stage_id AND r2.category NOT LIKE 'bahn%'))${dw.sql}
    ORDER BY date DESC`, [name, ...dw.params]);

  const isOm = medalType === "om";
  const years = [...new Set(allPodiums.map((r) => seasonYear(r.date, r.sport_type)))].sort((a, b) => b - a);
  const typeFiltered = isOm
    ? allPodiums.filter((r) => r.championship && r.national_rank <= 3)
    : allPodiums.filter((r) => r.rank <= 3 && !isKoHeat(r.category));
  const podiums = year ? typeFiltered.filter((r) => seasonYear(r.date, r.sport_type) === year) : typeFiltered;
  const medalRank = (r) => (isOm ? r.national_rank : r.rank);
  const gold = podiums.filter((r) => medalRank(r) === 1).length;
  const silver = podiums.filter((r) => medalRank(r) === 2).length;
  const bronze = podiums.filter((r) => medalRank(r) === 3).length;

  const yearChip = (val, label) => `<a class="chip ${(!year && !val) || year === val ? "active" : ""}"
      href="#/club/${encodeURIComponent(name)}${val ? "/" + val : ""}${isOm ? "/om" : ""}">${label}</a>`;
  const typeChip = (val, label) => `<a class="chip ${isOm === val ? "active" : ""}"
      href="#/club/${encodeURIComponent(name)}${year ? "/" + year : ""}${val ? "/om" : ""}">${label}</a>`;

  let tableHtml;
  if (isOm) {
    // one row per runner: Gold/Silber/Bronze/Summe medal counts, sorted like
    // a championship table (gold, then silver, then bronze), with a shared
    // rank number for ties - plus the ÖSTM-only subset of those same medals
    // broken out in "G-S-B" form, since ÖSTM is the more prestigious title
    // within the combined ÖM/ÖSTM count rather than a separate total
    const byPerson = new Map();
    for (const r of podiums) {
      if (!byPerson.has(r.person_id)) {
        byPerson.set(r.person_id, {
          person_id: r.person_id, person_name: r.person_name,
          gold: 0, silver: 0, bronze: 0, ostmGold: 0, ostmSilver: 0, ostmBronze: 0,
          entries: [],
        });
      }
      const p = byPerson.get(r.person_id);
      const mr = r.national_rank;
      if (mr === 1) p.gold++; else if (mr === 2) p.silver++; else if (mr === 3) p.bronze++;
      if (r.championship === "ÖSTM") {
        if (mr === 1) p.ostmGold++; else if (mr === 2) p.ostmSilver++; else if (mr === 3) p.ostmBronze++;
      }
      p.entries.push(r);
    }
    // ties on the overall Gold/Silber/Bronze count break by ÖSTM medal count
    // next (ÖSTM being the more prestigious title within the combined
    // count), same gold-then-silver-then-bronze precedence, before finally
    // falling back to name.
    const people = [...byPerson.values()].sort((a, b) =>
      b.gold - a.gold || b.silver - a.silver || b.bronze - a.bronze
      || b.ostmGold - a.ostmGold || b.ostmSilver - a.ostmSilver || b.ostmBronze - a.ostmBronze
      || a.person_name.localeCompare(b.person_name, "de-AT"));
    let place = 0, prevKey = null;
    people.forEach((p, i) => {
      const key = `${p.gold}-${p.silver}-${p.bronze}-${p.ostmGold}-${p.ostmSilver}-${p.ostmBronze}`;
      if (key !== prevKey) { place = i + 1; prevKey = key; }
      p.place = place;
      p.entries.sort((a, b) => a.national_rank - b.national_rank || b.date.localeCompare(a.date));
      // A legacy multi-day event (e.g. "OL Südbgld.", 3 stages) never gets a
      // real per-stage s.title of its own (only ANNE's own /stages API sets
      // that) - without SOME per-entry label, 3 medals at the same event
      // render as three identical "OL Südbgld. · Herren ab 50" lines with
      // only the date telling them apart. Falls back to "Etappe N" (the
      // same fallback the Wettkämpfe view uses) whenever this person's own
      // entries reveal the event has more than one distinct stage.
      const eventStages = new Map();
      for (const e of p.entries) {
        if (!eventStages.has(e.event_id)) eventStages.set(e.event_id, new Set());
        eventStages.get(e.event_id).add(e.stage_id);
      }
      for (const e of p.entries) {
        const multiStage = eventStages.get(e.event_id).size > 1;
        e.stage_label = e.stage_title || (multiStage ? `Etappe ${e.stage_number}` : "");
        // link straight to the specific race's own results page, same as
        // the Wettkämpfe list - "event · stage" is one click target there too
        e.href = multiStage ? `#/event/${e.event_id}/stage/${e.stage_number}` : `#/event/${e.event_id}`;
      }
    });
    const medalLabel = { 1: "Gold", 2: "Silber", 3: "Bronze" };

    tableHtml = `
    <table>
      <thead><tr>
        <th class="num"></th><th>Läufer:in</th><th class="num">Gold</th><th class="num">Silber</th>
        <th class="num">Bronze</th><th class="num">Summe</th><th class="num">ÖSTM</th>
      </tr></thead>
      <tbody>${people.length ? people.map((p) => `
        <tr class="expandable" data-toggle="${p.person_id}">
          <td class="num dim">${p.place}.</td>
          <td><a href="#/runner/${p.person_id}">${esc(p.person_name)}</a> <span class="expand-icon">▸</span></td>
          <td class="num">${p.gold || ""}</td>
          <td class="num">${p.silver || ""}</td>
          <td class="num">${p.bronze || ""}</td>
          <td class="num"><b>${p.gold + p.silver + p.bronze}</b></td>
          <td class="num nowrap">${p.ostmGold || p.ostmSilver || p.ostmBronze ? `${p.ostmGold}-${p.ostmSilver}-${p.ostmBronze}` : ""}</td>
        </tr>
        <tr class="detail-row" data-detail="${p.person_id}" hidden>
          <td colspan="7">
            <ul class="medal-events">${p.entries.map((e) => `
              <li><b>${medalLabel[e.national_rank]}</b>${e.championship ? ` <span class="badge">${e.championship}</span>` : ""} ·
                <a href="${e.href}">${esc(e.event_title)}${e.stage_label && e.stage_label !== e.event_title ? ` · <b>${esc(e.stage_label)}</b>` : ""}</a> ·
                <span class="dim">${esc(e.category_full || e.category)} · ${fmtDate(e.date)}</span></li>`).join("")}
            </ul>
          </td>
        </tr>`).join("") : `<tr><td colspan="7" class="dim">Keine Podestplätze</td></tr>`}
      </tbody>
    </table>`;
  } else {
    tableHtml = `
    <table>
      <thead><tr>
        <th>Datum</th><th>Wettkampf</th><th>Kategorie</th><th class="num">Platz</th><th class="hide-sm">Läufer:in</th>
      </tr></thead>
      <tbody>${podiums.length ? podiums.map((r) => `
        <tr>
          <td class="dim">${fmtDate(r.date)}</td>
          <td><a href="#/event/${r.event_id}">${esc(r.event_title)}</a></td>
          <td>${esc(r.category_full || r.category)}${r.championship ? ` <span class="badge">${r.championship}</span>` : ""}${r.result_kind && r.result_kind !== "individual" ? ` <span class="badge">${{ relay: "Staffel", pair: "Paar", team: "Mannschaft" }[r.result_kind] || r.result_kind}</span>` : ""}</td>
          <td class="num"><span class="rank ${r.rank === 1 ? "rank-1" : ""}">${r.rank}</span></td>
          <td class="hide-sm"><a href="#/runner/${r.person_id}">${esc(r.person_name)}</a></td>
        </tr>`).join("") : `<tr><td colspan="5" class="dim">Keine Podestplätze</td></tr>`}
      </tbody>
    </table>`;
  }

  app.innerHTML = `
    <div class="cat-head">
      <h1>${esc(name)}</h1>
      <a class="chip" href="#/club/${encodeURIComponent(name)}/dns">Nicht angetreten</a>
    </div>
    <p class="sub">${info.runners.toLocaleString("de-AT")} Läufer:innen · ${info.n.toLocaleString("de-AT")} Ergebnisse insgesamt.</p>
    <div class="stats">
      <div class="stat"><b>${gold}</b><span>Gold</span></div>
      <div class="stat"><b>${silver}</b><span>Silber</span></div>
      <div class="stat"><b>${bronze}</b><span>Bronze</span></div>
    </div>
    <h2>Medaillenspiegel</h2>
    <div class="chips">
      ${typeChip(false, "Alle Medaillen")}
      ${typeChip(true, "ÖM / ÖSTM")}
    </div>
    <div class="chips">
      ${yearChip(null, "Alle")}
      ${years.map((y) => yearChip(y, y)).join("")}
    </div>
    ${tableHtml}`;

  if (isOm) {
    app.querySelectorAll("tr.expandable").forEach((row) => {
      row.addEventListener("click", (ev) => {
        if (ev.target.closest("a")) return;
        const detail = app.querySelector(`tr.detail-row[data-detail="${row.dataset.toggle}"]`);
        if (!detail) return;
        detail.hidden = !detail.hidden;
        row.classList.toggle("expanded", !detail.hidden);
      });
    });
  }
}

function viewClubDns(name, yearParam, modeParam) {
  const currentYear = String(new Date().getFullYear());
  const mode = modeParam === "runner" ? "runner" : "event";

  const allRows = query(`
    SELECT e.id AS event_id, e.title AS event_title,
           COALESCE(s.date, e.date_from) AS date, r.category, r.category_full,
           p.id AS person_id, p.name AS person_name
    FROM result r
    JOIN stage s ON s.id = r.stage_id
    JOIN event e ON e.id = s.event_id
    JOIN person p ON p.id = r.person_id
    WHERE r.official_club = ? AND r.status = 'dns' AND r.source = 'anne-api'
      AND COALESCE(s.date, e.date_from) >= '2026-01-01'
    ORDER BY date, e.id`, [name]);

  const years = [...new Set(allRows.map((r) => r.date.slice(0, 4)).concat([currentYear]))].sort();
  const year = yearParam === "alle" ? null : (yearParam || currentYear);
  const rows = year ? allRows.filter((r) => r.date.startsWith(year)) : allRows;

  const yearChip = (val, label) => `<a class="chip ${(year === val) || (!year && val === null) ? "active" : ""}"
      href="#/club/${encodeURIComponent(name)}/dns/${val || "alle"}/${mode}">${label}</a>`;
  const modeChip = (val, label) => `<a class="chip ${mode === val ? "active" : ""}"
      href="#/club/${encodeURIComponent(name)}/dns/${yearParam === "alle" ? "alle" : year || currentYear}/${val}">${label}</a>`;

  let bodyHtml;
  if (rows.length === 0) {
    bodyHtml = `<p class="dim">Keine Einträge gefunden.</p>`;
  } else if (mode === "runner") {
    const byPerson = new Map();
    for (const r of rows) {
      if (!byPerson.has(r.person_id)) byPerson.set(r.person_id, { person_id: r.person_id, person_name: r.person_name, entries: [] });
      byPerson.get(r.person_id).entries.push(r);
    }
    const runners = [...byPerson.values()].sort((a, b) => a.person_name.localeCompare(b.person_name));
    bodyHtml = runners.map((g) => `
      <div class="cat-block">
        <div class="cat-head"><h3>${esc(g.person_name)}</h3></div>
        <table>
          <thead><tr><th>Wettkampf</th><th class="hide-sm">Kategorie</th><th class="num">Datum</th></tr></thead>
          <tbody>${g.entries.map((r) => `
            <tr>
              <td><a href="#/event/${r.event_id}">${esc(r.event_title)}</a></td>
              <td class="hide-sm dim">${esc(r.category_full || r.category)}</td>
              <td class="num dim">${fmtDate(r.date)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>`).join("");
  } else {
    const byEvent = [];
    for (const r of rows) {
      let g = byEvent[byEvent.length - 1];
      if (!g || g.event_id !== r.event_id) {
        g = { event_id: r.event_id, event_title: r.event_title, date: r.date, entries: [] };
        byEvent.push(g);
      }
      g.entries.push(r);
    }
    bodyHtml = byEvent.map((g) => `
      <div class="cat-block">
        <div class="cat-head">
          <h3><a href="#/event/${g.event_id}">${esc(g.event_title)}</a></h3>
          <span class="course">${fmtDate(g.date)}</span>
        </div>
        <table>
          <thead><tr><th>Läufer:in</th><th>Kategorie</th></tr></thead>
          <tbody>${g.entries.map((r) => `
            <tr>
              <td><a href="#/runner/${r.person_id}">${esc(r.person_name)}</a></td>
              <td class="dim">${esc(r.category_full || r.category)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>`).join("");
  }

  app.innerHTML = `
    <div class="cat-head">
      <h1>${esc(name)} — Nicht angetreten</h1>
      <a class="chip" href="#/club/${encodeURIComponent(name)}">← Verein</a>
    </div>
    <p class="sub">Registrierte, aber nicht gestartete Läufer:innen bei Wettkämpfen ab 2026 (laut ANNE).</p>
    <div class="chips">
      ${yearChip(null, "Alle")}
      ${years.map((y) => yearChip(y, y)).join("")}
    </div>
    <div class="chips">
      ${modeChip("event", "Nach Wettkampf")}
      ${modeChip("runner", "Nach Läufer:in")}
    </div>
    ${bodyHtml}`;
}

/* ---------- routing & boot ---------- */

function setActiveNav(name) {
  document.querySelectorAll(".nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === name));
}

function route() {
  if (!db) return;
  const hash = location.hash || "#/";
  let m;
  if ((m = hash.match(/^#\/runner\/(-?\d+)(?:\/(\d{4}))?/))) { viewRunner(Number(m[1]), m[2]); setActiveNav(); }
  else if ((m = hash.match(/^#\/event\/(\d+)(?:\/stage\/(\d+))?(?:(?:\/(om))|(?:\/lm\/(WIEN|NOE|BGLD|STMK|OOE|SBG|TIR|KTN|VBG)))?/))) {
    viewEvent(Number(m[1]), m[3] === "om", m[2] != null ? Number(m[2]) : null, m[4] || null); setActiveNav();
  }
  else if ((m = hash.match(/^#\/events(?:\/(\d{4}))?(?:\/(om))?(?:\/(meister))?/))) {
    viewEvents(m[1], m[2] === "om", m[3] === "meister"); setActiveNav("events");
  }
  else if ((m = hash.match(/^#\/runners(?:\/([A-Z#]))?/))) { viewRunners(m[1]); setActiveNav("runners"); }
  else if ((m = hash.match(/^#\/club\/([^/]+)\/dns(?:\/(\d{4}|alle))?(?:\/(event|runner))?/))) {
    viewClubDns(decodeURIComponent(m[1]), m[2], m[3]); setActiveNav("clubs");
  }
  else if ((m = hash.match(/^#\/club\/([^/]+)(?:\/(\d{4}))?(?:\/(om))?/))) {
    viewClub(decodeURIComponent(m[1]), m[2], m[3]); setActiveNav("clubs");
  }
  else if ((m = hash.match(/^#\/clubs/))) { viewClubs(); setActiveNav("clubs"); }
  else { viewHome(); setActiveNav(); }
  window.scrollTo(0, 0);
}

async function loadDb(SQL, { bustCache = false } = {}) {
  // Every normal page load already asks for this deploy's database (the
  // ?v= build id, injected at deploy time, changes on every push - browsers
  // that respect it never see a stale DB just from opening the page again).
  // A manual refresh goes further and bypasses the cache outright with a
  // timestamp, for the nightly-sync-only case where the app shell itself
  // didn't change but the data did.
  const build = window.OLR_BUILD || "dev";
  const url = bustCache ? `data/results.db.gz?v=${build}&t=${Date.now()}` : `data/results.db.gz?v=${build}`;
  const resp = await fetch(url, bustCache ? { cache: "reload" } : {});
  const stream = resp.body.pipeThrough(new DecompressionStream("gzip"));
  const buf = await new Response(stream).arrayBuffer();
  db = new SQL.Database(new Uint8Array(buf));
  runnersCache = null;  // rebuilt lazily from the new db
  clubsCache = null;
}

let sqlEngine = null;

async function refreshData() {
  const btn = document.getElementById("refresh");
  if (btn) btn.classList.add("spinning");
  try {
    await loadDb(sqlEngine, { bustCache: true });
    route();
  } finally {
    if (btn) btn.classList.remove("spinning");
  }
}

async function boot() {
  sqlEngine = await initSqlJs({
    locateFile: (f) => `https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.10.3/${f}`,
  });
  await loadDb(sqlEngine);
  setupSearch();
  setupDisciplineFilter();
  document.getElementById("refresh").addEventListener("click", refreshData);
  route();
}

window.addEventListener("hashchange", route);
boot().catch((err) => {
  app.innerHTML = `<h1>Fehler</h1><p class="sub">${esc(err.message)}</p>`;
});
