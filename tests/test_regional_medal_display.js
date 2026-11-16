"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const rules = require("../site/domain_rules.js");

test("regional medal colours require an awarded, eligible podium", () => {
  const source = fs.readFileSync(path.join(__dirname, "../site/app.js"), "utf8");
  const end = source.indexOf('window.addEventListener("hashchange", route);');
  assert.ok(end > 0);
  const context = vm.createContext({window:{OLRDomainRules:rules}, document:{getElementById:()=>({})},
    location:{search:""}, localStorage:{getItem:()=>null}, URLSearchParams});
  vm.runInContext(source.slice(0,end) + "\nglobalThis.tier = regionalMedalTier;", context);
  const result = {regional_eligibility_state:"eligible", status:"ok", out_of_competition:0, regional_rank:1};
  const category = {championship_starters:3, award_hold:null};
  assert.equal(context.tier(result, category, "WIEN"), "gold");
  assert.equal(context.tier({...result, regional_rank:2}, category, "WIEN"), "silver");
  assert.equal(context.tier({...result, regional_rank:3}, category, "WIEN"), "bronze");
  assert.equal(context.tier({...result, regional_rank:4}, category, "WIEN"), null);
  assert.equal(context.tier({...result, status:"dns"}, category, "WIEN"), null);
  assert.equal(context.tier({...result, out_of_competition:1}, category, "WIEN"), null);
  assert.equal(context.tier({...result, regional_eligibility_state:"unresolved"}, category, "WIEN"), null);
  assert.equal(context.tier(result, {...category, award_hold:"offen"}, "WIEN"), null);
  assert.equal(context.tier(result, {...category, championship_starters:2}, "WIEN"), null);
  assert.equal(context.tier(result, {...category, championship_starters:2}, "NOE"), "gold");
});
