// M3 browser smoke test (PLAN M3): loads each scan page in headless Chrome (software WebGL), waits until every
// viewer reports data-status="ready", and fails on any console error, page error, failed request or HTTP error.
// It also checks that every asset the viewer loaded came back 200 with the formats.md media type, from the API
// (x-rb-asset-source: api) unless the page shows the offline badge.
//
//   node web/tests/smoke.mjs "http://localhost:3000" [screenshot dir]
//
// Prints one JSON line per route; exit 1 if any route is not ready or has errors. Chrome is found at CHROME_PATH,
// or at the usual macOS/Linux install paths. Not part of `npm test`: headless WebGL on CI runners is unverified,
// so it runs locally before every push (PLAN M3 tests).
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import puppeteer from "puppeteer-core";

const base = (process.argv[2] ?? "http://localhost:3000").replace(/\/+$/, "");
const shotDir = process.argv[3] ?? null;
const ROUTES = ["/scan/myel018-L1-d273", "/scan/myel018-L1-d0", "/scan/phantom"];
const MEDIA = { ".nii.gz": "application/gzip", ".mz3": "application/octet-stream", ".vtp": "application/xml", ".vti": "application/xml", ".stl": "model/stl", ".json": "application/json" };
const expectedType = (url) => Object.entries(MEDIA).find(([ext]) => new URL(url).pathname.endsWith(ext))?.[1];

const chrome =
  process.env.CHROME_PATH ||
  [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].find((p) => existsSync(p));
if (!chrome) {
  console.error("smoke: no Chrome found; set CHROME_PATH");
  process.exit(2);
}
if (shotDir) mkdirSync(shotDir, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: true,
  args: ["--use-angle=swiftshader", "--ignore-gpu-blocklist", "--window-size=1440,1100"],
});
let failed = 0;
try {
  for (const route of ROUTES) {
    const ctx = await browser.createBrowserContext();
    const page = await ctx.newPage();
    await page.setViewport({ width: 1440, height: 1100 });
    const errors = [];
    const assets = [];
    const sources = [];
    const okAssets = new Set();
    const cancelled = [];
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(`console: ${m.text().slice(0, 240)}`);
    });
    page.on("pageerror", (e) => errors.push(`pageerror: ${String(e).slice(0, 240)}`));
    page.on("requestfailed", (r) => {
      const why = r.failure()?.errorText ?? "";
      // NiiVue's streaming gunzip cancels its reader once it has the bytes it needs (ImageFactory.ts,
      // reader.cancel()), so a .nii.gz that already answered 200 can end as ERR_ABORTED. Not an error.
      if (why === "net::ERR_ABORTED" && okAssets.has(r.url())) cancelled.push(path.basename(new URL(r.url()).pathname));
      else errors.push(`requestfailed: ${r.url()} ${why}`);
    });
    page.on("response", (r) => {
      const url = r.url();
      if (r.status() >= 400) errors.push(`http ${r.status()}: ${url}`);
      if (new URL(url).pathname.startsWith("/assets/scans/")) {
        const want = expectedType(url);
        const got = (r.headers()["content-type"] ?? "").split(";")[0].trim();
        assets.push(`${path.basename(new URL(url).pathname)} ${r.status()} ${got}`);
        sources.push({ url, source: r.headers()["x-rb-asset-source"] ?? "" });
        if (r.status() === 200 && got === want) okAssets.add(url);
        if (r.status() !== 200 || got !== want) errors.push(`asset ${url}: ${r.status()} ${got} (want 200 ${want})`);
      }
    });
    const t0 = Date.now();
    let status = "timeout";
    try {
      await page.goto(base + route, { waitUntil: "load", timeout: 30_000 });
      await page.waitForFunction(
        () => {
          const v = [...document.querySelectorAll("[data-viewer]")];
          const loading = document.querySelectorAll('[data-status="loading"]').length;
          return v.length > 0 && loading === 0;
        },
        { timeout: 60_000 },
      );
      status = await page.$$eval("[data-viewer]", (els) => {
        const s = [...new Set(els.map((e) => e.getAttribute("data-status")))];
        return s.length === 1 ? s[0] : s.join(",");
      });
    } catch (e) {
      status = `timeout ${String(e).slice(0, 120)}`;
    }
    const viewers = await page
      .$$eval("[data-viewer]", (els) =>
        els.map((e) => ({ id: e.getAttribute("data-viewer"), layers: e.getAttribute("data-layers"), ms: Number(e.getAttribute("data-ms")) })),
      )
      .catch(() => []);
    const offline = await page.$('[data-testid="offline-badge"]').then((h) => h !== null).catch(() => false);
    // While the API answers, every asset must come from it: a broken API asset route must not hide behind the bundle.
    if (!offline) for (const s of sources) if (s.source !== "api") errors.push(`asset ${s.url}: source ${s.source || "none"} (want api)`);
    await new Promise((r) => setTimeout(r, 1200)); // let the 3D pane finish drawing before the screenshot
    if (shotDir) await page.screenshot({ path: path.join(shotDir, `${route.split("/").pop()}.png`), fullPage: true });
    const line = { route, status, wallMs: Date.now() - t0, offline, viewers, assets, cancelledAfter200: cancelled, errors };
    console.log(JSON.stringify(line));
    if (status !== "ready" || errors.length) failed++;
    await ctx.close();
  }
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
