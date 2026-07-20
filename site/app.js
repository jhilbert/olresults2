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

// A "family" result_kind row never has a person_id (see fetchPodiums), but
// some family/group entries still slip through as result_kind='individual'
// with a person record whose "name" is really the group label ("Familie
// Raffeiner", "Fam. Kubanek", "Benjamin+Samuel Pauser+Rausböck", "Nina + Leo
// Madl", "... + Begleitung ..."), so they resolve to a real person_id and
// would otherwise appear as an individual club member. Filters these out of
// any "real runners" listing (club roster, member counts) by name pattern -
// a display-layer heuristic, not a fix for the underlying identity data.
const isFamilyPlaceholderName = (name) => /^fam(ilie|\.|illie|ille)?\b|\+|begleit| und /i.test(name || "");
const NOT_FAMILY_PLACEHOLDER_SQL =
  `p.name NOT LIKE 'Fam%' AND p.name NOT LIKE '%+%' AND p.name NOT LIKE '%Begleit%' AND p.name NOT LIKE '% und %'`;

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

/* ---------- discipline filter (Fuß / SkiO / MTBO / Trail-O) ---------- */

// event.sport_type, as ANNE reports it, is the only per-event discipline
// signal in the schema - there's no separate per-stage field, so every
// stage of a multi-day event shares its event's one sport_type. Only a
// legacy event ANNE never classified (sport_type NULL) passes every filter
// state unfiltered; all four known disciplines map onto their own button.
const DISCIPLINES = [
  ["footOrienteering", "Fuß"],
  ["skiOrienteering", "SkiO"],
  ["mountainbikeOrienteering", "MTBO"],
  ["trailOrienteering", "Trail-O"],
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

/* ---------- identity lens (Verein / Läufer:in / Disziplin) ---------- */

// The currently filtered club/runner lives in exactly one place: the URL's
// query string (?club=...&runner=<person.id>), so a link can be shared that
// opens straight into that filter. localStorage is only a fallback for a
// bare visit with no query string at all - the header lens is the only way
// to change it, from anywhere, and every change re-syncs both. official_club
// is the only club key the schema has (no clubs table), so the URL stores
// that string directly; a runner is stored by person.id alone, never
// name/ÖFOL (both resolved fresh from the loaded db every time, see
// resolveRunner) so a DB rebuild or a person_redirect merge can't leave a
// stale name sitting in a bookmarked link.
const IDENTITY_KEY = "olr-identity";

function readJSON(store, key) {
  try { return JSON.parse(store.getItem(key) || "null"); } catch { return null; }
}

function readIdentity() {
  const params = new URLSearchParams(location.search);
  const urlClub = params.get("club");
  const urlRunner = params.get("runner");
  if (urlClub || urlRunner) return { club: urlClub || null, runnerId: urlRunner ? +urlRunner : null };
  return readJSON(localStorage, IDENTITY_KEY) || { club: null, runnerId: null };
}
let identity = readIdentity();

function syncIdentityURL() {
  const params = new URLSearchParams(location.search);
  identity.club ? params.set("club", identity.club) : params.delete("club");
  identity.runnerId ? params.set("runner", identity.runnerId) : params.delete("runner");
  const qs = params.toString();
  history.replaceState(null, "", location.pathname + (qs ? "?" + qs : "") + location.hash);
}

function setIdentity(patch) {
  identity = { ...identity, ...patch };
  localStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
  syncIdentityURL();
}

function clubHits(q, limit = 12) {
  const dw = disciplineWhere("e.sport_type");
  return query(
    `SELECT r.official_club AS name, COUNT(*) AS n
     FROM result r JOIN stage s ON s.id = r.stage_id JOIN event e ON e.id = s.event_id
     WHERE r.official_club IS NOT NULL AND r.official_club LIKE ?${dw.sql}
     GROUP BY r.official_club ORDER BY n DESC LIMIT ?`, [`%${q}%`, ...dw.params, limit]);
}
// A runner's ÖFOL-ID (only ~12% have one) is shown to help disambiguate a
// search hit, but only person.id is ever persisted - already the site's own
// "#/runner/id" route, with person_redirect covering the rest.
function runnerHits(q, limit = 10) {
  const dw = disciplineWhere("e.sport_type");
  return query(
    `SELECT p.id AS person_id, p.name, p.year_of_birth AS yob,
            (SELECT identifier FROM person_identifier
             WHERE person_id = p.id AND scheme = 'oefol_id' LIMIT 1) AS oefol_id,
            COUNT(r.id) AS starts
     FROM person p JOIN result r ON r.person_id = p.id
     JOIN stage s ON s.id = r.stage_id JOIN event e ON e.id = s.event_id
     WHERE p.name LIKE ? AND r.result_kind != 'team'${dw.sql}
     GROUP BY p.id ORDER BY starts DESC LIMIT ?`, [`%${q}%`, ...dw.params, limit]);
}

// Resolves identity.runnerId (a bare person.id, possibly stale after a
// merge) to display info fresh from the loaded db every time, rather than
// ever persisting a name - and self-heals a redirected id back into
// identity so a bookmarked link keeps working after a future rebuild.
function resolveRunner(id) {
  if (id == null) return null;
  let [p] = query("SELECT id, name FROM person WHERE id = ?", [id]);
  if (!p) {
    const [redirect] = query("SELECT new_id FROM person_redirect WHERE old_id = ?", [id]);
    if (redirect) {
      [p] = query("SELECT id, name FROM person WHERE id = ?", [redirect.new_id]);
      if (p) setIdentity({ runnerId: p.id });
    }
  }
  if (!p) return null;
  const [oefol] = query(
    "SELECT identifier FROM person_identifier WHERE person_id = ? AND scheme = 'oefol_id' LIMIT 1", [p.id]);
  return { person_id: p.id, name: p.name, oefol_id: oefol ? oefol.identifier : null };
}

// The header lens now only holds the Disziplin chip - Verein/Läufer:in
// selection moved onto their own nav pages (see clubSearchHtml/
// runnerSearchHtml below), each with its own inline search instead of a
// floating panel shared from the header.
function renderLens() {
  const discFull = disciplineFilter.size === DISCIPLINES.length;
  const discLabel = discFull ? "Disziplin"
    : DISCIPLINES.filter(([v]) => disciplineFilter.has(v)).map(([, l]) => l).join(" · ");
  document.getElementById("lens").innerHTML = `
    <button class="lens-chip ${discFull ? "" : "set"}" data-lens="discipline">
      <span class="ic">🧭</span>${esc(discLabel)} ▾</button>`;
}

let lensOpen = false;
function closeLensPanel() { document.getElementById("lens-panel")?.remove(); lensOpen = false; }

function openDisciplinePanel(anchor) {
  closeLensPanel();
  const panel = document.createElement("div");
  panel.className = "lens-panel";
  panel.id = "lens-panel";
  panel.innerHTML = `<h4>Disziplinen</h4>
    ${DISCIPLINES.map(([v, l]) => `<label class="disc-check">
      <input type="checkbox" value="${v}" ${disciplineFilter.has(v) ? "checked" : ""}> ${l}</label>`).join("")}
    <small class="dim">Events ohne Disziplin (nicht klassifiziert) bleiben immer sichtbar.</small>`;
  anchor.parentNode.appendChild(panel);
  lensOpen = true;
}

// Inline Verein-Suche für die "Vereine"-Seite, wenn (noch) keiner gewählt
// ist - direkt im Seiteninhalt statt in einem Header-Panel. Ist bereits
// einer gewählt, übernimmt stattdessen der h1-Titel der Detailansicht
// selbst plus ein "Verein ändern"-Link (siehe clubDetailHtml) - keine
// zweite, redundante Chip-Anzeige derselben Auswahl. data-club/data-clear
// werden vom selben delegierten Klick-Handler unten bedient, egal wo im
// DOM sie auftauchen.
function clubSearchHtml() {
  return `<div class="page-pick">
    <input id="club-picker-input" class="page-pick-input" placeholder="Verein suchen …" autocomplete="off">
    <div id="club-picker-results" class="page-pick-results" hidden></div>
  </div>`;
}
function wireClubPicker() {
  const inp = document.getElementById("club-picker-input");
  if (!inp) return;
  const res = document.getElementById("club-picker-results");
  inp.addEventListener("input", () => {
    const q = inp.value.trim();
    if (!q) { res.hidden = true; return; }
    const hits = clubHits(q);
    res.innerHTML = hits.length
      ? hits.map((c) => `<button data-club="${esc(c.name)}">${esc(c.name)} <small>${c.n} Erg.</small></button>`).join("")
      : `<button disabled><small>keine Treffer</small></button>`;
    res.hidden = false;
  });
}

// Same idea for "Läufer:innen": search-only, the picked state is folded
// into runnerDetailHtml's own h1 + "Läufer:in ändern" link instead.
function runnerSearchHtml() {
  return `<div class="page-pick">
    <input id="runner-picker-input" class="page-pick-input" placeholder="Läufer:in suchen …" autocomplete="off">
    <div id="runner-picker-results" class="page-pick-results" hidden></div>
  </div>`;
}
function wireRunnerPicker() {
  const inp = document.getElementById("runner-picker-input");
  if (!inp) return;
  const res = document.getElementById("runner-picker-results");
  inp.addEventListener("input", () => {
    const q = inp.value.trim();
    if (q.length < 2) { res.hidden = true; return; }
    const hits = runnerHits(q);
    res.innerHTML = hits.length
      ? hits.map((r2) => `<button data-runner="${r2.person_id}">${esc(r2.name)}
          <small>${r2.yob ? "Jg. " + r2.yob + " · " : ""}${r2.starts} Starts · ${r2.oefol_id ? "ÖFOL " + esc(r2.oefol_id) : "Anne-ID " + r2.person_id}</small></button>`).join("")
      : `<button disabled><small>keine Treffer</small></button>`;
    res.hidden = false;
  });
}

// Wires the header's Disziplin chip plus every inline Verein-/Läufer:in-
// picker (wherever in the page they render) with one shared set of
// document-level delegated listeners, added once at boot.
function setupIdentity() {
  renderLens();

  document.addEventListener("click", (ev) => {
    const discChip = ev.target.closest('[data-lens="discipline"]');
    if (discChip) { ev.stopPropagation(); lensOpen ? closeLensPanel() : openDisciplinePanel(discChip); return; }

    const clubOpt = ev.target.closest("[data-club]");
    if (clubOpt) {
      setIdentity({ club: clubOpt.dataset.club === "__none" ? null : clubOpt.dataset.club });
      route(); return;
    }
    const runnerOpt = ev.target.closest("[data-runner]");
    if (runnerOpt) {
      const id = runnerOpt.dataset.runner;
      setIdentity({ runnerId: id === "__none" ? null : +id });
      route(); return;
    }
    const clearBtn = ev.target.closest("[data-clear]");
    if (clearBtn) {
      if (clearBtn.dataset.clear === "club") setIdentity({ club: null });
      if (clearBtn.dataset.clear === "runner") setIdentity({ runnerId: null });
      route(); return;
    }

    if (!ev.target.closest(".lens-panel")) closeLensPanel();
  });

  // Disziplin-Checkboxen togglen live, Panel bleibt offen (Mehrfachauswahl)
  document.addEventListener("change", (ev) => {
    const cb = ev.target.closest(".lens-panel .disc-check input[type=checkbox]");
    if (!cb) return;
    if (cb.checked) disciplineFilter.add(cb.value); else disciplineFilter.delete(cb.value);
    // never allow an empty filter - that would just hide everything, which
    // is strictly worse than not filtering at all
    if (disciplineFilter.size === 0) {
      disciplineFilter = new Set(DISCIPLINES.map(([v]) => v));
      document.querySelectorAll(".lens-panel .disc-check input").forEach((i) => { i.checked = true; });
    }
    saveDisciplineFilter();
    // update just the chip in place - renderLens() would overwrite #lens
    // and remove the still-open panel, which is one of its children
    const chip = document.querySelector('.lens-chip[data-lens="discipline"]');
    if (chip) {
      const discFull = disciplineFilter.size === DISCIPLINES.length;
      const label = discFull ? "Disziplin" : DISCIPLINES.filter(([v]) => disciplineFilter.has(v)).map(([, l]) => l).join(" · ");
      chip.classList.toggle("set", !discFull);
      chip.innerHTML = `<span class="ic">🧭</span>${esc(label)} ▾`;
    }
    route();
  });
}

/* ---------- views ---------- */

// Season chips shared by the Wettkämpfe list and by the Vereine/Läufer:innen
// pages' own "nobody picked yet" default content (a national ranking) -
// counts every competition regardless of scope; the medal-table callers
// apply the chosen year themselves via seasonYear() against their own rows.
function competitionYearCounts(dw) {
  const stageRows = query(`
    SELECT COALESCE(s.date, e.date_from) AS date, COUNT(r.id) AS n
    FROM event e JOIN stage s ON s.event_id = e.id JOIN result r ON r.stage_id = s.id
    WHERE 1=1${dw.sql} GROUP BY s.id`, dw.params);
  const yearCounts = new Map();
  for (const r of stageRows) {
    const yr = (r.date || "").slice(0, 4);
    yearCounts.set(yr, (yearCounts.get(yr) || 0) + 1);
  }
  return { total: stageRows.length, years: [...yearCounts.entries()].sort((a, b) => b[0].localeCompare(a[0])) };
}

// The shared "Gruppe" row for a Medaillenspiegel view: ÖM/ÖSTM locked on (a
// raw sum across every competition is meaningless in a medal table, see
// renderRankedMedalTable) plus the still-pending Wiener MS option - WMS
// placements need their own Wien-scoped rank (analogous to national_rank)
// to tell a Vienna-eligible podium from a non-eligible one who simply
// placed well overall, and that field doesn't exist yet
// (championship_instance only marks WHICH stage/category is WMS-eligible,
// state=candidate, not WHO placed 1-2-3 within it) - a data-pipeline
// dependency, not something the UI can safely compute on its own.
function medalGroupRow() {
  return `<div class="chips">
    <span class="badge champ-badge locked" title="Medaillenspiegel zeigt nur ÖM/ÖSTM (bald auch Wiener MS) - eine Summe über alle Wettkämpfe wäre hier wenig aussagekräftig.">✓ ÖM/ÖSTM</span>
    <span class="badge champ-badge disabled" title="Für Wiener MS fehlt noch ein eigener Wien-Rang (analog national_rank) - championship_instance (WIEN/WMS) markiert bisher nur die Kategorie, nicht die Platzierung, und ist state=candidate. Daten-/Build-Abstimmung nötig.">Wiener MS</span>
  </div>`;
}

function viewEvents(year, omOnly, top3) {
  const dw = disciplineWhere("e.sport_type");
  // One row per STAGE, not per event: a multi-day event (e.g. a 3-day
  // festival with a separate Sprint/Middle/Long each day) is really 3
  // distinct competitions, each with its own date and its own results -
  // collapsing them into a single event-level row hid that.
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

  const evHref = ({ y = year, om = omOnly, t3 = top3 } = {}) => {
    const segs = [];
    if (y) segs.push(y);
    if (om) segs.push("om");
    if (t3) segs.push("top3");
    return "#/events" + (segs.length ? "/" + segs.join("/") : "");
  };
  const yearChip = (val, label, n) => `<a class="chip ${(!year && !val) || year === val ? "active" : ""}"
      href="${evHref({ y: val })}">${label}${n != null ? ` <span>${n}</span>` : ""}</a>`;
  const omToggle = `<a class="badge champ-badge champ-toggle ${omOnly ? "active" : ""}"
      href="${evHref({ om: !omOnly })}">${omOnly ? "✓ " : ""}ÖM/ÖSTM${!omOnly ? ` (${omCount})` : ""}</a>`;
  const top3Toggle = `<a class="badge champ-badge champ-toggle ${top3 ? "active" : ""}"
      href="${evHref({ t3: !top3 })}">${top3 ? "✓ " : ""}Top 3</a>`;
  const wmsBadge = `<span class="badge champ-badge disabled" title="Für Wiener MS fehlt noch ein eigener Wien-Rang (analog national_rank) - championship_instance (WIEN/WMS) markiert bisher nur die Kategorie, nicht die Platzierung, und ist state=candidate. Daten-/Build-Abstimmung nötig.">Wiener MS</span>`;

  const champsByStage = new Map();
  if (top3) {
    for (const r of query(`
        SELECT r.stage_id, r.category, r.championship, r.national_rank, p.name AS person_name, r.club
        FROM result r JOIN person p ON p.id = r.person_id
        WHERE r.championship IS NOT NULL AND r.national_rank <= 3 AND r.status = 'ok'
        ORDER BY r.stage_id, r.category, r.national_rank`)) {
      if (!champsByStage.has(r.stage_id)) champsByStage.set(r.stage_id, new Map());
      const byCat = champsByStage.get(r.stage_id);
      if (!byCat.has(r.category)) byCat.set(r.category, { championship: r.championship, tiers: { 1: [], 2: [], 3: [] } });
      byCat.get(r.category).tiers[r.national_rank].push({ name: r.person_name, club: r.club });
    }
  }
  const bodyHtml = `
    <table>
      <thead><tr><th>Datum</th><th>Wettkampf</th><th class="hide-sm">Ort</th><th class="num">Ergebnisse</th></tr></thead>
      <tbody>${shown.length ? shown.map((r) => {
        // each row is one race; link straight to that race's own clean
        // results page. The stage name is only appended when the meet has
        // more than one race (otherwise the event title alone names it).
        const multi = stagesPerEvent.get(r.event_id) > 1;
        const stageLabel = multi ? (r.stage_title || `Etappe ${r.number}`) : "";
        const href = multi ? `#/event/${r.event_id}/stage/${r.number}` : `#/event/${r.event_id}`;
        const champs = top3 ? champsByStage.get(r.stage_id) : null;
        return `
        <tr>
          <td class="dim">${fmtDate(r.date)}</td>
          <td><a href="${href}">${esc(r.title)}${stageLabel ? ` <span class="dim">· ${esc(stageLabel)}</span>` : ""}</a></td>
          <td class="hide-sm dim">${esc(r.location || "")}</td>
          <td class="num">${r.n}</td>
        </tr>${champs ? `<tr class="detail-row"><td></td><td colspan="3">${renderChampions(champs)}</td></tr>` : ""}`;
      }).join("") : `<tr><td colspan="4" class="dim">Keine Wettkämpfe für diesen Filter</td></tr>`}
      </tbody>
    </table>`;

  app.innerHTML = `
    <h1>Wettkämpfe</h1>
    <p class="sub">${stageRows.length.toLocaleString("de-AT")} Wettkämpfe mit Ergebnissen${year ? ` · ${shown.length} in ${year}` : ""}.</p>
    <div class="chips">
      ${yearChip(null, "Alle", stageRows.length)}
      ${years.map(([yr, n]) => yearChip(yr, yr, n)).join("")}
    </div>
    <div class="chips">
      ${omToggle}
      ${wmsBadge}
      ${top3Toggle}
    </div>
    ${bodyHtml}`;
}

// A runner's own results, every event they've ever started - shared by
// their own page and the hub's Wettkampfliste when that runner is filtered
// there, so both show literally the same rows in the same shape.
function fetchRunnerRows(id, dw) {
  const allRows = query(`
    SELECT r.*, e.id AS event_id, e.title AS event_title, e.location, e.country,
           e.competition_type, e.sport_type, s.date AS stage_date, s.title AS stage_title,
           s.number AS stage_number, e.date_from,
           cs.starters, cs.classified, cs.winner_time_s,
           (SELECT COUNT(*) FROM result r2
            WHERE r2.stage_id = r.stage_id AND r2.category NOT LIKE 'bahn%') AS non_bahn_count
    FROM result r
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
  return allRows;
}

