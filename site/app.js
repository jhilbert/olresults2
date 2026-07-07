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
      <div class="stat"><b>${s.persons.toLocaleString("de-AT")}</b><span>Läufer:innen</span></div>
      <div class="stat"><b>${s.events.toLocaleString("de-AT")}</b><span>Wettkämpfe</span></div>
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
      </tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td class="dim">${fmtDate(r.stage_date || r.date_from)}</td>
          <td><a href="#/event/${r.event_id}">${esc(r.event_title)}</a>${r.stage_title ? ` <span class="dim">· ${esc(r.stage_title)}</span>` : ""}</td>
          <td class="hide-sm dim">${esc(r.location || "")}</td>
          <td>${esc(r.category_full || r.category)}</td>
          <td class="num">${rankCell(r)}</td>
          <td class="num">${fmtTime(r.time_s)}</td>
          <td class="num hide-sm dim">${r.time_behind_s ? "+" + fmtTime(r.time_behind_s) : ""}</td>
          <td class="num">${r.status === "ok" ? fmtPct(r.time_behind_s ?? 0, r.winner_time_s) : ""}</td>
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
             MAX(r.course_length_m) AS len, MAX(r.course_climb_m) AS climb,
             MAX(r.course_controls) AS ctrls
      FROM result r JOIN category_stats cs ON cs.stage_id = r.stage_id AND cs.category = r.category
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
            <span class="course">${course}${course ? " · " : ""}${c.starters} Starter</span>
          </div>
          <table>
            <thead><tr><th class="num">Pl</th><th>Name</th><th class="hide-sm">Verein</th>
              <th class="num">Zeit</th><th class="num">Diff</th></tr></thead>
            <tbody>${results.map((r) => `
              <tr>
                <td class="num">${rankCell({ ...r, starters: null })}</td>
                <td><a href="#/runner/${r.person_id}">${esc(r.person_name)}</a></td>
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

/* ---------- routing & boot ---------- */

function route() {
  if (!db) return;
  const hash = location.hash || "#/";
  let m;
  if ((m = hash.match(/^#\/runner\/(-?\d+)/))) viewRunner(Number(m[1]));
  else if ((m = hash.match(/^#\/event\/(\d+)/))) viewEvent(Number(m[1]));
  else viewHome();
  window.scrollTo(0, 0);
}

async function boot() {
  const SQL = await initSqlJs({
    locateFile: (f) => `https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.10.3/${f}`,
  });
  const resp = await fetch("data/results.db.gz");
  const stream = resp.body.pipeThrough(new DecompressionStream("gzip"));
  const buf = await new Response(stream).arrayBuffer();
  db = new SQL.Database(new Uint8Array(buf));
  setupSearch();
  route();
}

window.addEventListener("hashchange", route);
boot().catch((err) => {
  app.innerHTML = `<h1>Fehler</h1><p class="sub">${esc(err.message)}</p>`;
});
