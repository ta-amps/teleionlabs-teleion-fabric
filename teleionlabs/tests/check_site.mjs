// Browser checks for the text site, against a running copy of the Worker (wrangler dev or a deploy).
//   node tests/check_site.mjs --base http://127.0.0.1:8799
// For each page, at desktop and phone width, under the light and the dark colour scheme:
//   light   body paper rgb(245, 242, 234), color-scheme "only light", no dark element over 5% of the viewport
//   layout  no sideways scroll; the first line is for agents; one footer row of links plus one line
//   text    no stylesheet file, no font file, no images in the page
//   console no console errors, page errors or CSP violations
//   film    on the home page: the video plays muted, Sound works; on /video/: the main film plays
//   scripts from this site only; the only film script is /assets/home-film.js on the home and video pages
// Then axe per page and width: no serious or critical issue. Exit 1 on any failure.
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";

const require = createRequire(import.meta.url);
const PLAYWRIGHT = process.env.PLAYWRIGHT;
const AXE = process.env.AXE;
const { chromium } = require(PLAYWRIGHT);
const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const BASE = opt("--base", "http://127.0.0.1:8799").replace(/\/$/, "");
const PAGES = ["/", "/disclaimer/", "/fabric/", "/omninode/", "/shimming/", "/fabric-intelligence/", "/weight-space/", "/models/", "/feeds/", "/video/", "/explain/", "/explain/connect/", "/explain/task/", "/explain/search-and-reuse/", "/explain/omninode-shape/", "/explain/eranos/", "/benchmarks/", "/benchmarks/methodology/", "/docs/learning/", "/free/", "/docs/", "/status/", "/security/", "/privacy/", "/terms/", "/company/"];
const VIEWPORTS = [{ width: 1280, height: 720 }, { width: 390, height: 844 }];
const PAPER = "rgb(245, 242, 234)";
const failures = [];
const fail = (where, what) => failures.push(`${where}: ${what}`);

const browser = await chromium.launch({ executablePath: process.env.CHROME || "/usr/bin/google-chrome" });
const axeSource = await readFile(AXE, "utf8");
for (const viewport of VIEWPORTS) for (const colorScheme of ["light", "dark"]) {
  const ctx = await browser.newContext({ viewport, colorScheme });
  for (const path of PAGES) {
    const where = `${path} ${viewport.width} ${colorScheme}`;
    const page = await ctx.newPage();
    page.on("pageerror", (e) => fail(where, `page error ${e}`));
    page.on("console", (m) => { if (m.type() === "error" && !/api\/network|503/.test(m.text())) fail(where, `console ${m.text()}`); });
    const res = await page.goto(BASE + path, { waitUntil: "load" });
    if (!res.ok()) fail(where, `status ${res.status()}`);
    await page.waitForTimeout(300);
    const r = await page.evaluate(() => {
      const lum = (c) => { const m = c.match(/[\d.]+/g).map(Number); if (m.length > 3 && m[3] < 0.5) return 1; return (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255; };
      const area = innerWidth * innerHeight;
      const dark = [...document.querySelectorAll("body, body *")].filter((e) => { const b = e.getBoundingClientRect(); return e.tagName !== "VIDEO" && b.width * b.height > area * 0.05 && lum(getComputedStyle(e).backgroundColor) < 0.25; }).map((e) => e.tagName);
      return {
        bg: getComputedStyle(document.documentElement).backgroundColor,
        scheme: getComputedStyle(document.documentElement).colorScheme,
        dark, sideways: document.documentElement.scrollWidth > innerWidth,
        first: document.body.firstElementChild.textContent,
        sheets: document.querySelectorAll('link[rel="stylesheet"]').length,
        imgs: document.images.length,
        fonts: performance.getEntriesByType("resource").filter((e) => /\.woff2?$/.test(e.name)).length,
        footerRows: document.querySelectorAll("footer p").length,
      };
    });
    if (r.bg !== PAPER) fail(where, `background ${r.bg}`);
    if (!/only/.test(r.scheme) || !/light/.test(r.scheme) || /dark/.test(r.scheme)) fail(where, `color-scheme ${r.scheme}`);
    if (r.dark.length) fail(where, `dark element ${r.dark.join(",")}`);
    if (r.sideways) fail(where, "sideways scroll");
    if (!r.first.startsWith("Agent-first service")) fail(where, "first line isn't for agents");
    if (r.sheets || r.fonts || r.imgs) fail(where, `not text only: ${JSON.stringify([r.sheets, r.fonts, r.imgs])}`);
    if (r.footerRows !== 2) fail(where, `footer has ${r.footerRows} lines`);
    const scripts = await page.evaluate(() => [...document.scripts].map((s) => s.src).filter(Boolean).concat(performance.getEntriesByType("resource").filter((e) => /\.m?js(\?|$)/.test(e.name)).map((e) => e.name)));
    for (const src of scripts) {
      if (new URL(src).origin !== new URL(BASE).origin) fail(where, `script from another site ${src}`);
      if (/film3d|three/.test(src)) fail(where, `3D code ${src}`);
      if (/film/.test(src) && !((path === "/" || path === "/video/" || path.startsWith("/explain/")) && /\/assets\/home-film\.js$/.test(src))) fail(where, `film code on a text page ${src}`);
    }
    if (path === "/") {
      await page.waitForTimeout(2500);
      const v = await page.evaluate(() => { const e = document.querySelector("video#film"); return e && { t: e.currentTime, muted: e.muted, canvas: document.querySelectorAll("canvas").length }; });
      if (!v || !(v.t > 0) || !v.muted || v.canvas) fail(where, `home video ${JSON.stringify(v)}`);
      await page.click('[data-film="sound"]');
      if (await page.evaluate(() => document.querySelector("video#film").muted)) fail(where, "Sound did nothing");
    }
    if (path === "/video/") {
      await page.waitForTimeout(2500);
      const v = await page.evaluate(() => { const e = document.querySelector("video"); return e && { t: e.currentTime, src: e.currentSrc }; });
      if (!v || !(v.t > 0) || !/main-film/.test(v.src)) fail(where, `video page ${JSON.stringify(v)}`);
      await page.click('[data-film="sound"]');
      if (await page.evaluate(() => document.querySelector("video#film").muted)) fail(where, "Sound did nothing on /video/");
    }
    await page.close();
  }
  await ctx.close();
}
let axeRuns = 0;
for (const viewport of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport, bypassCSP: true });
  for (const path of PAGES) {
    const page = await ctx.newPage();
    await page.goto(BASE + path, { waitUntil: "load" });
    await page.addScriptTag({ content: axeSource });
    const out = await page.evaluate(async () => window.axe.run(document, { resultTypes: ["violations"] }));
    axeRuns++;
    for (const v of out.violations) if (["serious", "critical"].includes(v.impact)) fail(`${path} ${viewport.width} axe`, `${v.id}: ${v.nodes[0]?.target}`);
    await page.close();
  }
  await ctx.close();
}
await browser.close();
if (failures.length) { console.log(`FAIL ${failures.length}`); failures.forEach((f) => console.log(`  ${f}`)); process.exit(1); }
console.log(`PASS ${PAGES.length} pages, both widths, both colour schemes, ${axeRuns} axe runs`);
