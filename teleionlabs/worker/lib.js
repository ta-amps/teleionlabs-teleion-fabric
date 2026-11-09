// teleionlabs.com: static pages plus small edge jobs.
//   1. www.teleionlabs.com and preview hosts answer 301 (GET/HEAD) or 308 (other methods) to the apex, path and query kept.
//   2. /products and /tessera redirect 301/308 to /fabric/; /film and /video redirect 301/308 to /video/; legacy overview paths redirect 301/308 to agora.eranosforgood.org.
//   3. Every response carries the security headers: a CSP of 'self' only, HSTS without preload, nosniff.
//   4. GET /api/status reads the public health checks listed in /status.sources.json (built from
//      content/home.json), keeps only state and version, and caches the answer for 60 s.
// No storage, no cookies, no credentials. Only GET and HEAD are served, except the form on a link page (/l/<token>).

import { BANNED_PATTERNS, PRIVATE_SHA256 } from "./banned.generated.js";
import { maybeMediaRange } from "./media-range.js";
import { networkResponse } from "./network.js";
import { linkResponse } from "./link.js";
import { TEXT_CSS_HASH } from "./text.generated.js";

export const APEX = "teleionlabs.com";
const OVERVIEW_HOST = "agora.eranosforgood.org";
const STATUS_TTL_SECONDS = 60;
const isTextFile = (path) => path.endsWith(".txt");
// Retired pages: their job moved to the home film and the visitor's agent.
const RETIRED = new Set(["/start", "/story", "/network", "/experience", "/experience.txt"]);
const OVERVIEW_PREFIXES = new Set([
  "judges", "how-it-works", "source", "library", "feeds", "scenarios",
  "models", "use-cases", "learning", "deploy", "studio", "accessibility",
  "decks", "storyboards", "evidence", "contribute", "hub-3d", "downloads",
]);
const PROBE_TIMEOUT_MS = 4000;
// Upstream hosts the status check may read. Anything else in the sources file is ignored.
const STATUS_HOSTS = new Set(["fabric.teleionlabs.com", "shim.teleionlabs.com", "eranosforgood.org"]);
// Service bindings, when configured, reach the other Workers directly instead of over the internet.
const STATUS_BINDINGS = { fabric: "TESSERA", shim: "HERMA", eranos: "ERANOS" };

export const SECURITY_HEADERS = {
  "Content-Security-Policy": [
    "default-src 'self'",
    "script-src 'self'",
    `style-src '${TEXT_CSS_HASH}'`,
    "img-src 'self'",
    "font-src 'self'",
    "media-src 'self'",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
  ].join("; "),
  "Strict-Transport-Security": "max-age=31536000",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), browsing-topics=()",
  "Cross-Origin-Opener-Policy": "same-origin",
};

function withHeaders(response, extra = {}) {
  const init = response.status === 206
    ? { status: response.status, statusText: response.statusText, headers: response.headers, encodeBody: "manual" }
    : response;
  const res = new Response(response.body, init);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.headers.set(k, v);
  for (const [k, v] of Object.entries(extra)) res.headers.set(k, v);
  return res;
}

// A cold read of a large film can hang for 47 s and then fail at one Cloudflare location. Ask again while the first
// read runs, take the first good answer, and answer 503 at the limit so the page's own retry can run.
export async function patientAsset(env, request, again = [6000, 14000], limit = 25000) {
  const asks = [];
  const ask = () => { const p = Promise.resolve().then(() => env.ASSETS.fetch(new Request(request))).then((r) => { if (r.status >= 500) throw new Error(`asset ${r.status}`); return r; }); asks.push(p); return p; };
  const timers = [];
  const answer = new Promise((resolve, reject) => {
    ask().then(resolve, () => {});
    let failed = 0;
    const watch = (p) => p.catch(() => { failed += 1; if (failed > again.length) reject(new Error("no asset")); });
    watch(asks[0]);
    for (const ms of again) timers.push(setTimeout(() => { const p = ask(); p.then(resolve, () => {}); watch(p); }, ms));
    timers.push(setTimeout(() => reject(new Error("asset timeout")), limit));
  });
  try {
    const winner = await answer;
    for (const p of asks) p.then((r) => { if (r !== winner) r.body?.cancel().catch(() => {}); }, () => {});
    return winner;
  } catch {
    for (const p of asks) p.then((r) => r.body?.cancel().catch(() => {}), () => {});
    return new Response("The film is warming up. Please try again.", { status: 503, headers: { "Retry-After": "2", "Content-Type": "text/plain; charset=utf-8" } });
  } finally {
    timers.forEach(clearTimeout);
  }
}

