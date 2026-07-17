const ANNE_ORIGIN = "https://anne-api.oefol.at";
const ELIGIBILITY_STATE_KEY = "eligibility/user_eligibility.json";
const ELIGIBILITY_HISTORY_PREFIX = "eligibility/history/";
const MAX_STATE_BYTES = 2 * 1024 * 1024;

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

function isAuthorized(request, env) {
  const token = env.SYNC_GATEWAY_TOKEN;
  return Boolean(token) && request.headers.get("authorization") === `Bearer ${token}`;
}

function isAllowedAnnePath(pathname) {
  return pathname === "/v1/event"
    || pathname === "/v1/club"
    || /^\/v1\/event\/\d+\/(results|stages|attachments)$/.test(pathname);
}

function anneHeaders(env) {
  const headers = new Headers({
    accept: "application/json",
    "user-agent": "olresults-sync/1.0 (+https://github.com/jhilbert/olresults2)",
  });
  if (env.ANNE_API_KEY) headers.set("x-api-key", env.ANNE_API_KEY);
  return headers;
}

async function proxyAnne(requestUrl, env) {
  if (!isAllowedAnnePath(requestUrl.pathname)) {
    return jsonResponse({ error: "ANNE path not allowed" }, 404);
  }
  const target = new URL(requestUrl.pathname + requestUrl.search, ANNE_ORIGIN);
  const upstream = await fetch(target, {
    method: "GET",
    headers: anneHeaders(env),
    redirect: "follow",
  });
  const headers = new Headers(upstream.headers);
  headers.set("cache-control", "no-store");
  headers.set("x-content-type-options", "nosniff");
  return new Response(upstream.body, { status: upstream.status, headers });
}

async function fetchEligibility(userId, env) {
  const upstream = await fetch(`${ANNE_ORIGIN}/v1/user/${userId}`, {
    method: "GET",
    headers: anneHeaders(env),
    redirect: "follow",
  });
  if (!upstream.ok) {
    return jsonResponse({ error: "ANNE eligibility lookup failed", status: upstream.status }, 502);
  }
  const user = await upstream.json();
  if (!Object.prototype.hasOwnProperty.call(user, "championshipEligibility")) {
    return jsonResponse({ error: "ANNE response has no championshipEligibility field" }, 502);
  }
  return jsonResponse({
    userId: Number(userId),
    championshipEligibility: user.championshipEligibility,
  });
}

function validateEligibilityState(value) {
  if (!value || Array.isArray(value) || typeof value !== "object") return false;
  return Object.entries(value).every(([userId, byEvent]) =>
    /^\d+$/.test(userId)
    && byEvent && !Array.isArray(byEvent) && typeof byEvent === "object"
    && Object.entries(byEvent).every(([eventId, eligibility]) =>
      /^\d+$/.test(eventId)
      && (eligibility === true || eligibility === null || eligibility === "error")));
}

function eligibilityCounts(state) {
  return {
    people: Object.keys(state).length,
    decisions: Object.values(state).reduce((sum, events) => sum + Object.keys(events).length, 0),
  };
}

function stateWouldShrink(current, incoming) {
  return incoming.people < current.people || incoming.decisions < current.decisions;
}

async function sha256Hex(body) {
  const digest = await crypto.subtle.digest("SHA-256", body);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function readStateObject(env, key) {
  const object = await env.PRIVATE_STATE.get(key);
  if (!object) return null;
  const body = await object.arrayBuffer();
  let state;
  try {
    state = JSON.parse(new TextDecoder().decode(body));
  } catch {
    throw new Error(`stored eligibility state is invalid JSON: ${key}`);
  }
  if (!validateEligibilityState(state)) {
    throw new Error(`stored eligibility state has invalid shape: ${key}`);
  }
  return { object, body, state, counts: eligibilityCounts(state), sha256: await sha256Hex(body) };
}

async function saveHistory(env, stored, reason) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const key = `${ELIGIBILITY_HISTORY_PREFIX}${stamp}-${stored.sha256.slice(0, 16)}.json`;
  await env.PRIVATE_STATE.put(key, stored.body, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: {
      savedAt: new Date().toISOString(),
      sha256: stored.sha256,
      people: String(stored.counts.people),
      decisions: String(stored.counts.decisions),
      reason,
    },
  });
  return key;
}

async function putCurrentState(env, body, state, reason) {
  const counts = eligibilityCounts(state);
  const sha256 = await sha256Hex(body);
  await env.PRIVATE_STATE.put(ELIGIBILITY_STATE_KEY, body, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: {
      updatedAt: new Date().toISOString(),
      sha256,
      people: String(counts.people),
      decisions: String(counts.decisions),
      reason,
    },
  });
  return { counts, sha256 };
}

