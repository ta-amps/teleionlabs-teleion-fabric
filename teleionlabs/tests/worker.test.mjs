// Unit tests for the Worker (node --test). No network: fetch and the asset binding are stubbed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { handle, passesWordList, patientAsset, SECURITY_HEADERS } from "../worker/lib.js";
import entry from "../worker/index.js";

const DIST = fileURLToPath(new URL("../dist/", import.meta.url));
const SITE = JSON.parse(await readFile(new URL("../content/site.json", import.meta.url), "utf8"));
const FILM = SITE.main_film;
const POSTER = SITE.main_film_poster || FILM;

function assets() {
  return {
    async fetch(req) {
      const url = new URL(typeof req === "string" ? req : req.url);
      let path = url.pathname;
      if (path.endsWith("/")) path += "index.html";
      const type = path.endsWith(".html") ? { "Content-Type": "text/html; charset=utf-8" }
        : path.endsWith(".mp4") ? { "Content-Type": "video/mp4" } : {};
      try {
        const body = await readFile(DIST + path.slice(1));
        return new Response(body, { status: 200, headers: type });
      } catch {
        return new Response("not found", { status: 404, headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
    },
  };
}

const ctx = { waitUntil() {} };

test("the entry module exports only the default handler", () => {
  assert.equal(typeof entry.fetch, "function");
});

test("www answers 301 to the apex for GET, keeping path and query", async () => {
  const res = await handle(new Request("https://www.teleionlabs.com/free/?a=1#x"), { ASSETS: assets() }, ctx);
  assert.equal(res.status, 301);
  assert.equal(res.headers.get("Location"), "https://teleionlabs.com/free/?a=1");
  assert.equal(res.headers.get("Strict-Transport-Security"), "max-age=31536000");
});

test("www answers 308 for other methods", async () => {
  const res = await handle(new Request("https://www.teleionlabs.com/api/x", { method: "POST", body: "{}" }), { ASSETS: assets() }, ctx);
  assert.equal(res.status, 308);
  assert.equal(res.headers.get("Location"), "https://teleionlabs.com/api/x");
});

test("only GET and HEAD are served on the apex", async () => {
  const res = await handle(new Request("https://teleionlabs.com/", { method: "POST", body: "x" }), { ASSETS: assets() }, ctx);
  assert.equal(res.status, 405);
  assert.equal(res.headers.get("Allow"), "GET, HEAD");
});

test("pages carry the security headers, a CSP of 'self' only, and no alias headers on the apex", async () => {
  const res = await handle(new Request("https://teleionlabs.com/"), { ASSETS: assets() }, ctx);
  assert.equal(res.status, 200);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) assert.equal(res.headers.get(k), v, k);
  const csp = res.headers.get("Content-Security-Policy");
  assert.match(csp, /default-src 'self'/);
  assert.match(csp, /frame-ancestors 'none'/);
  assert.doesNotMatch(csp, /https?:|\*|unsafe/);
  assert.doesNotMatch(res.headers.get("Strict-Transport-Security"), /preload|includeSubDomains/);
  assert.equal(res.headers.get("X-Robots-Tag"), null);
});

test("the workers.dev alias redirects to the same path on the apex (one canonical host)", async () => {
  const res = await handle(new Request("https://teleionlabs.ta-amps.workers.dev/free/?a=1"), { ASSETS: assets() }, ctx);
  assert.equal(res.status, 301);
  assert.equal(res.headers.get("Location"), "https://teleionlabs.com/free/?a=1");
  assert.equal(res.headers.get("X-Robots-Tag"), null);
});

test("plain http answers 301 for GET and 308 otherwise, to https on the apex, with HSTS", async () => {
  const get = await handle(new Request("http://teleionlabs.com/start/?a=1"), { ASSETS: assets() }, ctx);
  assert.equal(get.status, 301);
  assert.equal(get.headers.get("Location"), "https://teleionlabs.com/start/?a=1");
  assert.equal(get.headers.get("Strict-Transport-Security"), "max-age=31536000");
  const post = await handle(new Request("http://teleionlabs.com/api/x", { method: "POST", body: "{}" }), { ASSETS: assets() }, ctx);
  assert.equal(post.status, 308);
  assert.equal(post.headers.get("Location"), "https://teleionlabs.com/api/x");
  const port = await handle(new Request("https://teleionlabs.com:8443/free/"), { ASSETS: assets() }, ctx);
  assert.equal(port.headers.get("Location"), "https://teleionlabs.com/free/", "a port is dropped");
});

test("the deployed config keeps what the Privacy page says: logs, traces and Issues off; https in local dev", async () => {
  const text = (await readFile(fileURLToPath(new URL("../wrangler.jsonc", import.meta.url)), "utf8")).replace(/^\s*\/\/.*$/gm, "");
  const config = JSON.parse(text);
  const o = config.observability || {};
  assert.equal(o.enabled, false, "observability");
  assert.equal(o.logs?.enabled, false, "logs");
  assert.equal(o.logs?.invocation_logs, false, "invocation logs");
  assert.equal(o.logs?.persist, false, "persisted logs");
  assert.equal(o.traces?.enabled, false, "traces");
  assert.equal(o.traces?.persist, false, "persisted traces");
  assert.equal(o.issues?.enabled, false, "real-time Issues set off explicitly, not left to the server default");
  assert.equal(config.dev?.upstream_protocol, "https", "local dev presents https URLs, so the redirect keeps it local");
});

test("/api/status reads only the listed health checks and keeps only state and version", async (t) => {
  const seen = [];
  t.mock.method(globalThis, "fetch", async (req) => {
    const url = typeof req === "string" ? req : req.url;
    seen.push(url);
    if (url.includes("fabric.")) return Response.json({ ok: true, version: "0.2.0", queues: [{ name: "secret-ish" }] });
    if (url.includes("shim.")) return Response.json({ ok: false });
    return new Response("boom", { status: 500 });
  });
  const res = await handle(new Request("https://teleionlabs.com/api/status"), { ASSETS: assets() }, ctx);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Cache-Control"), "no-store", "browsers never keep the status");
  const body = await res.json();
  assert.deepEqual(Object.keys(body).sort(), ["checked_at", "services"]);
  assert.deepEqual(body.services, [
    { id: "fabric", state: "up", version: "0.2.0" },
    { id: "shim", state: "down" },
    { id: "eranos", state: "down" },
  ]);
  assert.deepEqual(seen.sort(), [
    "https://eranosforgood.org/api/health",
    "https://fabric.teleionlabs.com/v1/health",
    "https://shim.teleionlabs.com/healthz",
  ]);
});

test("/api/status prefers a connected service binding and falls back when it fails", async (t) => {
  const publicCalls = [];
  t.mock.method(globalThis, "fetch", async (req) => {
    publicCalls.push(req.url);
    return Response.json({ ok: true, version: "9" });
  });
  const env = {
    ASSETS: assets(),
    TESSERA: { fetch: async () => Response.json({ ok: true, version: "0.2.0" }) },
    HERMA: { fetch: async () => new Response("not connected", { status: 503 }) },
    ERANOS: { fetch: async () => { throw new Error("no"); } },
  };
  const body = await (await handle(new Request("https://teleionlabs.com/api/status"), env, ctx)).json();
  assert.deepEqual(body.services.map((s) => s.state), ["up", "up", "up"]);
  assert.deepEqual(publicCalls.sort(), ["https://eranosforgood.org/api/health", "https://shim.teleionlabs.com/healthz"]);
});

test("a missing page answers with the 404 page's status", async () => {
  const res = await handle(new Request("https://teleionlabs.com/nope/"), { ASSETS: assets() }, ctx);
  assert.equal(res.status, 404);
});

test("/products and /tessera redirect 301 to /fabric/, /video and /film redirect 301 to /video/, and legacy Agora routes redirect 301 to agora.eranosforgood.org (T9)", async () => {
  for (const [from, to] of [
    ["/products?x=1", "https://teleionlabs.com/fabric/?x=1"],
    ["/tessera/", "https://teleionlabs.com/fabric/"],
    ["/tessera?a=2", "https://teleionlabs.com/fabric/?a=2"],
    ["/video?ref=1", "https://teleionlabs.com/video/?ref=1"],
    ["/film/", "https://teleionlabs.com/video/"],
    ["/film", "https://teleionlabs.com/video/"],
  ]) {
    const res = await handle(new Request(`https://teleionlabs.com${from}`), { ASSETS: assets() }, ctx);
    assert.equal(res.status, 301, from);
    assert.equal(res.headers.get("Location"), to, from);
  }
  for (const path of ["/judges/", "/models/legacy-subpage/"]) {
    const res = await handle(new Request(`https://teleionlabs.com${path}`), { ASSETS: assets() }, ctx);
    assert.equal(res.status, 301, path);
    assert.equal(res.headers.get("Location"), `https://agora.eranosforgood.org${path}`, path);
  }
});

test("/benchmarks/, /video/, /media/*.vtt, /records/*.json, /favicon.ico and og:image serve with expected MIME types (T3, T4)", async () => {
  const bench = await handle(new Request("https://teleionlabs.com/benchmarks/"), { ASSETS: assets() }, ctx);
  assert.equal(bench.status, 200);
  const vid = await handle(new Request("https://teleionlabs.com/video/"), { ASSETS: assets() }, ctx);
  assert.equal(vid.status, 200);
  assert.match(vid.headers.get("Content-Security-Policy"), /media-src 'self'/);
  const vtt = await handle(new Request(`https://teleionlabs.com/media/${FILM}.vtt`), { ASSETS: assets() }, ctx);
  assert.equal(vtt.status, 200);
  assert.equal(vtt.headers.get("Content-Type"), "text/vtt; charset=utf-8");
  assert.match(vtt.headers.get("Cache-Control"), /immutable/);
  const rec = await handle(new Request("https://teleionlabs.com/records/glimmer-30b-orchestrator-2026-11-02.json"), { ASSETS: assets() }, ctx);
  assert.equal(rec.status, 200);
  assert.equal(rec.headers.get("Content-Type"), "application/json; charset=utf-8");
  const parsed = await rec.json();
  assert.equal(parsed.metrics.routing_task_object_f1.rank_16_current_adapter, 0.978);
  const ico = await handle(new Request("https://teleionlabs.com/favicon.ico"), { ASSETS: assets() }, ctx);
  assert.equal(ico.status, 200);
  const og = await handle(new Request("https://teleionlabs.com/assets/og-teleion-1200x630.png"), { ASSETS: assets() }, ctx);
  assert.equal(og.status, 200);
});

test("agent-readable text files are served as plain text", async () => {
  for (const path of ["/llms.txt", "/robots.txt", "/.well-known/security.txt"]) {
    const res = await handle(new Request(`https://teleionlabs.com${path}`), { ASSETS: assets() }, ctx);
    assert.equal(res.status, 200, path);
    assert.equal(res.headers.get("Content-Type"), "text/plain; charset=utf-8", path);
  }
  const sec = await (await handle(new Request("https://teleionlabs.com/.well-known/security.txt"), { ASSETS: assets() }, ctx)).text();
  assert.match(sec, /^Contact: mailto:security@teleionlabs\.com$/m);
  const expires = new Date(sec.match(/^Expires: (.+)$/m)[1]);
  assert.ok(expires - Date.now() < 365 * 86400 * 1000, "Expires is less than a year away (RFC 9116)");
});

test("upstream strings pass the shared banned-words list before they leave the Worker (rule 16)", async (t) => {
  assert.equal(await passesWordList("0.2.0"), true);
  assert.equal(await passesWordList("Teleion"), true, "Teleion, with an i, is allowed");
  assert.equal(await passesWordList("abcdef0123456789"), false, "a 12+ character hex string is refused");
  t.mock.method(globalThis, "fetch", async () => Response.json({ ok: true, version: "abcdef0123456789" }));
  const body = await (await handle(new Request("https://teleionlabs.com/api/status"), { ASSETS: assets() }, ctx)).json();
  assert.ok(body.services.every((s) => s.state === "up" && !("version" in s)), "the refused version is dropped, the state stays");
});

test("HTML pages carry no-transform, so the zone injects nothing the CSP would block", async () => {
  for (const path of ["/", "/free/", "/nope/", "/status/", `/l/${"a".repeat(43)}`]) {
    const res = await handle(new Request(`https://teleionlabs.com${path}`, { headers: { Accept: "text/html" } }), { ASSETS: assets() }, ctx);
    assert.match(res.headers.get("Cache-Control"), /no-transform/, path);
  }
  const css = await handle(new Request("https://teleionlabs.com/assets/home-film.js"), { ASSETS: assets() }, ctx);
  assert.doesNotMatch(css.headers.get("Cache-Control") || "", /no-transform/);
});

test("/api/status: the Worker keeps a 60 s copy at the edge; browsers get no-store, also on a cache hit", async (t) => {
  const store = new Map();
  const pending = [];
  globalThis.caches = { default: {
    async match(req) { const r = store.get(req.url); return r ? r.clone() : undefined; },
    async put(req, res) { store.set(req.url, res); },
  } };
  t.after(() => { delete globalThis.caches; });
  t.mock.method(globalThis, "fetch", async () => Response.json({ ok: true, version: "1" }));
  const waitCtx = { waitUntil(p) { pending.push(p); } };
  const first = await handle(new Request("https://teleionlabs.com/api/status"), { ASSETS: assets() }, waitCtx);
  await Promise.all(pending);
  assert.equal(first.headers.get("Cache-Control"), "no-store");
  assert.equal(store.get("https://teleionlabs.com/api/status").headers.get("Cache-Control"), "public, max-age=60");
  const second = await handle(new Request("https://teleionlabs.com/api/status"), { ASSETS: assets() }, waitCtx);
  assert.equal(second.headers.get("Cache-Control"), "no-store");
  assert.deepEqual(await second.json(), await first.json());
});

test("/media/*.mp4 supports HTTP 206 byte-range seeking, 416 unsatisfiable ranges, and HEAD Accept-Ranges", async () => {
  for (const name of [`${FILM}.mp4`]) {
    const raw = await readFile(`${DIST}media/${name}`);
    const head = await handle(new Request(`https://teleionlabs.com/media/${name}`, { method: "HEAD", headers: {Range: "bytes=1-2"} }), { ASSETS: assets() }, ctx);
    assert.equal(head.status, 200, name);
    assert.equal(head.headers.get("Accept-Ranges"), "bytes", name);
    assert.equal(head.headers.get("Content-Type"), "video/mp4", name);

    const slice = await handle(
      new Request(`https://teleionlabs.com/media/${name}`, { headers: { Range: "bytes=100-611" } }),
      { ASSETS: assets() },
      ctx,
    );
    assert.equal(slice.status, 206, name);
    assert.equal(slice.headers.get("Accept-Ranges"), "bytes", name);
    assert.equal(slice.headers.get("Content-Range"), `bytes 100-611/${raw.byteLength}`, name);
    const bytes = new Uint8Array(await slice.arrayBuffer());
    assert.equal(bytes.byteLength, 512, name);
    assert.deepEqual(bytes, new Uint8Array(raw.subarray(100, 612)), name);

    const bad = await handle(
      new Request(`https://teleionlabs.com/media/${name}`, { headers: { Range: `bytes=${raw.byteLength + 10}-${raw.byteLength + 50}` } }),
      { ASSETS: assets() },
      ctx,
    );
    assert.equal(bad.status, 416, name);
    assert.equal(bad.headers.get("Content-Range"), `bytes */${raw.byteLength}`, name);
  }
});

test("every page is plain text: first line is for agents, no stylesheet file, no font, and a .txt copy", async () => {
  for (const [path, twin] of [["/", "/index.txt"], ["/free/", "/free.txt"], ["/docs/", "/docs.txt"], ["/models/", "/models.txt"], ["/models/", "/models/index.txt"]]) {
    const res = await handle(new Request(`https://teleionlabs.com${path}`), { ASSETS: assets() }, ctx);
    const body = await res.text();
    assert.match(body, /<body>\s*(?:<header>\s*)?<p>Agent-first service: teleionlabs\.com\/llms\.txt<\/p>/);
    assert.doesNotMatch(body, /rel="stylesheet"|\.woff2|class="/);
    const text = await handle(new Request(`https://teleionlabs.com${twin}`), { ASSETS: assets() }, ctx);
    assert.equal(text.status, 200);
    assert.match(text.headers.get("Content-Type"), /text\/plain/);
    assert.match(await text.text(), /Teleion Labs Incorporated/, `${twin} keeps the whole page`);
  }
});

test("the home page plays the rendered film as a video, keeps its words as text, and loads no canvas film", async () => {
  const res = await handle(new Request("https://teleionlabs.com/"), { ASSETS: assets() }, ctx);
  const body = await res.text();
  assert.match(body, new RegExp(`<video id="film" controls autoplay muted playsinline preload="metadata"[^>]* poster="/media/${POSTER}\\.jpg"`));
  assert.match(body, new RegExp(`<source src="/media/${FILM}\\.mp4" type="video/mp4">`));
  assert.match(body, new RegExp(`<track kind="captions" src="/media/${FILM}\\.vtt"(?![^>]*default)[^>]*>`));
  assert.match(body, /<script src="\/assets\/home-film\.js" defer>/);
  assert.doesNotMatch(body, /<canvas|\/assets\/film(3d)?\.js|film-words|data-seconds/);
  assert.doesNotMatch(body, /See it in 3D/i);
  assert.match(body, /Connect your agents today\./);
});

test("/video/ plays the main film with its captions off by default, and the old preview is gone", async () => {
  const body = await (await handle(new Request("https://teleionlabs.com/video/"), { ASSETS: assets() }, ctx)).text();
  assert.match(body, new RegExp(`<video id="film" controls autoplay muted playsinline[^>]* poster="/media/${POSTER}\\.jpg"`));
  assert.match(body, new RegExp(`<source src="/media/${FILM}\\.mp4"`));
  assert.match(body, new RegExp(`<track kind="captions" src="/media/${FILM}\\.vtt"(?![^>]*default)[^>]*>`));
  assert.doesNotMatch(body, /fabric-preview/);
  assert.match(body, /<button type="button" data-film="sound"[^>]* hidden>Turn sound on<\/button>/);
  assert.match(body, /<script src="\/assets\/home-film\.js" defer>/);
  const txt = await handle(new Request("https://teleionlabs.com/video.txt"), { ASSETS: assets() }, ctx);
  assert.equal(txt.status, 200);
  assert.match(await txt.text(), /Connect your agents today\./);
});

test("the footer keeps its short line, with the small beta note under it, in the page and its text copy", async () => {
  const body = await (await handle(new Request("https://teleionlabs.com/free/"), { ASSETS: assets() }, ctx)).text();
  assert.match(body, /<p>Teleion Labs Incorporated\. Free through December 31, 2026\.<\/p>\s*<div>Beta until 2027\. Open to everyone. Your experience may vary\.<\/div>\s*<\/footer>/);
  const txt = await (await handle(new Request("https://teleionlabs.com/free.txt"), { ASSETS: assets() }, ctx)).text();
  assert.match(txt, /Free through December 31, 2026\.\n+Beta until 2027\. Open to everyone. Your experience may vary\.\n$/);
});

test("the footer shows the required Muse Spark Open line and the notice is plain text at its address", async () => {
  const body = await (await handle(new Request("https://teleionlabs.com/free/"), { ASSETS: assets() }, ctx)).text();
  assert.match(body, /<a href="\/legal\/meta-models\.txt">Built with Muse Spark Open<\/a>/);
  const txt = await handle(new Request("https://teleionlabs.com/legal/meta-models.txt"), { ASSETS: assets() }, ctx);
  assert.equal(txt.status, 200);
  assert.match(txt.headers.get("content-type"), /^text\/plain/);
  assert.match(await txt.text(), /Built with Muse Spark Open/);
});

test("a slow or failed read of a film asks again, and a hopeless one answers 503 in time", async () => {
  const req = new Request("https://teleionlabs.com/media/x.mp4");
  let calls = 0;
  const hangs = { fetch: () => { calls += 1; return calls === 1 ? new Promise(() => {}) : Promise.resolve(new Response("ok")); } };
  assert.equal(await (await patientAsset({ ASSETS: hangs }, req, [5, 10], 500)).text(), "ok");
  calls = 0;
  const fails = { fetch: async () => { calls += 1; return calls === 1 ? new Response("x", { status: 500 }) : new Response("ok"); } };
  assert.equal(await (await patientAsset({ ASSETS: fails }, req, [5, 10], 500)).text(), "ok");
  const dead = { fetch: async () => new Response("x", { status: 500 }) };
  assert.equal((await patientAsset({ ASSETS: dead }, req, [1, 2], 500)).status, 503);
  const never = { fetch: () => new Promise(() => {}) };
  const t = Date.now();
  assert.equal((await patientAsset({ ASSETS: never }, req, [1, 2], 30)).status, 503);
  assert.ok(Date.now() - t < 400);
  const missing = { fetch: async () => new Response("no", { status: 404 }) };
  assert.equal((await patientAsset({ ASSETS: missing }, req, [5], 500)).status, 404);
});

test("the sitemap and the build drop the retired 3D page and its film code", async () => {
  const map = await (await handle(new Request("https://teleionlabs.com/sitemap.xml"), { ASSETS: assets() }, ctx)).text();
  assert.doesNotMatch(map, /experience/);
  for (const p of ["/assets/film.js", "/assets/film3d.js", "/assets/three-0.187.0.min.js"]) {
    assert.equal((await handle(new Request(`https://teleionlabs.com${p}`), { ASSETS: assets() }, ctx)).status, 404, p);
  }
});

test("retired pages redirect home", async () => {
  for (const p of ["/start/", "/story/", "/network/", "/start", "/experience/", "/experience", "/experience.txt"]) {
    const res = await handle(new Request(`https://teleionlabs.com${p}`), { ASSETS: assets() }, ctx);
    assert.equal(res.status, 301);
    assert.equal(res.headers.get("Location"), "https://teleionlabs.com/");
  }
});

test("a link page reads without acting, posts one answer, and sends the token only in a header", async () => {
  const token = "a".repeat(43);
  const calls = [];
  const TESSERA = { fetch: async (req) => {
    calls.push({ method: req.method, url: req.url, token: req.headers.get("X-Human-Link-Token"), body: req.method === "POST" ? await req.json() : null });
    return new Response(JSON.stringify(req.method === "POST"
      ? { state: "confirmed" }
      : { state: "open", kind: "confirm", text: "Add <40> Tasks", agent_label: "build-bot", expires_at: new Date(Date.now() + 600000).toISOString() }));
  } };
  const page = await handle(new Request(`https://teleionlabs.com/l/${token}`), { ASSETS: assets(), TESSERA }, ctx);
  const html = await page.text();
  assert.match(html, /Add &lt;40&gt; Tasks/);
  assert.match(html, /<form method="post">/);
  assert.match(page.headers.get("Cache-Control"), /no-store/);
  assert.equal(page.headers.get("Referrer-Policy"), "no-referrer");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "GET");
  assert.ok(!calls[0].url.includes(token));
  const post = await handle(new Request(`https://teleionlabs.com/l/${token}`, { method: "POST", body: new URLSearchParams({ answer: "confirm" }) }), { ASSETS: assets(), TESSERA }, ctx);
  assert.match(await post.text(), /Your agent has been told/);
  assert.deepEqual(calls[1].body, { answer: "confirm" });
  assert.equal(calls[1].token, token);
  const bad = await handle(new Request("https://teleionlabs.com/l/nope"), { ASSETS: assets(), TESSERA }, ctx);
  assert.match(await bad.text(), /This link has ended/);
  assert.equal(calls.length, 2);
});

test("a blank answer keeps the link open and says so", async () => {
  const token = "b".repeat(43);
  const open = { state: "open", kind: "ask", text: "Line one\nLine two", agent_label: "bot", expires_at: new Date(Date.now() + 600000).toISOString() };
  const TESSERA = { fetch: async () => new Response(JSON.stringify({ ...open, error: "value_required" }), { status: 400 }) };
  const res = await handle(new Request(`https://teleionlabs.com/l/${token}`, { method: "POST", body: new URLSearchParams({ answer: "send", value: "" }) }), { ASSETS: assets(), TESSERA }, ctx);
  const html = await res.text();
  assert.match(html, /Please type an answer/);
  assert.match(html, /<form method="post">/);
  assert.match(html, /Line one\nLine two/);
  assert.match(res.headers.get("Cache-Control"), /no-transform/);
});

test("links never touch once styling is gone", async () => {
  for (const path of ["/", "/video/", "/free/", "/docs/", "/fabric/", "/models/", "/company/", "/benchmarks/"]) {
    const res = await handle(new Request(`https://teleionlabs.com${path}`), { ASSETS: assets() }, ctx);
    assert.doesNotMatch(await res.text(), /<\/a><a\b/, path);
  }
});

test("/feeds/ and /models/ are our own pages; Agora subpages below them keep their redirect", async () => {
  const ctx = { waitUntil() {} };
  for (const root of ["/feeds", "/models"]) {
    const own = await handle(new Request(`https://teleionlabs.com${root}/`), { ASSETS: assets() }, ctx);
    assert.equal(own.status, 200, `${root}/`);
    const idxTxt = await handle(new Request(`https://teleionlabs.com${root}/index.txt`), { ASSETS: assets() }, ctx);
    assert.equal(idxTxt.status, 200, `${root}/index.txt`);
    assert.match(idxTxt.headers.get("content-type"), /^text\/plain/);
    const bare = await handle(new Request(`https://teleionlabs.com${root}`), { ASSETS: assets() }, ctx);
    assert.ok(![301, 308].includes(bare.status) || !String(bare.headers.get("location")).includes("agora"), root);
  }
  const below = await handle(new Request("https://teleionlabs.com/feeds/agent-harnesses/"), { ASSETS: assets() }, ctx);
  assert.equal(below.status, 301);
  assert.equal(below.headers.get("location"), "https://agora.eranosforgood.org/feeds/agent-harnesses/");
  const belowModels = await handle(new Request("https://teleionlabs.com/models/subpage/"), { ASSETS: assets() }, ctx);
  assert.equal(belowModels.status, 301);
  assert.equal(belowModels.headers.get("location"), "https://agora.eranosforgood.org/models/subpage/");
});

// Agents with few abilities: a browser-based agent reads any public page, wrong-host addresses say where to go, a missing path answers in plain words.
test("a browser agent may read public pages from any site, but not a link page", async () => {
  const preflight = await handle(new Request("https://teleionlabs.com/llms.txt", { method: "OPTIONS" }), { ASSETS: { fetch: async () => new Response("x") } }, {});
  assert.equal(preflight.status, 204);
  assert.equal(preflight.headers.get("access-control-allow-origin"), "*");
  const page = await handle(new Request("https://teleionlabs.com/llms.txt"), { ASSETS: { fetch: async () => new Response("x") } }, {});
  assert.equal(page.headers.get("access-control-allow-origin"), "*");
  const redirected = await handle(new Request("https://www.teleionlabs.com/x"), { ASSETS: { fetch: async () => new Response("x") } }, {});
  assert.equal(redirected.headers.get("access-control-allow-origin"), "*");
});
test("addresses that live on another host are redirected, keeping POST", async () => {
  const env = { ASSETS: { fetch: async () => new Response("x") } };
  for (const [path, target] of [
    ["/mcp", "https://fabric.teleionlabs.com/mcp"],
    ["/v1/search?q=a", "https://fabric.teleionlabs.com/v1/search?q=a"],
    ["/v1/chat/completions", "https://shim.teleionlabs.com/v1/chat/completions"],
    ["/q/tasks?limit=1", "https://shim.teleionlabs.com/q/tasks?limit=1"],
    ["/api/catalog/KIT-GRANT-04", "https://eranosforgood.org/api/catalog/KIT-GRANT-04"],
  ]) {
    const get = await handle(new Request(`https://teleionlabs.com${path}`), env, {});
    assert.equal(get.status, 308, path);
    assert.equal(get.headers.get("location"), target);
    const post = await handle(new Request(`https://teleionlabs.com${path}`, { method: "POST", body: "{}" }), env, {});
    assert.equal(post.status, 308, path);
  }
});
test("a missing page answers in plain words unless a browser asked for HTML", async () => {
  const env = { ASSETS: { fetch: async () => new Response("<!doctype html>not found page", { status: 404, headers: { "Content-Type": "text/html" } }) } };
  const plain = await handle(new Request("https://teleionlabs.com/nope"), env, {});
  assert.equal(plain.status, 404);
  assert.match(plain.headers.get("content-type"), /text\/plain/);
  assert.match(await plain.text(), /llms\.txt/);
  const json = await handle(new Request("https://teleionlabs.com/nope", { headers: { Accept: "application/json" } }), env, {});
  assert.equal((await json.json()).code, "not_found");
  const page = await handle(new Request("https://teleionlabs.com/nope", { headers: { Accept: "text/html,*/*" } }), env, {});
  assert.match(page.headers.get("content-type"), /text\/html/);
});
