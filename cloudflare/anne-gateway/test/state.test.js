import assert from "node:assert/strict";
import test from "node:test";

import {
  eligibilityCounts,
  identityCounts,
  stateWouldShrink,
  validateEligibilityState,
  validateIdentityState,
} from "../src/index.js";
import worker from "../src/index.js";


class MemoryR2 {
  constructor() { this.objects = new Map(); }

  async get(key) {
    const entry = this.objects.get(key);
    if (!entry) return null;
    return {
      arrayBuffer: async () => entry.body.slice(0),
      customMetadata: entry.customMetadata,
      httpMetadata: entry.httpMetadata,
      httpEtag: `"${key}"`,
    };
  }

  async put(key, body, options = {}) {
    const bytes = body instanceof ArrayBuffer
      ? body.slice(0)
      : new TextEncoder().encode(body).buffer;
    this.objects.set(key, {
      body: bytes,
      customMetadata: options.customMetadata || {},
      httpMetadata: options.httpMetadata || {},
      uploaded: new Date(),
    });
  }

  async list({ prefix }) {
    const objects = [...this.objects.entries()]
      .filter(([key]) => key.startsWith(prefix))
      .map(([key, entry]) => ({
        key,
        size: entry.body.byteLength,
        uploaded: entry.uploaded,
        customMetadata: entry.customMetadata,
      }));
    return { objects, truncated: false };
  }
}

function stateRequest(path, method = "GET", state) {
  return new Request(`https://gateway.test${path}`, {
    method,
    headers: {
      authorization: "Bearer test-token",
      ...(state === undefined ? {} : { "content-type": "application/json" }),
    },
    body: state === undefined ? undefined : JSON.stringify(state),
  });
}


test("validates the private eligibility ledger shape", () => {
  assert.equal(validateEligibilityState({ "1649": { "3929": true, "4000": null } }), true);
  assert.equal(validateEligibilityState({ "1649": { bad: true } }), false);
  assert.equal(validateEligibilityState({ "1649": { "3929": false } }), false);
  assert.equal(validateEligibilityState([]), false);
});

test("counts people and event-scoped decisions", () => {
  assert.deepEqual(
    eligibilityCounts({ "1": { "10": true, "11": null }, "2": { "10": "error" } }),
    { people: 2, decisions: 3 },
  );
});

test("validates a private ANNE identity snapshot without exposing it", () => {
  const snapshot = {
    schema_version: 1,
    fetched_at: "2026-07-18T12:00:00Z",
    users: [{ oefol_id: 9589, active_memberships: [] }],
  };
  assert.equal(validateIdentityState(snapshot), true);
  assert.deepEqual(identityCounts(snapshot), { people: 1 });
  assert.equal(validateIdentityState({ ...snapshot, users: [
    ...snapshot.users, { oefol_id: 9589, active_memberships: [] },
  ] }), false);
  assert.equal(validateIdentityState({ ...snapshot, users: [{}] }), false);
});

test("normal updates may grow but never shrink the ledger", () => {
  assert.equal(stateWouldShrink({ people: 2, decisions: 3 }, { people: 2, decisions: 4 }), false);
  assert.equal(stateWouldShrink({ people: 2, decisions: 3 }, { people: 1, decisions: 3 }), true);
  assert.equal(stateWouldShrink({ people: 2, decisions: 3 }, { people: 2, decisions: 2 }), true);
});

test("gateway snapshots updates, rejects shrink, and restores history", async () => {
  const env = { SYNC_GATEWAY_TOKEN: "test-token", PRIVATE_STATE: new MemoryR2() };
  const first = { "1": { "10": true } };
  const grown = { "1": { "10": true, "11": null }, "2": { "10": "error" } };

  let response = await worker.fetch(
    stateRequest("/state/eligibility", "PUT", first), env);
  assert.equal(response.status, 200);
  response = await worker.fetch(
    stateRequest("/state/eligibility", "PUT", grown), env);
  const update = await response.json();
  assert.equal(update.decisions, 3);
  assert.match(update.backupKey, /^eligibility\/history\//);

  response = await worker.fetch(
    stateRequest("/state/eligibility", "PUT", first), env);
  assert.equal(response.status, 409);

  response = await worker.fetch(stateRequest("/state/eligibility/history"), env);
  const history = await response.json();
  assert.equal(history.versions.length, 1);

  response = await worker.fetch(
    stateRequest("/state/eligibility/restore", "POST", { key: update.backupKey }), env);
  const restored = await response.json();
  assert.equal(restored.decisions, 1);
  assert.ok(restored.backupKey);
});

test("gateway stores and versions the private identity snapshot", async () => {
  const env = { SYNC_GATEWAY_TOKEN: "test-token", PRIVATE_STATE: new MemoryR2() };
  const first = {
    schema_version: 1,
    fetched_at: "2026-07-18T12:00:00Z",
    users: [{ oefol_id: 1, active_memberships: [] }],
  };
  const updated = {
    ...first,
    fetched_at: "2026-07-19T12:00:00Z",
    users: [...first.users, { oefol_id: 2, active_memberships: [] }],
  };
  let response = await worker.fetch(stateRequest("/state/identity", "PUT", first), env);
  assert.equal(response.status, 200);
  response = await worker.fetch(stateRequest("/state/identity", "PUT", updated), env);
  const saved = await response.json();
  assert.equal(saved.people, 2);
  assert.match(saved.backupKey, /^identity\/history\//);
  response = await worker.fetch(stateRequest("/state/identity"), env);
  const restored = await response.json();
  assert.equal(restored.users.length, 2);
});
