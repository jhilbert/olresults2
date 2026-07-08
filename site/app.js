/* OL Results — static frontend over site/data/results.db via sql.js */
"use strict";

let db = null;
const app = document.getElementById("app");

/* ---------- helpers ---------- */

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

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

function viewRunner(id) {
  const [p] = query("SELECT * FROM person WHERE id = ?", [id]);
  if (!p) { app.innerHTML = "<h1>Nicht gefunden</h1>"; return; }

  const rows = query(`
    SELECT r.*, e.id AS event_id, e.title AS event_title, e.location, e.country,
           e.competition_type, s.date AS stage_date, s.title AS stage_title, e.date_from,
           cs.starters, cs.classified, cs.winner_time_s
    FROM result r
    JOIN stage s ON s.id = r.stage_id
    JOIN event e ON e.id = s.event_id
    LEFT JOIN category_stats cs ON cs.stage_id = r.stage_id AND cs.category = r.category
    WHERE r.person_id = ?
    ORDER BY COALESCE(s.date, e.date_from) DESC`, [id]);

  const finished = rows.filter((r) => r.status === "ok" && r.rank != null);
  const wins = finished.filter((r) => r.rank === 1).length;
  const podiums = finished.filter((r) => r.rank <= 3).length;
  const clubs = [...new Set(rows.map((r) => r.club).filter(Boolean))].slice(0, 3);

  app.innerHTML = `
    <h1>${esc(p.name)}</h1>
    <p class="sub">${clubs.map(esc).join(" · ")}${p.year_of_birth ? ` · Jg. ${p.year_of_birth}` : ""}</p>
    <div class="stats">
      <div class="stat"><b>${rows.length}</b><span>Starts</span></div>
      <div class="stat"><b>${wins}</b><span>Siege</span></div>
      <div class="stat"><b>${podiums}</b><span>Podestplätze</span></div>
    </div>
    <h2>Ergebnisse</h2>
    <table>
      <thead><tr>
        <th>Datum</th><th>Wettkampf</th><th class="hide-sm">Ort</th><th>Kategorie</th>
        <th class="num">Platz</th><th class="num">Zeit</th><th class="num hide-sm">Diff</th><th class="num">%</th>
        <th class="hide-sm">Bemerkung</th>
      </tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td class="dim">${fmtDate(r.stage_date || r.date_from)}</td>
          <td><a href="#/event/${r.event_id}">${esc(r.event_title)}</a>${r.stage_title ? ` <span class="dim">· ${esc(r.stage_title)}</span>` : ""}</td>
          <td class="hide-sm dim">${esc(r.location || "")}</td>
          <td>${esc(r.category_full || r.category)}${r.result_kind && r.result_kind !== "individual" ? ` <span class="badge">${{ relay: "Staffel", pair: "Paar", team: "Mannschaft" }[r.result_kind] || r.result_kind}</span>` : ""}</td>
          <td class="num">${rankCell(r)}</td>
          <td class="num">${fmtTime(r.time_s)}</td>
          <td class="num hide-sm dim">${r.time_behind_s ? "+" + fmtTime(r.time_behind_s) : ""}</td>
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
            <span class="course">${course}${course ? " · " : ""}${(c.starters ?? c.entries)} Starter</span>
          </div>
          <table>
            <thead><tr><th class="num">Pl</th><th>Name</th><th class="hide-sm">Verein</th>
              <th class="num">Zeit</th><th class="num">Diff</th></tr></thead>
            <tbody>${results.map((r) => `
              <tr>
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
      const like = `%${q}%`;
      const persons = query(
        `SELECT p.id, p.name, p.year_of_birth,
                (SELECT COUNT(*) FROM result r WHERE r.person_id = p.id) AS n
         FROM person p WHERE p.name LIKE ? ORDER BY n DESC LIMIT 8`, [like]);
      const events = query(
        `SELECT e.id, e.title, e.date_from FROM event e
         WHERE e.title LIKE ? AND EXISTS
           (SELECT 1 FROM stage s JOIN result r ON r.stage_id = s.id WHERE s.event_id = e.id)
         ORDER BY e.date_from DESC LIMIT 5`, [like]);
      let html = "";
      if (persons.length) html += `<div class="group">Läufer:innen</div>` +
        persons.map((p) => `<a href="#/runner/${p.id}">${esc(p.name)}
          <span class="meta">${p.year_of_birth ? "Jg. " + p.year_of_birth + " · " : ""}${p.n} Starts</span></a>`).join("");
      if (events.length) html += `<div class="group">Wettkämpfe</div>` +
        events.map((e) => `<a href="#/event/${e.id}">${esc(e.title)}
          <span class="meta">${fmtDate(e.date_from)}</span></a>`).join("");
      dropdown.innerHTML = html || `<div class="group">Keine Treffer</div>`;
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

/* ---------- routing & boot ---------- */

function setActiveNav(name) {
  document.querySelectorAll(".nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === name));
}

function route() {
  if (!db) return;
  const hash = location.hash || "#/";
  let m;
  if ((m = hash.match(/^#\/runner\/(-?\d+)/))) { viewRunner(Number(m[1])); setActiveNav(); }
  else if ((m = hash.match(/^#\/event\/(\d+)/))) { viewEvent(Number(m[1])); setActiveNav(); }
  else if ((m = hash.match(/^#\/events(?:\/(\d{4}))?/))) { viewEvents(m[1]); setActiveNav("events"); }
  else if ((m = hash.match(/^#\/runners(?:\/([A-Z#]))?/))) { viewRunners(m[1]); setActiveNav("runners"); }
  else { viewHome(); setActiveNav(); }
  window.scrollTo(0, 0);
}

async function loadDb(SQL, { bustCache = false } = {}) {
  // bustCache bypasses the browser's HTTP cache so a manual refresh actually
  // re-downloads the database (a normal F5 often reuses the cached ~7 MB file)
  const url = bustCache ? `data/results.db.gz?t=${Date.now()}` : "data/results.db.gz";
  const resp = await fetch(url, bustCache ? { cache: "reload" } : {});
  const stream = resp.body.pipeThrough(new DecompressionStream("gzip"));
  const buf = await new Response(stream).arrayBuffer();
  db = new SQL.Database(new Uint8Array(buf));
  runnersCache = null;  // rebuilt lazily from the new db
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
