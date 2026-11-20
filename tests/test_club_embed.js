"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const rules = require("../site/domain_rules.js");
const root=path.join(__dirname,"../site/embed/naturfreunde-wien");

test("embed uses only club medal data, team totals and non-navigating details", async () => {
  const elements=Object.fromEntries(["modes","year-control","year","sport","content","data-status"].map(id=>[id,{innerHTML:"",hidden:false,handlers:{},addEventListener(type,fn){this.handlers[type]=fn;}}]));
  const base={event_id:1,stage_id:1,result_list_id:"r",category:"Mixed",championship:"ÖSTM",national_rank:1,rank:1,result_kind:"relay",team_number:"1",club_name:"Naturfreunde Wien",date:"2026-05-01",sport_type:"footOrienteering",event_title:"Staffel"};
  const payload={schema_version:1,club:"Naturfreunde Wien",generated_at:"2026-11-20T00:00:00Z",history:{years:{"2000":{national:7,vienna:11}}},national:[{...base,person_id:"1",person_name:"Anna"},{...base,person_id:"2",person_name:"Bea"}],vienna:[{...base,championship:"Wiener MS",regional_entry_id:"v",person_id:"1",person_name:"Anna"}]};
  const ctx=vm.createContext({window:{OLRDomainRules:rules},document:{getElementById:id=>elements[id]},fetch:async()=>({ok:true,json:async()=>payload}),console});
  vm.runInContext(fs.readFileSync(path.join(root,"embed.js"),"utf8"),ctx);
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(elements.content.innerHTML,/<th scope="row">Gesamt<\/th><td class="gold">1<\/td>/);
  assert.match(elements.content.innerHTML,/Anna/);assert.match(elements.content.innerHTML,/Bea/);
  assert.doesNotMatch(elements.content.innerHTML,/<a\b/i);
  assert.doesNotMatch(elements.modes.innerHTML,/Jugend|Alle Medaillen/);
  elements.modes.handlers.click({target:{closest:()=>({dataset:{mode:"ostm"}})}});
  assert.match(elements.content.innerHTML,/team-heading/);
  elements.modes.handlers.click({target:{closest:()=>({dataset:{mode:"success"}})}});
  assert.equal(elements["year-control"].hidden,true);
  assert.match(elements.content.innerHTML,/2000/);
  assert.doesNotMatch(elements.content.innerHTML,/<a\b/i);
  elements.sport.handlers.change({target:{value:"skiOrienteering"}});
  assert.doesNotMatch(elements.content.innerHTML,/2000/);
});
