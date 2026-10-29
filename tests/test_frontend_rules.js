"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  seasonYear,
  personIdString,
  compareResultCategories,
  regionalCategoryLabel,
  youthTitleCategory,
  youthTitleGrant,
} = require("../site/domain_rules.js");

test("calendar disciplines use January through December", () => {
  assert.equal(seasonYear("2025-01-01", "footOrienteering"), "2025");
  assert.equal(seasonYear("2025-12-31", "mountainbikeOrienteering"), "2025");
  assert.equal(seasonYear("2025-11-01", "trailOrienteering"), "2025");
});

test("youth grant ages require an explicit youth upper bound", () => {
  for (const name of ["Damen -12", "H-14", "Herren bis 16", "D 18",
    "D15-18", "Herren 13 bis 14", "Mixed Staffel bis 16"]) {
    assert.ok(youthTitleCategory(name), name);
  }
  for (const name of ["Damen -10", "Damen -20", "D 15-", "Damen ab 15",
    "Herren 21-", "Damen ab 165", "Offen"]) {
    assert.equal(youthTitleCategory(name), null, name);
  }
  assert.equal(youthTitleCategory("Wien Damen -14").gender, "D");
  assert.equal(youthTitleCategory("W H-14").gender, "H");
  assert.equal(youthTitleCategory("Damen+Herren -12 W").gender, null);
  assert.equal(youthTitleCategory("Nachwuchs -16").gender, null);
});

test("youth grants never guess the Wiener rate for mixed categories", () => {
  assert.equal(youthTitleGrant("om", null), 400);
  assert.equal(youthTitleGrant("wien", "D"), 110);
  assert.equal(youthTitleGrant("wien", "H"), 90);
  assert.equal(youthTitleGrant("wien", null), null);
});

test("Ski-O season runs from November through October", () => {
  assert.equal(seasonYear("2025-10-31", "skiOrienteering"), "2025");
  assert.equal(seasonYear("2025-11-01", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2025-12-31", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2026-01-01", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2026-10-31", "skiOrienteering"), "2026");
  assert.equal(seasonYear("2026-11-01", "skiOrienteering"), "2027");
});

test("missing dates do not create a season", () => {
  assert.equal(seasonYear("", "skiOrienteering"), "");
  assert.equal(seasonYear(null, "footOrienteering"), "");
});

test("64-bit fallback person IDs remain exact decimal text", () => {
  const fallbackId = "-2617108969934253528";
  assert.equal(personIdString(fallbackId), fallbackId);
  assert.equal(personIdString(9137), "9137");
  assert.equal(personIdString(Number(fallbackId)), null);
  assert.equal(personIdString("not-an-id"), null);
});

test("result categories sort by gender block and ascending age", () => {
  const categories = [
    "Rahmen Offen", "H 45", "D 55", "H -12", "D-14", "D-12", "H -14", "D 19",
  ];
  assert.deepEqual(categories.sort(compareResultCategories), [
    "D-12", "D-14", "D 19", "D 55",
    "H -12", "H -14", "H 45",
    "Rahmen Offen",
  ]);
});

test("regional categories use readable jurisdiction and age labels", () => {
  assert.equal(regionalCategoryLabel("NOE", "D 19"), "NÖ Damen 19-");
  assert.equal(regionalCategoryLabel("NOE", "D-12"), "NÖ Damen -12");
  assert.equal(regionalCategoryLabel("WIEN", "H 45"), "Wien Herren 45-");
  assert.equal(regionalCategoryLabel("BGLD", "DH -14"),
    "Burgenland Damen und Herren -14");
  assert.equal(regionalCategoryLabel("WIEN", "D 15", "Wien Damen 15-"), "Wien Damen 15-");
  assert.equal(regionalCategoryLabel("WIEN", "H 15", "Wien Herren ab 15"), "Wien Herren 15-");
  assert.equal(regionalCategoryLabel("WIEN", "D 14", "Wien Damen -14"), "Wien Damen -14");
});
