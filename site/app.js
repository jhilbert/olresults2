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
  if (r.status !== "ok") return `<span class="status">${esc(r.status)}</span>`;
  if (r.rank == null) return "";
  return `<span class="rank ${r.rank === 1 ? "rank-1" : ""}">${r.rank}</span>` +
         (r.starters ? `<span class="of">/${r.classified}</span>` : "");
}

/* ---------- views ---------- */

function viewHome() {
  const [s] = query(`SELECT
    (SELECT COUNT(*) FROM result) AS results,
    (SELECT COUNT(*) FROM person) AS persons,
    (SELECT COUNT(DISTINCT event_id) FROM stage s JOIN result r ON r.stage_id = s.id) AS events`);
  const recent = query(`
    SELECT e.id, e.title, e.date_from, e.location, COUNT(r.id) AS n
    FROM event e JOIN stage s ON s.event_id = e.id JOIN result r ON r.stage_id = s.id
    GROUP BY e.id ORDER BY e.date_from DESC LIMIT 15`);

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
  const [p] = query("SELECT * FROM person WHERE id = ?", [id]);
  if (!p) { app.innerHTML = "<h1>Nicht gefunden</h1>"; return; }

  const allRows = query(`
    SELECT r.*, e.id AS event_id, e.title AS event_title, e.location, e.country,
           e.competition_type, s.date AS stage_date, s.title AS stage_title, e.date_from,
           cs.starters, cs.classified, cs.winner_time_s,
           (SELECT COUNT(*) FROM result r2
            WHERE r2.stage_id = r.stage_id AND r2.category NOT LIKE 'bahn%') AS non_bahn_count
    FROM result r
    JOIN stage s ON s.id = r.stage_id
    JOIN event e ON e.id = s.event_id
    LEFT JOIN category_stats cs ON cs.stage_id = r.stage_id AND cs.category = r.category
    WHERE r.person_id = ?
    ORDER BY COALESCE(s.date, e.date_from) DESC`, [id]);

  const years = [...new Set(allRows.map((r) => (r.stage_date || r.date_from || "").slice(0, 4)).filter(Boolean))]
    .sort((a, b) => b - a);
  const rows = year ? allRows.filter((r) => (r.stage_date || r.date_from || "").startsWith(year)) : allRows;

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
          <td><a href="#/event/${r.event_id}">${esc(r.event_title)}</a>${r.stage_title ? ` <span class="dim">· ${esc(r.stage_title)}</span>` : ""}</td>
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

function viewEvent(id) {
  const [e] = query("SELECT * FROM event WHERE id = ?", [id]);
  if (!e) { app.innerHTML = "<h1>Nicht gefunden</h1>"; return; }

  const stages = query(
    `SELECT s.* FROM stage s WHERE s.event_id = ?
     AND EXISTS (SELECT 1 FROM result r WHERE r.stage_id = s.id)
     ORDER BY s.number`, [id]);

  let html = `
    <h1>${esc(e.title)}</h1>
    <p class="sub">${fmtDate(e.date_from)} · ${esc(e.location || "")}
      ${e.url ? `· <a href="${esc(e.url)}" target="_blank" rel="noopener">ANNE ↗</a>` : ""}</p>`;

  for (const st of stages) {
    if (stages.length > 1) html += `<h2>${esc(st.title || "Etappe " + st.number)}</h2>`;
    const cats = query(`
      SELECT r.category, MAX(r.category_full) AS category_full,
             cs.starters, cs.classified, cs.winner_time_s,
             COUNT(*) AS entries,
             MAX(r.course_length_m) AS len, MAX(r.course_climb_m) AS climb,
             MAX(r.course_controls) AS ctrls
      FROM result r LEFT JOIN category_stats cs
        ON cs.stage_id = r.stage_id AND cs.category = r.category
      WHERE r.stage_id = ? GROUP BY r.category ORDER BY r.category`, [st.id]);
    const stageHasOfficial = cats.some((c) => !isBahn(c.category));
    for (const c of cats) {
      const results = query(`
        SELECT r.*, p.name AS person_name FROM result r
        JOIN person p ON p.id = r.person_id
        WHERE r.stage_id = ? AND r.category = ?
        ORDER BY CASE WHEN r.rank IS NULL THEN 1 ELSE 0 END, r.rank, r.time_s`,
        [st.id, c.category]);
      const course = [
        c.len ? (c.len / 1000).toFixed(1).replace(".", ",") + " km" : null,
        c.climb ? c.climb + " Hm" : null,
        c.ctrls ? c.ctrls + " Posten" : null,
      ].filter(Boolean).join(" · ");
      html += `
        <div class="cat-block">
          <div class="cat-head">
            <h3>${esc(c.category_full || c.category)}</h3>
            <span class="course">${course}${course ? " · " : ""}${(c.starters ?? c.entries)} Starter${isBahn(c.category) && stageHasOfficial ? " · inoffizielle Bahnwertung" : ""}</span>
          </div>
          <table>
            <thead><tr><th class="num">Pl</th><th>Name</th><th class="hide-sm">Verein</th>
              <th class="num">Zeit</th><th class="num">Diff</th></tr></thead>
            <tbody>${results.map((r) => `
              <tr class="${isBahn(c.category) && stageHasOfficial ? "bahn-row" : ""}">
                <td class="num">${rankCell({ ...r, starters: null })}</td>
                <td><a href="#/runner/${r.person_id}">${esc(r.person_name)}</a>${r.note ? `<div class="note">${esc(r.note)}</div>` : ""}</td>
                <td class="hide-sm dim">${esc(r.club || "")}</td>
                <td class="num">${fmtTime(r.time_s)}</td>
                <td class="num dim">${r.status === "ok" && r.time_behind_s ? "+" + fmtTime(r.time_behind_s) : ""}</td>
              </tr>`).join("")}
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
                (SELECT COUNT(*) FROM result r WHERE r.person_id = p.id) AS n
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

function viewEvents(year) {
  const events = query(`
    SELECT e.id, e.title, e.location,
           COALESCE(MIN(s.date), e.date_from) AS date,
           COUNT(r.id) AS n
    FROM event e JOIN stage s ON s.event_id = e.id JOIN result r ON r.stage_id = s.id
    GROUP BY e.id ORDER BY date DESC`);
  const years = query(`
    SELECT substr(COALESCE(s.date, e.date_from), 1, 4) AS yr, COUNT(DISTINCT e.id) AS n
    FROM event e JOIN stage s ON s.event_id = e.id JOIN result r ON r.stage_id = s.id
    GROUP BY yr ORDER BY yr DESC`);

  const shown = year ? events.filter((e) => (e.date || "").startsWith(year)) : events;
  const chip = (val, label, n) =>
    `<a class="chip ${(!year && !val) || year === val ? "active" : ""}"
        href="#/events${val ? "/" + val : ""}">${label}${n != null ? ` <span>${n}</span>` : ""}</a>`;

  app.innerHTML = `
    <h1>Wettkämpfe</h1>
    <p class="sub">${events.length.toLocaleString("de-AT")} Wettkämpfe mit Ergebnissen${year ? ` · ${shown.length} in ${year}` : ""}.</p>
    <div class="chips">
      ${chip(null, "Alle", events.length)}
      ${years.map((y) => chip(y.yr, y.yr, y.n)).join("")}
    </div>
    <table>
      <thead><tr><th>Datum</th><th>Wettkampf</th><th class="hide-sm">Ort</th><th class="num">Ergebnisse</th></tr></thead>
      <tbody>${shown.map((e) => `
        <tr>
          <td class="dim">${fmtDate(e.date)}</td>
          <td><a href="#/event/${e.id}">${esc(e.title)}</a></td>
          <td class="hide-sm dim">${esc(e.location || "")}</td>
          <td class="num">${e.n}</td>
        </tr>`).join("")}
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
    runnersCache = query(`
      SELECT p.id, p.name, p.year_of_birth, COUNT(r.id) AS n
      FROM person p JOIN result r ON r.person_id = p.id
      WHERE r.result_kind != 'team'   -- team rosters aren't individual runners
      GROUP BY p.id ORDER BY p.name COLLATE NOCASE`);
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
    clubsCache = query(`
      SELECT official_club AS name, COUNT(*) AS n, COUNT(DISTINCT person_id) AS runners
      FROM result WHERE official_club IS NOT NULL
      GROUP BY official_club ORDER BY official_club COLLATE NOCASE`);
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
    FROM result WHERE official_club = ?`, [name])[0];
  if (!info || !info.n) { app.innerHTML = "<h1>Nicht gefunden</h1>"; return; }

  // national_rank is placement among only championship-eligible (Austrian)
  // finishers - it can differ from the overall race `rank` when a foreign/
  // ineligible competitor placed ahead, so the ÖM/ÖSTM view needs rows that
  // wouldn't otherwise make the top-3 by raw rank alone.
  const allPodiums = query(`
    SELECT r.rank, r.national_rank, r.category, r.category_full, r.result_kind, r.championship,
           e.id AS event_id, e.title AS event_title, s.title AS stage_title,
           COALESCE(s.date, e.date_from) AS date, p.id AS person_id, p.name AS person_name
    FROM result r
    JOIN stage s ON s.id = r.stage_id
    JOIN event e ON e.id = s.event_id
    JOIN person p ON p.id = r.person_id
    WHERE r.official_club = ? AND r.status = 'ok'
      AND (r.rank <= 3 OR (r.championship IS NOT NULL AND r.national_rank <= 3))
      AND NOT (r.category LIKE 'bahn%' AND EXISTS (
        SELECT 1 FROM result r2
        WHERE r2.stage_id = r.stage_id AND r2.category NOT LIKE 'bahn%'))
    ORDER BY date DESC`, [name]);

  const isOm = medalType === "om";
  const years = [...new Set(allPodiums.map((r) => r.date.slice(0, 4)))].sort((a, b) => b - a);
  const typeFiltered = isOm
    ? allPodiums.filter((r) => r.championship && r.national_rank <= 3)
    : allPodiums.filter((r) => r.rank <= 3 && !isKoHeat(r.category));
  const podiums = year ? typeFiltered.filter((r) => r.date.startsWith(year)) : typeFiltered;
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
                <a href="#/event/${e.event_id}">${esc(e.event_title)}</a>${e.stage_title && e.stage_title !== e.event_title ? ` · <b>${esc(e.stage_title)}</b>` : ""} ·
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
  else if ((m = hash.match(/^#\/event\/(\d+)/))) { viewEvent(Number(m[1])); setActiveNav(); }
  else if ((m = hash.match(/^#\/events(?:\/(\d{4}))?/))) { viewEvents(m[1]); setActiveNav("events"); }
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
  document.getElementById("refresh").addEventListener("click", refreshData);
  route();
}

window.addEventListener("hashchange", route);
boot().catch((err) => {
  app.innerHTML = `<h1>Fehler</h1><p class="sub">${esc(err.message)}</p>`;
});