function cacheControlFor(pathname) {
  if (pathname.startsWith("/fonts/") || pathname.startsWith("/media/")) return "public, max-age=31536000, immutable";
  if (pathname.startsWith("/assets/") || pathname === "/favicon.svg" || pathname === "/favicon.ico") return "public, max-age=3600";
  return "public, max-age=300";
}

const BANNED = [];
const PRIVATE = new Set(PRIVATE_SHA256);
let wordListOk = BANNED_PATTERNS.length > 0 && PRIVATE.size > 0;
for (const p of BANNED_PATTERNS) {
  try {
    BANNED.push(new RegExp(p, "i"));
  } catch {
    wordListOk = false;
  }
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function passesWordList(value, tokenMemo) {
  if (!wordListOk || typeof value !== "string") return false;
  if (BANNED.some((re) => re.test(value))) return false;
  for (const token of value.toLowerCase().match(/[\p{L}\p{N}]+/gu) || []) {
    let allowed = tokenMemo?.get(token);
    if (allowed === undefined) {
      allowed = !PRIVATE.has(await sha256Hex(token));
      tokenMemo?.set(token, allowed);
    }
    if (!allowed) return false;
  }
  return true;
}

let sourcesMemo = null;
async function loadSources(env) {
  if (sourcesMemo) return sourcesMemo;
  const res = await env.ASSETS.fetch(new Request(`https://${APEX}/status.sources.json`));
  if (!res.ok) throw new Error(`status sources: ${res.status}`);
  const list = await res.json();
  sourcesMemo = (Array.isArray(list) ? list : []).filter((s) => {
    try {
      const u = new URL(s.url);
      return typeof s.id === "string" && u.protocol === "https:" && STATUS_HOSTS.has(u.hostname);
    } catch {
      return false;
    }
  });
  return sourcesMemo;
}

function healthRequest(url) {
  return new Request(url, {
    headers: { Accept: "application/json", "User-Agent": "teleionlabs-status/1" },
    signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
  });
}

async function get(env, source) {
  const binding = env[STATUS_BINDINGS[source.id]];
  if (binding && typeof binding.fetch === "function") {
    try {
      const res = await binding.fetch(healthRequest(source.url));
      if (res.ok) return res;
    } catch {
      // Fall through to the public address.
    }
  }
  return fetch(healthRequest(source.url));
}

async function probe(env, source) {
  try {
    const res = await get(env, source);
    if (!res.ok) return { id: source.id, state: "down" };
    const body = await res.json().catch(() => null);
    const out = { id: source.id, state: body && body.ok === true ? "up" : "down" };
    if (body && typeof body.version === "string" && /^[0-9A-Za-z.+-]{1,20}$/.test(body.version) && (await passesWordList(body.version))) {
      out.version = body.version;
    }
    return out;
  } catch {
    return { id: source.id, state: "down" };
  }
}

function forClient(res) {
  const out = new Response(res.body, res);
  out.headers.set("Cache-Control", "no-store");
  return out;
}

export async function statusResponse(request, env, ctx, now = () => new Date()) {
  const cache = globalThis.caches && globalThis.caches.default;
  const key = new Request(`https://${APEX}/api/status`, { method: "GET" });
  if (cache) {
    const hit = await cache.match(key);
    if (hit) return forClient(hit);
  }
  const sources = await loadSources(env);
  const services = await Promise.all(sources.map((s) => probe(env, s)));
  const body = JSON.stringify({ checked_at: now().toISOString(), services });
  const type = { "Content-Type": "application/json; charset=utf-8" };
  if (cache && ctx && typeof ctx.waitUntil === "function") {
    ctx.waitUntil(cache.put(key, new Response(body, { headers: { ...type, "Cache-Control": `public, max-age=${STATUS_TTL_SECONDS}` } })));
  }
  return new Response(body, { headers: { ...type, "Cache-Control": "no-store" } });
}

// Everything here except a one-use link page is public text, so a browser-based agent may read it from any site.
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Max-Age": "600",
};
// Addresses people and agents try here that live on another host. POST stays POST with 308.
function elsewhere(pathname) {
  if (pathname === "/mcp") return "https://fabric.teleionlabs.com";
  if (/^\/v1\/(chat|responses|messages|context|provisions|conversations|dry-run|insights|memory|outcomes|receipts|sync|admin)(\/|$)/.test(pathname)) return "https://shim.teleionlabs.com";
  if (pathname.startsWith("/v1/")) return "https://fabric.teleionlabs.com";
  if (pathname === "/q" || pathname.startsWith("/q/") || pathname === "/help") return "https://shim.teleionlabs.com";
  if (pathname === "/api/catalog" || pathname.startsWith("/api/catalog/")) return "https://eranosforgood.org";
  return null;
}
function plainNotFound(url, accept) {
  const path = url.pathname.length > 200 ? `${url.pathname.slice(0, 200)}...` : url.pathname;
  const next = { detail: `No page at ${path}.`, start: `https://${APEX}/llms.txt`, ways_to_connect: `https://${APEX}/docs/#ways` };
  if (accept.includes("json")) {
    return new Response(JSON.stringify({ ok: false, code: "not_found", ...next }), { status: 404, headers: { "Content-Type": "application/json; charset=utf-8" } });
  }
  return new Response(`${next.detail}\nStart at ${next.start}\nWays to connect: ${next.ways_to_connect}\n`, { status: 404, headers: { "Content-Type": "text/plain; charset=utf-8" } });
}

