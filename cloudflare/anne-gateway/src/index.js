const ANNE_ORIGIN = "https://anne-api.oefol.at";
const ELIGIBILITY_STATE_KEY = "eligibility/user_eligibility.json";
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

async function eligibilityState(request, env) {
  if (request.method === "GET") {
    const object = await env.PRIVATE_STATE.get(ELIGIBILITY_STATE_KEY);
    if (!object) return jsonResponse({ error: "eligibility state not initialized" }, 404);
    const headers = new Headers({
      "content-type": object.httpMetadata?.contentType || "application/json; charset=utf-8",
      "cache-control": "no-store",
      etag: object.httpEtag,
    });
    return new Response(object.body, { headers });
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
    await env.PRIVATE_STATE.put(ELIGIBILITY_STATE_KEY, body, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: { updatedAt: new Date().toISOString() },
    });
    return jsonResponse({ ok: true, people: Object.keys(state).length });
  }

  return jsonResponse({ error: "method not allowed" }, 405);
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
