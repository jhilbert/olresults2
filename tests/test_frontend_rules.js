"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { seasonYear } = require("../site/domain_rules.js");

test("calendar disciplines use January through December", () => {
  assert.equal(seasonYear("2025-01-01", "footOrienteering"), "2025");
  assert.equal(seasonYear("2025-12-31", "mountainbikeOrienteering"), "2025");
  assert.equal(seasonYear("2025-11-01", "trailOrienteering"), "2025");
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
