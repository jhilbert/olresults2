"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const rules = require("../site/domain_rules.js");

test("detail routes activate their navigation section, never the external Wiener MS link", () => {
  const source = fs.readFileSync(path.join(__dirname, "../site/app.js"), "utf8");
  const bootIndex = source.indexOf('window.addEventListener("hashchange", route);');
  assert.ok(bootIndex > 0);
  const links = ["events", "clubs", "runners", undefined].map((nav) => ({
    dataset: nav ? {nav} : {},
    classList: {active: false, toggle(_name, active) { this.active = active; }},
  }));
  const context = vm.createContext({
    window: {OLRDomainRules: rules, scrollTo: () => {}},
    document: {getElementById: () => ({}), querySelectorAll: () => links},
    location: {search: "", hash: ""}, localStorage: {getItem: () => null}, URLSearchParams,
  });
  vm.runInContext(source.slice(0, bootIndex) + `
    db = {};
    viewEvent = viewRunner = viewClub = viewClubDns = () => {};
    globalThis.checkRoute = (hash) => { location.hash = hash; route(); };
    globalThis.clearActive = () => setActiveNav();`, context);
  for (const [hash, expected] of [
    ["#/event/4404", "events"], ["#/event/4404/lm/WIEN", "events"],
    ["#/runner/7646", "runners"], ["#/club/Naturfreunde%20Wien", "clubs"],
    ["#/club/Naturfreunde%20Wien/2025/youth", "clubs"],
    ["#/club/Naturfreunde%20Wien/2025/lm/WIEN", "clubs"],
    ["#/club/Naturfreunde%20Wien/dns", "clubs"],
  ]) {
    context.checkRoute(hash);
    assert.deepEqual(links.filter((link) => link.classList.active).map((link) => link.dataset.nav), [expected], hash);
  }
  context.clearActive();
  assert.equal(links.some((link) => link.classList.active), false);
});
