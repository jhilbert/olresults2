"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const rules = require("../site/domain_rules.js");

// Exercise the actual renderer without booting sql.js or a browser.
const source = fs.readFileSync(path.join(__dirname, "../site/app.js"), "utf8");
const bootIndex = source.indexOf('window.addEventListener("hashchange", route);');
assert.ok(bootIndex > 0);
const context = vm.createContext({
  window: { OLRDomainRules: rules },
  document: { getElementById: () => ({}) },
  location: { search: "" },
  localStorage: { getItem: () => null },
  URLSearchParams,
});
vm.runInContext(source.slice(0, bootIndex) + "\nglobalThis.renderYouth = renderYouthTitleTable;", context);

test("youth table counts only evidenced winners and makes unknowns visible", () => {
  const base = { person_id: "1", person_name: "Anna", category: "D-14",
    category_full: "Damen -14", result_list_id: "list1", stage_id: 1,
    stage_number: 1, event_id: 1, event_title: "Wettkampf", date: "2025-01-01",
    national_rank: 1, status: "ok", out_of_competition: 0,
    identity_state: "resolved", championship: "ÖM",
    championship_eligibility_state: "eligible", championship_source_scope: "winner_only" };
  const regional = { ...base, regional_entry_id: "region1", detection_basis: "category",
    source_category: "D-14 Wien", source_category_full: "Damen -14 Wien" };
  const inferred = { ...base, person_id: "2", person_name: "Zweite", result_list_id: "list2",
    championship_source_scope: "inferred" };
  const mixed = { ...regional, person_id: "3", person_name: "Dritte", regional_entry_id: "region2",
    source_category: "DH-12 Wien", source_category_full: "Damen+Herren -12 Wien" };
  const notWinner = { ...base, person_id: "4", person_name: "Vierte", national_rank: 2 };
  const wrongAge = { ...base, person_id: "5", person_name: "Fünfte", category_full: "Damen -20" };
  const ooc = { ...base, person_id: "7", person_name: "Siebte", out_of_competition: 1 };
  const noEvidence = { ...regional, person_id: "6", person_name: "Sechste", regional_entry_id: "region3",
    detection_basis: "event_title" };
  const natOnly = { ...regional, person_id: "8", person_name: "Achte", regional_entry_id: "region4",
    detection_basis: "source_nat" };
  const html = context.renderYouth([base, inferred, notWinner, wrongAge, ooc], [regional, regional, mixed, noEvidence, natOnly]);
  assert.match(html, /Σ 1<\/small><\/th><th class="num">Wiener MS<br><small>Σ 2/);
  assert.match(html, /Σ 510 € \+ offen/);
  assert.match(html, /3 mögliche Jugendtitel noch prüfen/);
  assert.match(html, /ÖM-Kategorie nur abgeleitet/);
  assert.match(html, /Wiener Meisterschaftskategorie prüfen/);
  assert.doesNotMatch(html, /Vierte/);
  assert.doesNotMatch(html, /Fünfte/);
  assert.doesNotMatch(html, /Siebte/);
  assert.match(html, /Fördersatz offen/);
});

test("WOLV evidence requires matching event, date, person, club and time", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  assert.equal(source.events["4879"].titles.length, 10);
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const winner = { person_id: "8980", person_name: "Albert Imriska", category: "DH -12",
    source_category: "Wien Damen&Herren -12", source_category_full: "Wien Damen&Herren -12", regional_entry_id: "w1", detection_basis: "category",
    result_list_id: "list1", stage_number: 1, event_id: 4879, event_title: "Sprint MS",
    date: "2025-05-10", national_rank: 1, identity_state: "resolved", status: "ok",
    out_of_competition: 0, time_s: 532, club_name: "Naturfreunde Wien" };
  const matched = context.renderYouth([], [winner]);
  assert.match(matched, /WOLV-Meistertitel bestätigt \(D-12\)/);
  assert.match(matched, /Fördersatz 90 € bestätigt/);
  assert.match(matched, /Σ 90 €/);
  assert.doesNotMatch(matched, /Fördersatz offen/);
  for (const changed of [{ time_s: 533 }, { date: "2025-05-11" },
    { club_name: "Anderer Verein" }, { person_id: "1" }, { event_id: 1234 },
    { source_category: "andere Klasse" }]) {
    const html = context.renderYouth([], [{ ...winner, ...changed }]);
    assert.doesNotMatch(html, /WOLV-Meistertitel bestätigt/);
    assert.doesNotMatch(html, /Fördersatz 90 € bestätigt/);
    assert.doesNotMatch(html, /Σ 90 €/);
  }
});

