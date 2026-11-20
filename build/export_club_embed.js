"use strict";
// Execute the same podium queries as the main application, against the newly
// built database. Only the public fields needed by the club widget are saved.
const fs = require("node:fs");
const vm = require("node:vm");
const {execFileSync} = require("node:child_process");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const database = path.resolve(process.argv[2] || path.join(root, "site/data/results.db"));
const output = path.resolve(process.argv[3] || path.join(root, "site/embed/naturfreunde-wien/data.json"));
const source = fs.readFileSync(path.join(root, "site/app.js"), "utf8");
const boot = source.indexOf('window.addEventListener("hashchange", route);');
if (boot < 0) throw new Error("Application boot marker missing");
const rules = require(path.join(root, "site/domain_rules.js"));
const python = `import json,sqlite3,sys\nc=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)\nc.row_factory=sqlite3.Row\nprint(json.dumps([dict(r) for r in c.execute(sys.argv[2],json.loads(sys.argv[3]))],ensure_ascii=False))`;
const context = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
  location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams,
  runSql:(sql, params) => JSON.parse(execFileSync("python3", ["-c", python, database, sql, JSON.stringify(params)], {encoding:"utf8", maxBuffer:50*1024*1024}))});
vm.runInContext(source.slice(0, boot) + `\nquery = (sql, params=[]) => runSql(sql,params);\nglobalThis.national = fetchPodiums({club:'Naturfreunde Wien',dw:{sql:'',params:[]},includeAdditional:true}).filter(r => ['ÖM','ÖSTM'].includes(r.championship) && isMedalRank(r.national_rank));\nglobalThis.vienna = fetchRegionalPodiums({jurisdiction:'WIEN',club:'Naturfreunde Wien',dw:{sql:'',params:[]}});`, context);
const fields = ["result_id","rank","national_rank","category","category_full","result_kind","championship","status","team_number","team_name","note","result_list_id","event_id","event_title","sport_type","stage_title","stage_id","stage_number","date","person_id","person_name","observed_name","club_name","regional_entry_id","identity_basis"];
const compact = rows => Array.from(rows, r => Object.fromEntries(fields.filter(k => r[k] != null).map(k => [k,r[k]])));
const history = JSON.parse(fs.readFileSync(path.join(root,"site/data/club-success-history.json"),"utf8")).clubs["Naturfreunde Wien"];
const payload = {schema_version:1, generated_at:new Date().toISOString(), club:"Naturfreunde Wien", national:compact(context.national), vienna:compact(context.vienna), history};
if (!payload.national.length || !payload.vienna.length) throw new Error("Empty club medal export");
// Guard the projection against omitting a field used for team deduplication.
for (const [original, exported] of [[context.national,payload.national],[context.vienna,payload.vienna]]) {
  const years = new Set(original.map(r=>rules.seasonYear(r.date,r.sport_type)));
  for (const year of years) for (const sport of [null,"footOrienteering","skiOrienteering","mountainbikeOrienteering","trailOrienteering"]) {
    const select = rows => Array.from(rows).filter(r=>rules.seasonYear(r.date,r.sport_type)===year && (!sport || !r.sport_type || r.sport_type===sport));
    if (JSON.stringify(rules.clubMedalCounts(select(original))) !== JSON.stringify(rules.clubMedalCounts(select(exported)))) throw new Error(`Club medal export mismatch: ${year} ${sport}`);
  }
}
fs.mkdirSync(path.dirname(output),{recursive:true});
fs.writeFileSync(output,JSON.stringify(payload));
console.log(`Club embed: ${payload.national.length} national and ${payload.vienna.length} Vienna person/team rows -> ${output}`);
