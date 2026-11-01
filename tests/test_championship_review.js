"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

test("championship source overview shows findings without confirming them", async () => {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {value: "all", innerHTML: "", textContent: "", addEventListener() {}});
    return nodes.get(id);
  };
  node("search").value = "";
  const sample = {classes: [{event_id: 4304, stage_id: 1, stage_date: "2024-06-15", event_title: "Test <Event>",
    category: "M-12", championship_type: "ÖM", jurisdiction: "AUT", source_categories: ["M-12"],
    invitation: {state: "category_conflict", urls: [], reviewed_url: "https://example.org/notice"},
    crosscheck: {state: "not_compared", source_url: null}, rule_findings: [],
    result: {row_count: 3, completeness: "not_confirmed", identity_open: 1,
      identity_applicable: 3, issues: [], source_urls: [], list_ids: ["list:one"]}}],
    open_source_claims: [], candidate_events_without_instance: []};
  const context = vm.createContext({document: {getElementById: node},
    fetch: async () => ({ok: true, json: async () => sample}), console});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../site/championship-review.js"), "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(node("events").innerHTML, /Widerspruch/);
  assert.doesNotMatch(node("events").innerHTML, /Vollständigkeit nicht manuell bestätigt/);
  assert.match(node("events").innerHTML, /review\.html\?event=4304&amp;list=list%3Aone/);
  assert.doesNotMatch(node("events").innerHTML, /Test <Event>/);
  node("state").value = "conflict";
  vm.runInContext("render()", context);
  assert.match(node("summary").textContent, /1 bekannte Quellenkonflikte/);
});