test("WOLV night list confirms both winners in the pair but not unannotated ranks", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  assert.equal(source.events["4963"].titles.length, 7);
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const base = { category: "H-14", source_category_full: "H-14", detection_basis: "source_nat",
    regional_entry_id: "pair", result_list_id: "list1", stage_number: 1, event_id: 4963,
    event_title: "Nacht MS", date: "2025-10-11", national_rank: 1,
    identity_state: "resolved", status: "ok", result_kind: "pair", out_of_competition: 0,
    time_s: 1576, club_name: "WAT-OL" };
  const emil = { ...base, person_id: "10035", person_name: "Emil Wally" };
  const arthur = { ...base, person_id: "10052", person_name: "Arthur Wally" };
  const maria = { ...base, person_id: "8110", person_name: "Maria Beck", category: "D15-18",
    source_category_full: "D15-18", regional_entry_id: "maria", result_kind: "individual",
    time_s: 4047, club_name: "Naturfreunde Wien" };
  const html = context.renderYouth([], [emil, arthur, maria]);
  assert.match(html, /Wiener MS<br><small>Σ 2/);
  assert.match(html, /Σ 180 €/);
  assert.equal((html.match(/WOLV-Meistertitel bestätigt \(H-14\)/g) || []).length, 2);
  assert.match(html, /1 mögliche Jugendtitel noch prüfen/);
  assert.match(html, /WOLV nennt Rang 1, aber keinen Meistertitel/);
  assert.match(html, /WOLV: Rang 1, Titel nicht ausgewiesen/);
});

test("WOLV middle list establishes the mixed U12 winner's female title and grant", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/regional-title-evidence.json"), "utf8"));
  assert.equal(source.events["4847"].titles.length, 8);
  context.sourceFixture = source.events;
  vm.runInContext("regionalTitleEvidence = sourceFixture", context);
  const winner = { person_id: "10345", person_name: "Lihuen Le Blanc", category: "DH - 12",
    source_category: "Herren+Damen - 12W", source_category_full: "Herren+Damen - 12W",
    regional_entry_id: "mittel-u12", detection_basis: "category", result_kind: "individual",
    result_list_id: "list1", stage_number: 1, event_id: 4847, event_title: "Mittel MS",
    date: "2025-05-24", national_rank: 1, identity_state: "resolved", status: "ok",
    out_of_competition: 0, time_s: 2392, club_name: "Naturfreunde Wien" };
  const html = context.renderYouth([], [winner]);
  assert.match(html, /WOLV-Meistertitel bestätigt/);
  assert.match(html, /Σ 110 €/);
  assert.match(html, /Fördersatz 110 € bestätigt/);
  assert.doesNotMatch(html, /Fördersatz offen/);
  const other = context.renderYouth([], [{ ...winner, person_id: "2" }]);
  assert.doesNotMatch(other, /Σ 110 €/);
  assert.match(other, /Fördersatz offen/);
  const noTitle = context.renderYouth([], [{ ...winner, person_id: "8506",
    person_name: "Anna Skern", category: "D 13-14W", source_category: "Damen 13-14W",
    source_category_full: "Damen 13-14W", time_s: 1871 }]);
  assert.match(noTitle, /WOLV nennt Rang 1, aber keinen Meistertitel/);
  assert.doesNotMatch(noTitle, /Σ 110 €/);
});