export async function handle(request, env, ctx) {
  const pathname = new URL(request.url).pathname;
  // /api/network keeps its own narrower browser rule, like /q/network on the shim.
  const isLinkPage = /^\/l\//.test(pathname) || pathname === "/api/network";
  if (request.method === "OPTIONS" && !isLinkPage) {
    return withHeaders(new Response(null, { status: 204, headers: CORS }));
  }
  const res = await serve(request, env, ctx);
  if (isLinkPage) return res;
  res.headers.set("Access-Control-Allow-Origin", "*");
  return res;
}

async function serve(request, env, ctx) {
  const url = new URL(request.url);
  const method = request.method.toUpperCase();
  const safe = method === "GET" || method === "HEAD";

  const versionPreview = /^[0-9a-f]{8}-teleionlabs\.ta-amps\.workers\.dev$/.test(url.hostname);
  if (!versionPreview && (url.hostname !== APEX || url.protocol !== "https:" || url.port !== "")) {
    const target = new URL(url.toString());
    target.protocol = "https:";
    target.hostname = APEX;
    target.port = "";
    target.hash = "";
    return withHeaders(new Response(null, { status: safe ? 301 : 308, headers: { Location: target.toString() } }));
  }

  // /products and /tessera redirect 301/308 to /fabric/ (Teleion Fabric public product naming).
  if (
    url.pathname === "/products" ||
    url.pathname.startsWith("/products/") ||
    url.pathname === "/tessera" ||
    url.pathname.startsWith("/tessera/")
  ) {
    const target = new URL(`https://${APEX}/fabric/`);
    target.search = url.search;
    return withHeaders(new Response(null, { status: safe ? 301 : 308, headers: { Location: target.toString() } }));
  }

  // The optional kit is a folder of plain files; its bare address opens the README.
  if (url.pathname === "/kit" || url.pathname === "/kit/") {
    return withHeaders(new Response(null, { status: safe ? 302 : 308, headers: { Location: `https://${APEX}/kit/README.txt` } }));
  }

  // /video (without slash) and legacy /film paths redirect 301/308 to the durable /video/ page.
  if (url.pathname === "/video" || url.pathname === "/film" || url.pathname.startsWith("/film/")) {
    const target = new URL(`https://${APEX}/video/`);
    target.search = url.search;
    return withHeaders(new Response(null, { status: safe ? 301 : 308, headers: { Location: target.toString() } }));
  }

  // Legacy overview paths move 301/308 to agora.eranosforgood.org (T9).
  const firstSeg = url.pathname.split("/").filter(Boolean)[0] || "";
  // /feeds/ and /models/ themselves are our own pages now; Agora subpages below them keep their redirect.
  const ownOverviewPage =
    url.pathname === "/feeds" ||
    url.pathname === "/feeds/" ||
    url.pathname === "/feeds/index.txt" ||
    url.pathname === "/models" ||
    url.pathname === "/models/" ||
    url.pathname === "/models/index.txt";
  if (OVERVIEW_PREFIXES.has(firstSeg) && !ownOverviewPage) {
    const path = url.pathname.split("/").filter(Boolean).length === 1 && !url.pathname.endsWith("/")
      ? `${url.pathname}/`
      : url.pathname;
    const target = new URL(`https://${OVERVIEW_HOST}${path}`);
    target.search = url.search;
    return withHeaders(new Response(null, { status: safe ? 301 : 308, headers: { Location: target.toString() } }));
  }

  const other = elsewhere(url.pathname);
  if (other) {
    const target = `${other}${url.pathname}${url.search}`;
    return withHeaders(new Response(safe ? `This address lives at ${target}\n` : null, { status: 308, headers: { Location: target, "Content-Type": "text/plain; charset=utf-8" } }));
  }

  const link = url.pathname.match(/^\/l\/([^/]*)\/?$/);
  if (link && (safe || method === "POST")) {
    const res = await linkResponse(request, env, link[1]);
    return method === "HEAD" ? new Response(null, res) : res;
  }

  const bare = url.pathname.replace(/\/$/, "");
  if (RETIRED.has(bare)) {
    return withHeaders(new Response(null, { status: safe ? 301 : 308, headers: { Location: `https://${APEX}/` } }));
  }

  if (!safe) {
    return withHeaders(new Response(null, { status: 405, headers: { Allow: "GET, HEAD" } }));
  }

  if (url.pathname === "/api/network") {
    return withHeaders(await networkResponse(request, env, ctx, passesWordList));
  }

  if (url.pathname === "/api/status") {
    const res = await statusResponse(request, env, ctx);
    const out = withHeaders(res);
    return method === "HEAD" ? new Response(null, out) : out;
  }

  const wantsText = !url.pathname.includes(".") && /^text\/(plain|markdown)/.test(request.headers.get("Accept") || "");
  const twin = wantsText ? (url.pathname === "/" ? "/index.txt" : `${url.pathname.replace(/\/$/, "")}.txt`) : null;
  const assetRequest = twin ? new Request(new URL(twin, url), request) : request;
  const servedPath = twin || url.pathname;
  const rawAsset = url.pathname.startsWith("/media/") ? await patientAsset(env, assetRequest) : await env.ASSETS.fetch(assetRequest);
  const accept = (request.headers.get("Accept") || "").toLowerCase();
  if (rawAsset.status === 404 && !accept.includes("text/html")) return withHeaders(plainNotFound(url, accept), { Vary: "Accept" });
  const asset = await maybeMediaRange(request, rawAsset, ctx);
  const extra = {};
  const mediaOk = asset.status === 200 || asset.status === 206;
  if (mediaOk && (url.pathname.startsWith("/fonts/") || url.pathname.startsWith("/media/") || !asset.headers.has("Cache-Control"))) {
    extra["Cache-Control"] = cacheControlFor(url.pathname);
  }
  if (isTextFile(servedPath) && asset.status === 200) extra["Content-Type"] = "text/plain; charset=utf-8";
  extra.Vary = "Accept";
  if (url.pathname.startsWith("/records/") && url.pathname.endsWith(".json") && asset.status === 200) {
    extra["Content-Type"] = "application/json; charset=utf-8";
  }
  if (url.pathname.startsWith("/media/") && mediaOk) {
    if (url.pathname.endsWith(".mp4")) extra["Content-Type"] = "video/mp4";
    else if (url.pathname.endsWith(".vtt")) extra["Content-Type"] = "text/vtt; charset=utf-8";
  }
  if (url.pathname.startsWith("/video-gallery/") && url.pathname.endsWith(".mp4") && mediaOk) extra["Content-Type"] = "video/mp4";
  // The owner's review gallery stays out of search. The World film brings its own inline styles and script, so only that folder relaxes those two lines.
  if (url.pathname.startsWith("/video-gallery")) extra["X-Robots-Tag"] = "noindex";
  if (url.pathname === "/video-gallery/") extra["Content-Security-Policy"] = SECURITY_HEADERS["Content-Security-Policy"].replace(`style-src '${TEXT_CSS_HASH}'`, `style-src 'self' '${TEXT_CSS_HASH}'`);
  if (url.pathname.startsWith("/video-gallery/world/") || url.pathname.startsWith("/video-gallery/claude-design/") || url.pathname.startsWith("/video-gallery/iso/") || url.pathname.startsWith("/video-gallery/mix/")) {
    extra["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'";
  }
  if ((asset.headers.get("Content-Type") || "").startsWith("text/html")) {
    const cc = extra["Cache-Control"] || asset.headers.get("Cache-Control") || "public, max-age=0, must-revalidate";
    extra["Cache-Control"] = /no-transform/.test(cc) ? cc : `${cc}, no-transform`;
  }
  if ((servedPath === "/status.txt" || servedPath === "/status/") && asset.status === 200) {
    return withHeaders(await withLiveStatus(asset, request, env, ctx, servedPath), { ...extra, "Cache-Control": "no-store, no-transform" });
  }
  return withHeaders(asset, extra);
}

// The status page is plain text, so the Worker writes the live answers into it.
const STATE_WORDS = { up: "Answering", down: "Not answering" };
async function withLiveStatus(asset, request, env, ctx, path) {
  let services = [];
  try {
    services = (await (await statusResponse(request, env, ctx)).json()).services || [];
  } catch {
    services = [];
  }
  const state = (id) => STATE_WORDS[services.find((s) => s.id === id)?.state] || "No answer from the check";
  let body = await asset.text();
  body = path === "/status.txt"
    ? body.replace(/\[\[([a-z]+)\]\]/g, (_, id) => state(id))
    : body.replace(/(<span data-svc="([a-z]+)">)[^<]*/g, (_, open, id) => open + state(id));
  const headers = new Headers(asset.headers);
  headers.delete("Content-Length");
  headers.delete("ETag");
  return new Response(body, { status: 200, headers });
}