function renderRunnerResultsTable(rows) {
  return `
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
          <td class="num">${fmtTime(r.time_s)}</td>
          <td class="num dim">${r.time_behind_s ? "+" + fmtTime(r.time_behind_s) : ""}</td>
          <td class="num">${r.status === "ok" ? fmtPct(r.time_behind_s ?? 0, r.winner_time_s) : ""}</td>
          <td class="hide-sm dim note-cell">${r.note ? esc(r.note) : ""}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

// A runner's own detail content (name, club/birth subtitle, stat tiles,
// season chips, results table) - shared by their own #/runner/id page and
// the Läufer:innen nav page's "Ergebnisse" Ansicht when that runner is
// picked there. Resolves a person_redirect merge; returns null (not an
// error page) when truly not found, so each caller can decide how to show
// that in its own layout.
// Split into header (name + "Läufer:in ändern" + club/birth subtitle - the
// "who is this" part, independent of any toggle) and body (season chips,
// stat tiles, results table - the part that changes with the year filter).
// The Läufer:innen nav page needs its own Ergebnisse/Medaillenspiegel
// Ansicht row wedged in between the two; the direct #/runner/id page has no
// such toggle, so it just concatenates both halves back together.
// withChangeButton mirrors clubDetailHtml: only the nav page has a picker
// to return to.
// Just the identifying part - name, "Läufer:in ändern", club/birth subtitle
// - independent of any year/Ansicht filter, so it renders byte-identical
// whether the Läufer:innen page is showing "Ergebnisse" or "Medaillenspiegel
// (Einzeln)". Both runnerDetailHtml (below) and viewRunnersPage's medals
// branch build their header through this one function so the two can never
// drift apart again.
function runnerHeaderHtml(id, { withChangeButton } = {}) {
  let [p] = query("SELECT * FROM person WHERE id = ?", [id]);
  if (!p) {
    const [redirect] = query("SELECT new_id FROM person_redirect WHERE old_id = ?", [id]);
    if (redirect) [p] = query("SELECT * FROM person WHERE id = ?", [redirect.new_id]);
  }
  if (!p) return null;

  const dw = disciplineWhere("e.sport_type");
  const allRows = fetchRunnerRows(p.id, dw);
  const clubs = [...new Set(allRows.map((r) => r.club).filter(Boolean))].slice(0, 3);

  const html = `
    <div class="cat-head">
      <h1>${esc(p.name)}</h1>
      ${withChangeButton ? `<button class="change-link" data-clear="runner">Läufer:in ändern</button>` : ""}
    </div>
    <p class="sub">${clubs.map(esc).join(" · ")}${p.year_of_birth ? ` · Jg. ${p.year_of_birth}` : ""}</p>`;
  return { p, dw, allRows, html };
}

function runnerDetailHtml(id, year, { withChangeButton } = {}) {
  const h = runnerHeaderHtml(id, { withChangeButton });
  if (!h) return null;
  const { p, allRows } = h;

  const years = [...new Set(allRows.map((r) => seasonYear(r.stage_date || r.date_from, r.sport_type)).filter(Boolean))]
    .sort((a, b) => b - a);
  const rows = year ? allRows.filter((r) => seasonYear(r.stage_date || r.date_from, r.sport_type) === year) : allRows;

  const countable = rows.filter((r) => !(isBahn(r.category) && r.non_bahn_count > 0));
  const finished = countable.filter((r) => r.status === "ok" && r.rank != null && !isKoHeat(r.category));
  const wins = finished.filter((r) => r.rank === 1).length;
  const podiums = finished.filter((r) => r.rank <= 3).length;

  const chip = (val, label) => `<a class="chip ${(!year && !val) || year === val ? "active" : ""}"
      href="#/runner/${p.id}${val ? "/" + val : ""}">${label}</a>`;

  const body = `
    <div class="chips">
      ${chip(null, "Alle")}
      ${years.map((y) => chip(y, y)).join("")}
    </div>
    <div class="stats">
      <div class="stat"><b>${countable.length}</b><span>Starts</span></div>
      <div class="stat"><b>${wins}</b><span>Siege</span></div>
      <div class="stat"><b>${podiums}</b><span>Podestplätze</span></div>
    </div>
    ${renderRunnerResultsTable(rows)}`;
  return { header: h.html, body };
}

function viewRunner(id, year) {
  const r = runnerDetailHtml(id, year);
  app.innerHTML = r ? r.header + r.body : "<h1>Nicht gefunden</h1>";
}

// Läufer:innen nav page: with a runner picked, the h1 doubles as "currently
// selected" (with "Läufer:in ändern" right next to it - no separate
// picked-chip display); without one, an inline search box. Either way, an
// Ansicht toggle between "Ergebnisse" (that runner's own results) and
// "Medaillenspiegel (Einzeln)" (the national individual ranking, narrowed to
// just that runner when picked - collapsing to their own one-row total
// across every club they've represented, since the table ranks by person,
// not by club). Deliberately independent of whatever happens to be picked
// on the Vereine page - the three nav pages never filter each other.
function viewRunnersPage(year, omGroup, ansicht) {
  const runner = resolveRunner(identity.runnerId);
  const ansichtChip = (val, label) => `<a class="chip ${ansicht === val ? "active" : ""}"
      href="#/runners${year ? "/" + year : ""}${omGroup ? "/om" : ""}${val === "medals" ? "/medals" : ""}">${label}</a>`;
  const ansichtRow = `<div class="chips">
    ${ansichtChip("results", "Ergebnisse")}
    ${ansichtChip("medals", "Medaillenspiegel (Einzeln)")}
  </div>`;

  if (ansicht === "medals") {
    const dw = disciplineWhere("e.sport_type");
    const { years } = competitionYearCounts(dw);
    const podiumsAll = fetchPodiums({ personId: runner ? runner.person_id : null, dw });
    // Unlike Vereine, "Medaillenspiegel (Einzeln)" here is a real 3-way
    // Gruppe choice, not locked to ÖM/ÖSTM - a runner's own full "Alle"
    // podium tally is a reasonable thing to want to see for themselves.
    const typeFiltered = omGroup
      ? podiumsAll.filter((r) => r.championship && r.national_rank <= 3)
      : podiumsAll.filter((r) => r.rank <= 3 && !isKoHeat(r.category));
    const podiums = year ? typeFiltered.filter((r) => seasonYear(r.date, r.sport_type) === year) : typeFiltered;
    const yearChip = (val, label) => `<a class="chip ${(!year && !val) || year === val ? "active" : ""}"
        href="#/runners${val ? "/" + val : ""}${omGroup ? "/om" : ""}/medals">${label}</a>`;
    const groupHref = (om2) => `#/runners${year ? "/" + year : ""}${om2 ? "/om" : ""}/medals`;
    // Landes-Meisterschaften would need their own per-Bundesland rank
    // (analogous to national_rank) AND a Verein→Bundesland mapping to pick
    // the right one automatically - neither exists in the data yet (only
    // Wien has any championship_instance rows at all, and those are
    // state=candidate with no placement info), so this stays a disabled
    // placeholder rather than a guess that could crown the wrong "Meister".
    const groupRow = `<div class="chips">
      <a class="badge champ-badge champ-toggle ${!omGroup ? "active" : ""}" href="${groupHref(false)}">${!omGroup ? "✓ " : ""}Alle</a>
      <a class="badge champ-badge champ-toggle ${omGroup ? "active" : ""}" href="${groupHref(true)}">${omGroup ? "✓ " : ""}ÖM/ÖSTM</a>
      <span class="badge champ-badge disabled" title="Landes-Meisterschaften brauchen einen eigenen Landes-Rang je Bundesland (analog national_rank) sowie eine Zuordnung Verein → Bundesland, um automatisch die passende auszuwählen - beides fehlt in den Daten noch. Daten-/Build-Abstimmung nötig.">Landes MS</span>
    </div>`;

    const head = runner
      ? runnerHeaderHtml(runner.person_id, { withChangeButton: true }).html
      : `<h1>Läufer:innen</h1>${runnerSearchHtml()}`;

    app.innerHTML = `
      ${head}
      ${ansichtRow}
      ${groupRow}
      <div class="chips">${yearChip(null, "Alle")}${years.map(([yr]) => yearChip(yr, yr)).join("")}</div>
      ${renderRankedMedalTable(podiums, { showClub: true, isOm: omGroup, capOutput: 500 })}`;
  } else if (runner) {
    const r = runnerDetailHtml(runner.person_id, year, { withChangeButton: true });
    app.innerHTML = r.header + ansichtRow + r.body;
  } else {
    app.innerHTML = `<h1>Läufer:innen</h1>${runnerSearchHtml()}${ansichtRow}<p class="sub dim">Wähle oben eine Läufer:in, um ihre Ergebnisse zu sehen.</p>`;
  }
  wireExpandableMedalRows();
  wireRunnerPicker();
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