async function eligibilityState(request, env) {
  if (request.method === "GET") {
    const stored = await readStateObject(env, ELIGIBILITY_STATE_KEY);
    if (!stored) return jsonResponse({ error: "eligibility state not initialized" }, 404);
    const headers = new Headers({
      "content-type": stored.object.httpMetadata?.contentType || "application/json; charset=utf-8",
      "cache-control": "no-store",
      etag: stored.object.httpEtag,
      "x-state-sha256": stored.sha256,
      "x-state-people": String(stored.counts.people),
      "x-state-decisions": String(stored.counts.decisions),
    });
    return new Response(stored.body, { headers });
  }

  if (request.method === "PUT") {
    const contentLength = Number(request.headers.get("content-length") || 0);
    if (contentLength > MAX_STATE_BYTES) return jsonResponse({ error: "state too large" }, 413);
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_STATE_BYTES) return jsonResponse({ error: "state too large" }, 413);
    let state;
    try {
      state = JSON.parse(new TextDecoder().decode(body));
    } catch {
      return jsonResponse({ error: "invalid JSON" }, 400);
    }
    if (!validateEligibilityState(state)) {
      return jsonResponse({ error: "invalid eligibility state shape" }, 400);
    }
    const current = await readStateObject(env, ELIGIBILITY_STATE_KEY);
    const incoming = eligibilityCounts(state);
    if (current && stateWouldShrink(current.counts, incoming)) {
      return jsonResponse({
        error: "eligibility state may not shrink during a normal push",
        current: current.counts,
        incoming,
      }, 409);
    }
    const incomingSha = await sha256Hex(body);
    if (current && incomingSha === current.sha256) {
      // The first deployment of versioning sees a legacy current object with
      // no checksum metadata.  Snapshot it once even when the JSON itself did
      // not change, then rewrite only the metadata-enriched current object.
      const needsInitialSnapshot = !current.object.customMetadata?.sha256;
      const backupKey = needsInitialSnapshot
        ? await saveHistory(env, current, "initial-versioned-snapshot") : null;
      if (needsInitialSnapshot) {
        await putCurrentState(env, body, state, "metadata-upgrade");
      }
      return jsonResponse({
        ok: true, unchanged: true, ...incoming, sha256: incomingSha, backupKey,
      });
    }
    const backupKey = current ? await saveHistory(env, current, "before-update") : null;
    const saved = await putCurrentState(env, body, state, "normal-update");
    return jsonResponse({ ok: true, ...saved.counts, sha256: saved.sha256, backupKey });
  }

  return jsonResponse({ error: "method not allowed" }, 405);
}

async function eligibilityHistory(request, env) {
  if (request.method !== "GET") return jsonResponse({ error: "method not allowed" }, 405);
  const listed = await env.PRIVATE_STATE.list({
    prefix: ELIGIBILITY_HISTORY_PREFIX,
    include: ["customMetadata"],
  });
  const versions = listed.objects
    .map((object) => ({
      key: object.key,
      size: object.size,
      uploaded: object.uploaded,
      ...object.customMetadata,
    }))
    .sort((a, b) => b.key.localeCompare(a.key));
  return jsonResponse({ versions, truncated: listed.truncated });
}

async function restoreEligibility(request, env) {
  if (request.method !== "POST") return jsonResponse({ error: "method not allowed" }, 405);
  let key;
  try {
    ({ key } = await request.json());
  } catch {
    return jsonResponse({ error: "invalid JSON" }, 400);
  }
  if (typeof key !== "string" || !key.startsWith(ELIGIBILITY_HISTORY_PREFIX)) {
    return jsonResponse({ error: "invalid history key" }, 400);
  }
  const historical = await readStateObject(env, key);
  if (!historical) return jsonResponse({ error: "history version not found" }, 404);
  const current = await readStateObject(env, ELIGIBILITY_STATE_KEY);
  const backupKey = current ? await saveHistory(env, current, "before-restore") : null;
  const saved = await putCurrentState(env, historical.body, historical.state, `restore:${key}`);
  return jsonResponse({
    ok: true,
    restoredFrom: key,
    backupKey,
    ...saved.counts,
    sha256: saved.sha256,
  });
}

export default {
  async fetch(request, env) {
    if (!isAuthorized(request, env)) return jsonResponse({ error: "unauthorized" }, 401);
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return jsonResponse({ ok: true });
    }
    if (url.pathname === "/state/eligibility") {
      return eligibilityState(request, env);
    }
    if (url.pathname === "/state/eligibility/history") {
      return eligibilityHistory(request, env);
    }
    if (url.pathname === "/state/eligibility/restore") {
      return restoreEligibility(request, env);
    }
    const eligibilityMatch = url.pathname.match(/^\/eligibility\/(\d+)$/);
    if (eligibilityMatch && request.method === "GET") {
      return fetchEligibility(eligibilityMatch[1], env);
    }
    if (url.pathname.startsWith("/v1/") && request.method === "GET") {
      return proxyAnne(url, env);
    }
    return jsonResponse({ error: "not found" }, 404);
  },
};

export { eligibilityCounts, stateWouldShrink, validateEligibilityState };
