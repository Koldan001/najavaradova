/**
 * najavaradova-api — serves the outage feed for the static front page.
 *
 *   GET  /outages.json        public. Returns the stored feed, edge-cached 5 min.
 *   GET  /outages.json?loc=1  same, but ALSO injects the caller's approximate
 *                             location (from Cloudflare's edge, city-level, no
 *                             browser permission prompt) into meta.user so the map
 *                             can centre on the visitor's city. The front end only
 *                             adds ?loc=1 after the visitor accepts the optional
 *                             "location / personalisation" category in the cookie
 *                             banner - without it no location is derived or sent.
 *                             This variant is returned uncached (private, no-store)
 *                             so one visitor's city can never be served to another.
 *   PUT  /outages.json   private. Body is the new feed JSON. Requires
 *                        "Authorization: Bearer <PUBLISH_TOKEN>". Called by
 *                        scripts/publish_web_data.py after a scrape.
 *
 * The Worker's own *.workers.dev address answers only callers that send
 * "x-feed-key: <FEED_KEY>" (see feedKeyOk below); everyone else gets a 404 there.
 *
 * Storage: a Workers KV namespace bound as DATA, holding one key, "outages.json".
 * Secrets: PUBLISH_TOKEN and FEED_KEY, set in the Worker's Variables and Secrets (encrypted).
 *
 * Source of truth for this file lives in the repo at cf/worker.js. The dashboard
 * editor copy must be kept in sync by hand — paste this whole file on change.
 */

const KEY = "outages.json";
const EMPTY = { meta: { city: "Srbija", count: 0 }, outages: [] };

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, PUT, OPTIONS",
  "access-control-allow-headers": "authorization, content-type",
  // api.najavaradova.rs is a data endpoint, not a page. Keep it and its 404s out of
  // search indexes so Google stops reporting "api.najavaradova.rs/ — Not found (404)".
  "x-robots-tag": "noindex",
};

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=300",
  ...CORS,
};

// The workers.dev address sits outside the zone's bot protection - that is why the GitHub
// runners that build the city pages read the feed there (api.najavaradova.rs answers them 403).
// Left open, it was a way to pull the feed with no limits at all, so it only serves callers
// holding FEED_KEY (the same value is the FEED_KEY repo secret the workflow sends). With the
// secret unset it serves no one. api.najavaradova.rs stays public: the front page reads it.
function feedKeyOk(request, env) {
  const given = request.headers.get("x-feed-key") || "";
  const expected = env.FEED_KEY || "";
  if (!expected || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.hostname.endsWith(".workers.dev") && !feedKeyOk(request, env)) {
      return new Response("Not found", { status: 404, headers: CORS });
    }

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }

    if (url.pathname !== "/outages.json") {
      return new Response("Not found", { status: 404, headers: CORS });
    }

    if (request.method === "GET") {
      const stored = await env.DATA.get(KEY);
      let data;
      try {
        data = stored ? JSON.parse(stored) : { ...EMPTY };
      } catch {
        data = { ...EMPTY };
      }

      // Only when the front end explicitly asks (?loc=1, set after the visitor accepts the
      // optional location category in the cookie banner) do we derive and return the coarse,
      // city-level geo estimate Cloudflare puts on request.cf. No flag -> no location touched.
      const wantLoc = url.searchParams.get("loc") === "1";
      const cf = request.cf || {};
      const lat = Number(cf.latitude);
      const lng = Number(cf.longitude);
      if (wantLoc && Number.isFinite(lat) && Number.isFinite(lng)) {
        data.meta = data.meta || {};
        data.meta.user = { lat, lng, city: cf.city || null };
      }

      // The plain feed is shared and edge-cacheable; the ?loc=1 response is personal to this
      // visitor, so it must never sit in a shared cache where another visitor could get it.
      const headers = wantLoc
        ? { ...JSON_HEADERS, "cache-control": "private, no-store" }
        : JSON_HEADERS;
      return new Response(JSON.stringify(data), { headers });
    }

    if (request.method === "PUT") {
      const expected = `Bearer ${env.PUBLISH_TOKEN}`;
      if (!env.PUBLISH_TOKEN || request.headers.get("authorization") !== expected) {
        return new Response("Forbidden", { status: 403, headers: CORS });
      }

      const text = await request.text();
      try {
        JSON.parse(text);
      } catch {
        return new Response("Body is not valid JSON", { status: 400, headers: CORS });
      }

      await env.DATA.put(KEY, text);
      return new Response("OK", { headers: CORS });
    }

    return new Response("Method not allowed", { status: 405, headers: CORS });
  },
};