function viewEvent(id, medalsOnly, stageNum) {
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
    ${medalsOnly && hasChamp ? `<p class="sub dim">Kompaktübersicht: nur ÖM/ÖSTM-Altersklassen, nur Medaillenränge.</p>` : ""}`;

  for (const st of stages) {
    const cats = query(`
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
      const results = reorderTeamMembers(query(`
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
      const catChamp = [...new Set(results.map((r) => r.championship).filter(Boolean))];
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
      const placementCell = (r, tier) => tier
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
                const r = unit.rows[0], tier = medalTier[r.national_rank];
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
              const team = unit.rows[0], tier = medalTier[team.national_rank];
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
                </tr>${team.result_kind === "pair" ? "" : unit.rows.map((r) => `<tr class="team-member">
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

// One line per medal tier (Gold/Silber/Bronze) per age bracket, Herren/Damen
// of the same bracket combined - ÖSTM (always Elite) first, then ÖM
// brackets youngest to oldest. byCat comes from the "Top 3" query in
// viewEvents(): national_rank 1-3, not just the winner.
function renderChampions(byCat) {
  const groups = new Map();
  for (const [cat, info] of byCat) {
    const { gender, base } = parseCategoryGenderBase(cat);
    const key = `${info.championship}||${base.toLowerCase()}`;
    if (!groups.has(key)) {
      groups.set(key, {
        championship: info.championship, base, sortAge: categoryAgeSort(base),
        tiers: { 1: { D: null, H: null, other: [] }, 2: { D: null, H: null, other: [] }, 3: { D: null, H: null, other: [] } },
      });
    }
    const g = groups.get(key);
    for (const tier of [1, 2, 3]) {
      const names = info.tiers[tier];
      if (!names.length) continue;
      const label = names.map((n) => `${esc(n.name)}${n.club ? ` <span class="dim">(${esc(n.club)})</span>` : ""}`).join(" / ");
      const slot = g.tiers[tier];
      if (gender === "D" && !slot.D) slot.D = label;
      else if (gender === "H" && !slot.H) slot.H = label;
      else slot.other.push(label);
    }
  }
  const medalIcon = { 1: "🥇", 2: "🥈", 3: "🥉" };
  const list = [...groups.values()].sort((a, b) =>
    (a.championship === "ÖSTM" ? 0 : 1) - (b.championship === "ÖSTM" ? 0 : 1) || a.sortAge - b.sortAge);
  return `<ul class="medal-events">${list.map((g) => `
    <li><span class="badge champ-badge">${g.championship}</span> <b>${esc(g.base)}</b>:
      ${[1, 2, 3].map((tier) => {
        const slot = g.tiers[tier];
        const parts = [slot.H && `H: ${slot.H}`, slot.D && `D: ${slot.D}`, ...slot.other].filter(Boolean);
        return parts.length ? `<div>${medalIcon[tier]} ${parts.join(" · ")}</div>` : "";
      }).join("")}
    </li>`).join("")}
  </ul>`;
}

// Every podium finish (top-3 overall rank, or top-3 national_rank when
// championship-tagged) for an arbitrary scope - a club, a single runner, or
// - with neither set - every podium nationally. Shared by the club detail
// page and the home hub's own Medaillenspiegel Ansicht so the exact same
// query (and the ranked-/chronological-table renderers built on it) isn't
// duplicated between the two.
function fetchPodiums({ club, personId, dw }) {
  const conds = [`r.status = 'ok'`,
    // national_rank is placement among only championship-eligible (Austrian)
    // finishers - it can differ from the overall race `rank` when a
    // foreign/ineligible competitor placed ahead, so the ÖM/ÖSTM table needs
    // rows that wouldn't otherwise make the top-3 by raw rank alone
    `(r.rank <= 3 OR (r.championship IS NOT NULL AND r.national_rank <= 3))`,
    `NOT (r.category LIKE 'bahn%' AND EXISTS (
      SELECT 1 FROM result r2 WHERE r2.stage_id = r.stage_id AND r2.category NOT LIKE 'bahn%'))`];
  const params = [];
  if (club) { conds.push(`r.official_club = ?`); params.push(club); }
  if (personId) { conds.push(`r.person_id = ?`); params.push(personId); }
  // LEFT JOIN, not JOIN: a "family" result_kind has no person_id at all (it's
  // a family unit, not an individually identifiable competitor) - an inner
  // join would silently drop every family podium from a club's medal count,
  // even though the club itself is still perfectly well known. Callers that
  // rank by PERSON (renderRankedMedalTable) skip person_id-less rows
  // themselves instead, since there's no one to attribute them to there.
  return query(`
    SELECT r.rank, r.national_rank, r.category, r.category_full, r.result_kind, r.championship,
           e.id AS event_id, e.title AS event_title, e.sport_type, s.title AS stage_title,
           s.id AS stage_id, s.number AS stage_number,
           COALESCE(s.date, e.date_from) AS date, p.id AS person_id,
           COALESCE(p.name, r.observed_name) AS person_name,
           r.official_club AS club_name
    FROM result r
    JOIN stage s ON s.id = r.stage_id
    JOIN event e ON e.id = s.event_id
    LEFT JOIN person p ON p.id = r.person_id
    WHERE ${conds.join(" AND ")}${dw.sql}
    ORDER BY date DESC`, [...params, ...dw.params]);
}

// One row per runner: Gold/Silber/Bronze/Summe medal counts, sorted like a
// championship table (gold, then silver, then bronze), with a shared rank
// number for ties - plus the ÖSTM-only subset of those same medals broken
// out in "G-S-B" form, since ÖSTM is the more prestigious title within the
// combined ÖM/ÖSTM count rather than a separate total. showClub adds a
// Verein column, needed once the scope is wider than a single club. isOm
// picks which field counts as "medal place": national_rank when scoped to
// ÖM/ÖSTM (it's the only rank that excludes ineligible finishers placed
// ahead), the plain overall rank otherwise - national_rank is null on the
// vast majority of non-championship results, so using it unconditionally
// would silently undercount every podium outside ÖM/ÖSTM.
function renderRankedMedalTable(podiums, { showClub, isOm, capOutput }) {
  const medalRank = (r) => (isOm ? r.national_rank : r.rank);
  const byPerson = new Map();
  for (const r of podiums) {
    if (r.person_id == null) continue;  // a "family" result has no individual to attribute it to
    if (!byPerson.has(r.person_id)) {
      byPerson.set(r.person_id, {
        person_id: r.person_id, person_name: r.person_name, club_name: r.club_name,
        gold: 0, silver: 0, bronze: 0, ostmGold: 0, ostmSilver: 0, ostmBronze: 0, entries: [],
      });
    }
    const p = byPerson.get(r.person_id);
    const mr = medalRank(r);
    if (mr === 1) p.gold++; else if (mr === 2) p.silver++; else if (mr === 3) p.bronze++;
    if (r.championship === "ÖSTM") {
      if (mr === 1) p.ostmGold++; else if (mr === 2) p.ostmSilver++; else if (mr === 3) p.ostmBronze++;
    }
    p.entries.push(r);
  }
  // ties on the overall Gold/Silber/Bronze count break by ÖSTM medal count
  // next, same gold-then-silver-then-bronze precedence, before falling back
  // to name. Every total above is tallied across the FULL podiums list, no
  // matter how large - capOutput below only ever shortens the rendered
  // page, never what got counted, unlike capping the *input* rows (which
  // would silently drop whichever medals didn't make the cut before they
  // could even be counted).
  const people = [...byPerson.values()].sort((a, b) =>
    b.gold - a.gold || b.silver - a.silver || b.bronze - a.bronze
    || b.ostmGold - a.ostmGold || b.ostmSilver - a.ostmSilver || b.ostmBronze - a.ostmBronze
    || a.person_name.localeCompare(b.person_name, "de-AT"));
  let place = 0, prevKey = null;
  people.forEach((p, i) => {
    const key = `${p.gold}-${p.silver}-${p.bronze}-${p.ostmGold}-${p.ostmSilver}-${p.ostmBronze}`;
    if (key !== prevKey) { place = i + 1; prevKey = key; }
    p.place = place;
  });

  const truncated = capOutput && people.length > capOutput;
  const shown = truncated ? people.slice(0, capOutput) : people;
  for (const p of shown) {
    p.entries.sort((a, b) => medalRank(a) - medalRank(b) || b.date.localeCompare(a.date));
    // A legacy multi-day event never gets a real per-stage s.title of its
    // own (only ANNE's own /stages API sets that) - falls back to
    // "Etappe N" whenever this person's own entries reveal the event has
    // more than one distinct stage.
    const eventStages = new Map();
    for (const e of p.entries) {
      if (!eventStages.has(e.event_id)) eventStages.set(e.event_id, new Set());
      eventStages.get(e.event_id).add(e.stage_id);
    }
    for (const e of p.entries) {
      const multiStage = eventStages.get(e.event_id).size > 1;
      e.stage_label = e.stage_title || (multiStage ? `Etappe ${e.stage_number}` : "");
      e.href = multiStage ? `#/event/${e.event_id}/stage/${e.stage_number}` : `#/event/${e.event_id}`;
    }
  }
  const medalLabel = { 1: "Gold", 2: "Silber", 3: "Bronze" };
  const cols = showClub ? 8 : 7;

  return `
  <table>
    <thead><tr>
      <th class="num"></th><th>Läufer:in</th>${showClub ? `<th class="hide-sm">Verein</th>` : ""}
      <th class="num">Gold</th><th class="num">Silber</th>
      <th class="num">Bronze</th><th class="num">Summe</th><th class="num">ÖSTM</th>
    </tr></thead>
    <tbody>${shown.length ? shown.map((p) => `
      <tr class="expandable" data-toggle="${p.person_id}">
        <td class="num dim">${p.place}.</td>
        <td><a href="#/runner/${p.person_id}">${esc(p.person_name)}</a> <span class="expand-icon">▸</span></td>
        ${showClub ? `<td class="hide-sm dim">${esc(p.club_name || "")}</td>` : ""}
        <td class="num">${p.gold || ""}</td>
        <td class="num">${p.silver || ""}</td>
        <td class="num">${p.bronze || ""}</td>
        <td class="num"><b>${p.gold + p.silver + p.bronze}</b></td>
        <td class="num nowrap">${p.ostmGold || p.ostmSilver || p.ostmBronze ? `${p.ostmGold}-${p.ostmSilver}-${p.ostmBronze}` : ""}</td>
      </tr>
      <tr class="detail-row" data-detail="${p.person_id}" hidden>
        <td colspan="${cols}">
          <ul class="medal-events">${p.entries.map((e) => `
            <li><b>${medalLabel[medalRank(e)]}</b>${e.championship ? ` <span class="badge">${e.championship}</span>` : ""} ·
              <a href="${e.href}">${esc(e.event_title)}${e.stage_label && e.stage_label !== e.event_title ? ` · <b>${esc(e.stage_label)}</b>` : ""}</a> ·
              <span class="dim">${esc(e.category_full || e.category)} · ${fmtDate(e.date)}</span></li>`).join("")}
          </ul>
        </td>
      </tr>`).join("") : `<tr><td colspan="${cols}" class="dim">Keine Podestplätze</td></tr>`}
    </tbody>
  </table>`
  + (truncated ? `<p class="sub dim">Top ${capOutput} von ${people.length} Läufer:innen mit Medaillen gezeigt – grenze mit Verein, Läufer:in oder Saison weiter ein.</p>` : "");
}

// Flat chronological podium list (every top-3 finish, not just championship
// medals) - the "Alle Medaillen" counterpart to the ranked ÖM/ÖSTM table
// above. Assumes the caller already filtered to rank<=3 and excluded
// knock-out heats.
function renderChronoMedalTable(podiums, { showClub }) {
  return `
  <table>
    <thead><tr>
      <th>Datum</th><th>Wettkampf</th><th>Kategorie</th><th class="num">Platz</th>
      <th class="hide-sm">Läufer:in</th>${showClub ? `<th class="hide-sm">Verein</th>` : ""}
    </tr></thead>
    <tbody>${podiums.length ? podiums.map((r) => `
      <tr>
        <td class="dim">${fmtDate(r.date)}</td>
        <td><a href="#/event/${r.event_id}">${esc(r.event_title)}</a></td>
        <td>${esc(r.category_full || r.category)}${r.championship ? ` <span class="badge">${r.championship}</span>` : ""}${r.result_kind && r.result_kind !== "individual" ? ` <span class="badge">${{ relay: "Staffel", pair: "Paar", team: "Mannschaft" }[r.result_kind] || r.result_kind}</span>` : ""}</td>
        <td class="num"><span class="rank ${r.rank === 1 ? "rank-1" : ""}">${r.rank}</span></td>
        <td class="hide-sm">${r.person_id == null
          ? `<span class="family-name">${esc(r.person_name || "Family")}</span>`
          : `<a href="#/runner/${r.person_id}">${esc(r.person_name)}</a>`}</td>
        ${showClub ? `<td class="hide-sm dim">${esc(r.club_name || "")}</td>` : ""}
      </tr>`).join("") : `<tr><td colspan="${showClub ? 6 : 5}" class="dim">Keine Podestplätze</td></tr>`}
    </tbody>
  </table>`;
}

// One row per club: Gold/Silber/Bronze/Summe, same tie-break precedence as
// the per-runner table. A podium finisher without an official_club (a
// foreign/unaffiliated athlete) can't be attributed to any club, so is
// simply excluded here - they still count in the per-runner table.
function renderClubMedalTable(podiums, { isOm }) {
  const medalRank = (r) => (isOm ? r.national_rank : r.rank);
  const byClub = new Map();
  for (const r of podiums) {
    if (!r.club_name) continue;
    if (!byClub.has(r.club_name)) byClub.set(r.club_name, { name: r.club_name, gold: 0, silver: 0, bronze: 0 });
    const c = byClub.get(r.club_name);
    const mr = medalRank(r);
    if (mr === 1) c.gold++; else if (mr === 2) c.silver++; else if (mr === 3) c.bronze++;
  }
  const clubs = [...byClub.values()].sort((a, b) =>
    b.gold - a.gold || b.silver - a.silver || b.bronze - a.bronze || a.name.localeCompare(b.name, "de-AT"));
  let place = 0, prevKey = null;
  clubs.forEach((c, i) => {
    const key = `${c.gold}-${c.silver}-${c.bronze}`;
    if (key !== prevKey) { place = i + 1; prevKey = key; }
    c.place = place;
  });

  return `
  <table>
    <thead><tr>
      <th class="num"></th><th>Verein</th>
      <th class="num">Gold</th><th class="num">Silber</th><th class="num">Bronze</th><th class="num">Summe</th>
    </tr></thead>
    <tbody>${clubs.length ? clubs.map((c) => `
      <tr>
        <td class="num dim">${c.place}.</td>
        <td><a href="#/club/${encodeURIComponent(c.name)}">${esc(c.name)}</a></td>
        <td class="num">${c.gold || ""}</td>
        <td class="num">${c.silver || ""}</td>
        <td class="num">${c.bronze || ""}</td>
        <td class="num"><b>${c.gold + c.silver + c.bronze}</b></td>
      </tr>`).join("") : `<tr><td colspan="6" class="dim">Keine Podestplätze</td></tr>`}
    </tbody>
  </table>`;
}

// A club's own detail content (info line, Gold/Silber/Bronze tiles, "Nicht
// angetreten" link, Alle-Medaillen/ÖM toggle, own season chips, table) -
// shared by the direct #/club/:name link and the Vereine nav page when
// that club is picked there. Returns null (not an error page) when no such
// club exists, so each caller can decide how to show that in its own layout.
// withChangeButton adds a "Verein ändern" link next to the h1 - only from
// the Vereine nav page, which is the only context with somewhere to change
// it back to (the direct #/club/:name link has no picker to return to).
// hrefBase lets the internal toggle/year/roster links stay on whichever
// route rendered this (the direct #/club/:name link, or the Vereine nav
// page at #/clubs) instead of always hardcoding #/club/:name - previously
// every internal link jumped straight to the direct-link route, which has
// no picker to return to, so navigating those toggles from the Vereine page
// silently dropped the "Verein ändern" action. withChangeButton mirrors
// that: only the nav page has somewhere to change the pick back to. view
// switches between the medal spiegel (default) and a plain roster of every
// club member, toggled via the info line itself ("N Läufer:innen" /
// "N Ergebnisse") rather than a separate control.
function clubDetailHtml(name, year, medalType, { withChangeButton, hrefBase, view } = {}) {
  const info = query(`
    SELECT COUNT(*) AS n, (
      SELECT COUNT(DISTINCT r2.person_id) FROM result r2 JOIN person p ON p.id = r2.person_id
      WHERE r2.official_club = ? AND r2.result_kind != 'team' AND ${NOT_FAMILY_PLACEHOLDER_SQL}
    ) AS runners
    FROM result WHERE official_club = ?`, [name, name])[0];
  if (!info || !info.n) return null;

  const base = hrefBase || `#/club/${encodeURIComponent(name)}`;
  const isOm = medalType === "om";
  const dw = disciplineWhere("e.sport_type");

  const membersHref = `${base}${year ? "/" + year : ""}${isOm ? "/om" : ""}/members`;
  const resultsHref = `${base}${year ? "/" + year : ""}${isOm ? "/om" : ""}`;
  const header = `
    <div class="cat-head">
      <h1>${esc(name)}</h1>
      ${withChangeButton ? `<button class="change-link" data-clear="club">Verein ändern</button>` : ""}
      <a class="chip" href="#/club/${encodeURIComponent(name)}/dns">Nicht angetreten</a>
    </div>
    <p class="sub">
      <a href="${membersHref}">${info.runners.toLocaleString("de-AT")} Läufer:innen</a> ·
      <a href="${resultsHref}">${info.n.toLocaleString("de-AT")} Ergebnisse</a> insgesamt.
    </p>`;

  if (view === "members") {
    const roster = query(`
      SELECT p.id, p.name, p.year_of_birth, COUNT(*) AS n
      FROM result r
      JOIN stage s ON s.id = r.stage_id JOIN event e ON e.id = s.event_id
      JOIN person p ON p.id = r.person_id
      WHERE r.official_club = ? AND r.result_kind != 'team' AND ${NOT_FAMILY_PLACEHOLDER_SQL}${dw.sql}
      GROUP BY p.id ORDER BY p.name COLLATE NOCASE`, [name, ...dw.params]);
    return header + `
      <table>
        <thead><tr><th>Name</th><th class="num">Jg</th><th class="num">Starts</th></tr></thead>
        <tbody>${roster.map((r) => `
          <tr>
            <td><a href="#/runner/${r.id}">${esc(r.name)}</a></td>
            <td class="num dim">${r.year_of_birth || ""}</td>
            <td class="num">${r.n}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
  }

  const allPodiums = fetchPodiums({ club: name, dw });
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
      href="${base}${val ? "/" + val : ""}${isOm ? "/om" : ""}">${label}</a>`;
  const typeChip = (val, label) => `<a class="chip ${isOm === val ? "active" : ""}"
      href="${base}${year ? "/" + year : ""}${val ? "/om" : ""}">${label}</a>`;

  const tableHtml = isOm
    ? renderRankedMedalTable(podiums, { showClub: false, isOm: true })
    : renderChronoMedalTable(podiums, { showClub: false });

  return header + `
    <div class="chips">
      ${typeChip(false, "Alle Medaillen")}
      ${typeChip(true, "ÖM / ÖSTM")}
    </div>
    <div class="chips">
      ${yearChip(null, "Alle")}
      ${years.map((y) => yearChip(y, y)).join("")}
    </div>
    <div class="stats">
      <div class="stat"><b>${gold}</b><span>Gold</span></div>
      <div class="stat"><b>${silver}</b><span>Silber</span></div>
      <div class="stat"><b>${bronze}</b><span>Bronze</span></div>
    </div>
    ${tableHtml}`;
}

function wireExpandableMedalRows() {
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

function viewClub(name, year, medalType, view) {
  const html = clubDetailHtml(name, year, medalType, { view });
  app.innerHTML = html || "<h1>Nicht gefunden</h1>";
  wireExpandableMedalRows();
}

// Vereine nav page: with a club picked, shows exactly that club's own page
// (clubDetailHtml - the h1 there doubles as "currently selected", with its
// own "Verein ändern" link right next to it, so there's no separate
// picked-chip display duplicating the same name). hrefBase: "#/clubs" keeps
// every internal toggle/year/roster link on this nav page instead of
// jumping to the direct #/club/:name link, which would silently drop the
// "Verein ändern" action. With none picked, an inline search box plus the
// national club ranking as a sensible default. Deliberately independent of
// whatever happens to be picked on the Läufer:innen page - the three nav
// pages never filter each other.
function viewClubsPage(year, medalType, view) {
  if (identity.club) {
    const html = clubDetailHtml(identity.club, year, medalType, { withChangeButton: true, hrefBase: "#/clubs", view });
    app.innerHTML = html || `<h1>Vereine</h1><p class="sub dim">Kein Verein mit diesem Namen gefunden.</p>`;
  } else {
    const dw = disciplineWhere("e.sport_type");
    const { years } = competitionYearCounts(dw);
    const podiumsAll = fetchPodiums({ dw });
    const typeFiltered = podiumsAll.filter((r) => r.championship && r.national_rank <= 3);
    const podiums = year ? typeFiltered.filter((r) => seasonYear(r.date, r.sport_type) === year) : typeFiltered;
    const yearChip = (val, label) => `<a class="chip ${(!year && !val) || year === val ? "active" : ""}"
        href="#/clubs${val ? "/" + val : ""}">${label}</a>`;
    app.innerHTML = `
      <h1>Vereine</h1>
      ${clubSearchHtml()}
      <p class="sub">Bundesweite Vereins-Rangliste – oder wähle oben einen Verein.</p>
      <div class="chips">${yearChip(null, "Alle")}${years.map(([yr]) => yearChip(yr, yr)).join("")}</div>
      ${medalGroupRow()}
      ${renderClubMedalTable(podiums, { isOm: true })}`;
  }
  wireExpandableMedalRows();
  wireClubPicker();
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
  else if ((m = hash.match(/^#\/event\/(\d+)(?:\/stage\/(\d+))?(?:\/(om))?/))) {
    viewEvent(Number(m[1]), m[3] === "om", m[2] != null ? Number(m[2]) : null); setActiveNav();
  }
  else if ((m = hash.match(/^#\/club\/([^/]+)\/dns(?:\/(\d{4}|alle))?(?:\/(event|runner))?/))) {
    viewClubDns(decodeURIComponent(m[1]), m[2], m[3]); setActiveNav();
  }
  else if ((m = hash.match(/^#\/club\/([^/]+)(?:\/(\d{4}))?(?:\/(om))?(?:\/(members))?/))) {
    viewClub(decodeURIComponent(m[1]), m[2], m[3], m[4]); setActiveNav();
  }
  else if ((m = hash.match(/^#\/clubs(?:\/(\d{4}))?(?:\/(om))?(?:\/(members))?/))) {
    viewClubsPage(m[1], m[2], m[3]); setActiveNav("clubs");
  }
  else if ((m = hash.match(/^#\/runners(?:\/(\d{4}))?(?:\/(om))?(?:\/(medals))?/))) {
    viewRunnersPage(m[1], m[2] === "om", m[3] === "medals" ? "medals" : "results"); setActiveNav("runners");
  }
  else if ((m = hash.match(/^#\/events(?:\/(\d{4}))?(?:\/(om))?(?:\/(top3))?/))) {
    viewEvents(m[1], m[2] === "om", m[3] === "top3"); setActiveNav("events");
  }
  else { viewEvents(null, false, false); setActiveNav("events"); }
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
  setupIdentity();
  syncIdentityURL();  // reflect a localStorage-seeded filter into the address bar too, so it's always a shareable link
  document.getElementById("refresh").addEventListener("click", refreshData);
  route();
}

window.addEventListener("hashchange", route);
boot().catch((err) => {
  app.innerHTML = `<h1>Fehler</h1><p class="sub">${esc(err.message)}</p>`;
});