test("ÖM invitation confirms only its listed event classes, not a person's eligibility", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/national-category-evidence.json"), "utf8"));
  context.nationalFixture = source.events;
  vm.runInContext("nationalCategoryEvidence = nationalFixture", context);
  const winner = { person_id: "8900", person_name: "Annina Urbanek", category: "Damen bis 14",
    category_full: "Damen bis 14", result_list_id: "list3", result_kind: "individual",
    stage_number: 1, event_id: 4830, event_title: "ÖM Mittel", date: "2025-09-21",
    national_rank: 1, identity_state: "resolved", status: "ok", out_of_competition: 0,
    championship: "ÖM", championship_eligibility_state: "eligible", championship_source_scope: "inferred" };
  assert.match(context.renderYouth([winner], []), /ÖM-Kategorie laut Ausschreibung/);
  assert.match(context.renderYouth([winner], []), /Σ 400 €/);
  for (const changed of [{ event_id: 4831 }, { date: "2025-09-22" }, { stage_number: 2 },
    { category: "Damen ab 15 bis 18", category_full: "Damen ab 15 bis 18" },
    { result_kind: "relay" }]) {
    const html = context.renderYouth([{ ...winner, ...changed }], []);
    assert.doesNotMatch(html, /ÖM-Kategorie laut Ausschreibung/);
    assert.doesNotMatch(html, /Σ 400 €/);
  }
  const unclearEligibility = context.renderYouth([{ ...winner, championship_eligibility_state: "provisional" }], []);
  assert.match(unclearEligibility, /ÖM-Berechtigung prüfen/);
  assert.doesNotMatch(unclearEligibility, /Σ 400 €/);
});

test("2026 ÖM sprint invitation confirms both U18 elite winners only on stage 2", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/national-category-evidence.json"), "utf8"));
  context.nationalFixture = source.events;
  vm.runInContext("nationalCategoryEvidence = nationalFixture", context);
  const base = { result_kind: "individual", stage_number: 2, event_id: 5278,
    event_title: "ÖM 3Tage-4Läufe", date: "2026-05-02", national_rank: 1,
    identity_state: "resolved", status: "ok", out_of_competition: 0,
    championship: "ÖM", championship_eligibility_state: "eligible",
    championship_source_scope: "inferred" };
  const mika = { ...base, person_id: "9209", person_name: "Mika Asenbauer",
    category: "D18-E", category_full: "Damen bis 18 Elite", result_list_id: "d18" };
  const lauri = { ...base, person_id: "8901", person_name: "Lauri Urbanek",
    category: "H18-E", category_full: "Herren bis 18 Elite", result_list_id: "h18" };
  const html = context.renderYouth([mika, lauri], []);
  assert.match(html, /Σ 800 €/);
  assert.equal((html.match(/ÖM-Kategorie laut Ausschreibung/g) || []).length, 2);
  assert.doesNotMatch(html, /noch prüfen/);
  for (const changed of [{ stage_number: 3 }, { date: "2026-05-03" },
    { category: "D15-18" }, { championship_eligibility_state: "provisional" },
    { identity_state: "candidate" }, { result_kind: "relay" }]) {
    const invalid = context.renderYouth([{ ...mika, ...changed }], []);
    assert.doesNotMatch(invalid, /Σ 400 €/);
  }
});

test("user-confirmed 2024 youth titles are exact and do not override other checks", () => {
  const source = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../site/data/youth-title-confirmations.json"), "utf8"));
  context.confirmationFixture = source.events;
  vm.runInContext("youthTitleConfirmations = confirmationFixture", context);
  const rows = Object.entries(source.events).flatMap(([eventId, event]) => event.entries.map((e) => ({
    ...e, event_id: Number(eventId), event_title: "ÖM", date: event.date,
    stage_number: event.stage_number, person_name: e.name, club_name: e.club,
    category_full: e.category, national_rank: 1, status: "ok", out_of_competition: 0,
    identity_state: "resolved", championship: "ÖM", championship_eligibility_state: "eligible",
    championship_source_scope: "inferred" })));
  assert.equal(rows.length, 9);
  const html = context.renderYouth(rows, []);
  assert.match(html, /Σ 3[.\s\u00a0]600 €/);
  assert.equal((html.match(/ÖM-Titel vom Nutzer bestätigt/g) || []).length, 9);
  assert.doesNotMatch(html, /noch prüfen/);
  const individual = rows[0], relay = rows.find((r) => r.result_kind === "relay");
  for (const changed of [{ event_id: 4000 }, { date: "2024-06-09" }, { stage_number: 2 },
    { person_id: "1" }, { club_name: "Anderer Verein" }, { category: "Herren bis 14" },
    { result_list_id: "other" }, { time_s: individual.time_s + 1 },
    { result_kind: "relay" }, { identity_state: "candidate" },
    { championship_eligibility_state: "unknown" }]) {
    assert.doesNotMatch(context.renderYouth([{ ...individual, ...changed }], []), /Σ 400 €/);
  }
  assert.doesNotMatch(context.renderYouth([{ ...relay, team_number: "999" }], []), /Σ 400 €/);
});
