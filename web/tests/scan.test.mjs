// M3 page tests for /scan/[id] and /assets/scans/... against the production build (`npm run build` first).
// A stub API (node:http) serves the golden fixtures from contracts/fixtures, so the tests need no Python, no
// database and no demo bundle; closing it makes the app fall back to the same fixtures ("offline · fixtures").
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const contracts = path.join(webDir, "..", "contracts");
const labels = JSON.parse(readFileSync(path.join(contracts, "labels.json"), "utf8"));
const index = JSON.parse(readFileSync(path.join(contracts, "fixtures", "index.json"), "utf8"));

// The server-rendered HTML a visitor sees (the RSC payload in <script> tags is not visible text).
const visibleText = (html) =>
  html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<template[\s\S]*?<\/template>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ");

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// ---- the stub API -------------------------------------------------------------------------------------------
const fixture = (p) => {
  const e = index.fixtures.find((f) => f.kind === "response" && f.method === "GET" && f.path === p && f.status < 300);
  return e ? JSON.parse(readFileSync(path.join(contracts, "fixtures", e.file), "utf8")) : null;
};
const stub = { assets: "501", hits: [] };
const api = createHttpServer((req, res) => {
  stub.hits.push(req.url);
  const send = (status, body, type = "application/json") => {
    res.writeHead(status, { "content-type": type });
    res.end(typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body));
  };
  if (/^\/api\/v1\/scans\/[^/]+\/assets\//.test(req.url)) {
    return stub.assets === "api"
      ? send(200, Buffer.from("FROM-API"), "application/gzip")
      : send(501, { detail: "lands in M3", available_in: "M3" });
  }
  const body = fixture(req.url); // the committed fixtures, unchanged
  return body ? send(200, body) : send(404, { detail: "unknown" });
});

// ---- the app --------------------------------------------------------------------------------------------------
let server;
let base;
let apiPort;
let demo;
const serverLog = [];

before(async () => {
  assert.ok(existsSync(path.join(webDir, ".next", "BUILD_ID")), "no production build: run `npm run build` in web/ first");
  apiPort = await freePort();
  await new Promise((r) => api.listen(apiPort, "127.0.0.1", r));
  // a tiny stand-in demo bundle, so the asset fallback is tested without demo/
  demo = mkdtempSync(path.join(tmpdir(), "rb-web-demo-"));
  mkdirSync(path.join(demo, "phantom"));
  writeFileSync(path.join(demo, "phantom", "strain_min_principal_ue.mock.mz3"), "FROM-BUNDLE");
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  server = spawn(
    process.execPath,
    [path.join(webDir, "node_modules", "next", "dist", "bin", "next"), "start", "-p", String(port), "-H", "127.0.0.1"],
    {
      cwd: webDir,
      env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1", RB_API_URL: `http://127.0.0.1:${apiPort}`, RB_DEMO_DIR: demo, RB_CONTRACTS_DIR: contracts },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  server.stdout.on("data", (d) => serverLog.push(String(d)));
  server.stderr.on("data", (d) => serverLog.push(String(d)));
  const deadline = Date.now() + 30_000;
  for (;;) {
    if (server.exitCode !== null) throw new Error(`next start exited early (${server.exitCode}):\n${serverLog.join("")}`);
    try {
      if ((await fetch(base + "/")).ok) break;
    } catch {
      // not listening yet
    }
    if (Date.now() > deadline) throw new Error(`next start not ready after 30 s:\n${serverLog.join("")}`);
    await new Promise((r) => setTimeout(r, 250));
  }
});

after(async () => {
  if (server && server.exitCode === null) server.kill("SIGTERM");
  if (api.listening) await new Promise((r) => api.close(r));
  if (demo) rmSync(demo, { recursive: true, force: true });
});

const page = async (route) => {
  const res = await fetch(base + route);
  return { status: res.status, html: await res.text() };
};
const tile = (key) => labels.tiles.find((t) => t.key === key);
const ROUTES = ["/scan/myel018-L1-d273", "/scan/myel018-L1-d0", "/scan/phantom"];

function assertStep1(html, route) {
  const text = visibleText(html);
  assert.ok(text.includes(labels.status_line), `${route}: status line`);
  assert.ok(text.includes(labels.attribution.short), `${route}: attribution`);
  assert.ok(text.includes(labels.copilot.drawer_label), `${route}: copilot drawer`);
  for (const q of labels.copilot.suggested_questions) assert.ok(text.includes(q), `${route}: question ${q}`);
  assert.match(html, /<details[^>]*data-disabled="true"/, `${route}: drawer disabled`);
  assert.equal((html.match(/<button[^>]*disabled=""/g) ?? []).length, 5, `${route}: 5 disabled questions`);
}

// The committed phantom fixture: its strain legend says MOCK (labels.json), and the caption about a simulated
// load is absent because the mock field comes from a fixed formula, not from a simulation (demo/ATTRIBUTION.md).
function assertPhantom(html) {
  const text = visibleText(html);
  assert.ok(text.includes(labels.viewer.phantom_label));
  const legend = html.match(/<figure[^>]*data-testid="strain-legend"[\s\S]*?<\/figure>/);
  assert.ok(legend, "the phantom renders the strain legend");
  const legendText = visibleText(legend[0]);
  assert.ok(legendText.includes(labels.viewer.strain_legend + labels.viewer.mock_suffix), "legend title");
  assert.ok(legendText.includes(labels.basis_badges.mock), "the legend carries the MOCK badge");
  assert.ok(!text.includes(labels.viewer.heatmap_caption), "no simulated-load caption on mock data");
  assert.ok(text.includes(labels.provenance_badges.synthetic_insilico));
  assert.ok(!text.includes(labels.viewer.segmentation_badge), "no expert-mask badge on the phantom");
}

// After M3 lands, the demo bundle is in the fixtures: no CT, mask or strain file and no tile waits on M3.
function assertNothingPendingM3(text, route) {
  const pendingM3 = labels.basis_badges.pending.replace("{available_in}", "M3");
  assert.ok(!text.includes(pendingM3), `${route}: renders ${JSON.stringify(pendingM3)}`);
}

// ---- tests ----------------------------------------------------------------------------------------------------
describe("scan page, API up", () => {
  test("Day 273: both scans side by side, lesion tile, pending stiffness, summary, copilot, no offline badge", async () => {
    const { status, html } = await page("/scan/myel018-L1-d273");
    assert.equal(status, 200);
    const text = visibleText(html);
    assert.match(html, /data-source="api"/);
    assert.ok(!text.includes(labels.viewer.offline_badge), "no offline badge while the API answers");
    assert.match(html, /data-panel="myel018-L1-d0"/);
    assert.match(html, /data-panel="myel018-L1-d273"/);
    assert.ok(html.indexOf('data-panel="myel018-L1-d0"') < html.indexOf('data-panel="myel018-L1-d273"'), "Day 0 on the left");
    assert.equal(text.split(labels.viewer.not_registered).length - 1, 2, "both panels say not yet registered (M5)");
    assert.ok(text.includes("Lesion volume 3.24 cm³ (original scan grid"), "Day 273 lesion volume names its grid");
    assert.ok(text.includes("Lesion volume 0.00 cm³ (original scan grid"), "Day 0 lesion volume");
    assert.ok(text.includes("L1 stiffness change since Day 0: pending FE (M5)"), "stiffness tile pending");
    assert.ok(text.includes(labels.viewer.lesion_outline_caption), "lesion caption");
    assert.ok(text.includes(labels.viewer.segmentation_badge), "segmentation badge");
    assert.ok(text.includes("template · 4/4 numbers match computed values"), "summary badge");
    assert.ok(text.includes(labels.ai.footer), "summary footer");
    assertStep1(html, "d273");
  });

  test("the timeline has solid real points linking to each scan, and the simulated legend", async () => {
    const { html } = await page("/scan/myel018-L1-d273");
    const text = visibleText(html);
    assert.match(html, /href="\/scan\/myel018-L1-d0"[^>]*>\s*<g data-point="myel018-L1-d0" data-simulated="false"/);
    assert.match(html, /href="\/scan\/myel018-L1-d273"[^>]*>\s*<g data-point="myel018-L1-d273" data-simulated="false"/);
    assert.match(html, /data-segment="measured"/);
    assert.ok(text.includes(`${labels.viewer.simulated_badge}: ${labels.viewer.timeline_legend}`), "legend");
  });

  test("phantom: SIMULATED, phantom label, strain legend with MOCK, no load-simulation caption on mock data", async () => {
    const { html } = await page("/scan/phantom");
    assertPhantom(html);
    assert.match(html, /data-source="api"/);
    assertStep1(html, "phantom");
  });

  test("no panel shows an asset or metric still pending M3", async () => {
    for (const route of ROUTES) assertNothingPendingM3(visibleText((await page(route)).html), route);
  });

  test("every tile title is a labels.json template; units and badges come from labels.json", async () => {
    for (const route of ROUTES) {
      const { html } = await page(route);
      for (const [, key, inner] of html.matchAll(/<article[^>]*data-tile="([^"]+)"[^>]*>\s*<h3[^>]*>([\s\S]*?)<\/h3>/g)) {
        const t = tile(key);
        assert.ok(t, `${route}: unknown tile ${key}`);
        const title = visibleText(inner).trim();
        const asRegex = (tpl) =>
          new RegExp("^" + tpl.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{\w+\}/g, ".+?") + "$");
        assert.ok([t.label, t.pending_label].filter(Boolean).some((tpl) => asRegex(tpl).test(title)),
          `${route}: tile ${key} title ${JSON.stringify(title)} is not from labels.json`);
      }
      const badges = new Set(Object.values(labels.basis_badges).map((b) => b.split(" (")[0]));
      for (const [, b] of html.matchAll(/data-testid="basis-badge"[^>]*>([^<]*)</g)) {
        assert.ok(badges.has(b.split(" (")[0]), `${route}: badge ${b}`);
      }
    }
  });

  test("rendered text uses no banned claim and none of labels.json never_show", async () => {
    for (const route of ROUTES) {
      const text = visibleText((await page(route)).html);
      for (const pattern of labels.wording.banned_claims) {
        const m = text.match(new RegExp(pattern, "i"));
        assert.equal(m, null, `${route}: banned wording ${JSON.stringify(m?.[0])} (pattern ${pattern})`);
      }
      for (const s of labels.wording.never_show) assert.ok(!text.toLowerCase().includes(s.toLowerCase()), `${route}: ${s}`);
    }
  });
});

describe("asset route", () => {
  test("falls back to the bundle when the API asset route answers 501, with the formats.md media type", async () => {
    stub.assets = "501";
    const res = await fetch(base + "/assets/scans/phantom/strain_min_principal_ue.mock.mz3");
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("content-type"), "application/octet-stream");
    assert.equal(res.headers.get("x-rb-asset-source"), "bundle");
    assert.equal(await res.text(), "FROM-BUNDLE");
  });

  test("uses the API when it serves the asset", async () => {
    stub.assets = "api";
    try {
      const res = await fetch(base + "/assets/scans/phantom/ct.nii.gz");
      assert.equal(res.status, 200);
      assert.equal(res.headers.get("content-type"), "application/gzip");
      assert.equal(res.headers.get("x-rb-asset-source"), "api");
      assert.equal(await res.text(), "FROM-API");
    } finally {
      stub.assets = "501";
    }
  });

  test("404 for names outside formats.md, traversal and missing files", async () => {
    for (const p of [
      "/assets/scans/phantom/..%2F..%2Fpackage.json",
      "/assets/scans/phantom/fields.mock.vtu",
      "/assets/scans/phantom/.hidden.nii.gz",
      "/assets/scans/Phantom/ct.nii.gz",
      "/assets/scans/phantom/missing.nii.gz",
    ]) {
      const res = await fetch(base + p);
      assert.equal(res.status, 404, p);
      await res.body?.cancel();
    }
  });
});

describe("scan page, API down", () => {
  before(async () => {
    await new Promise((r) => api.close(r));
    api.closeAllConnections?.();
  });

  test("the same pages render from the fixtures with the offline badge", async () => {
    for (const route of ROUTES) {
      const { status, html } = await page(route);
      assert.equal(status, 200, route);
      const text = visibleText(html);
      assert.match(html, /data-source="fixtures"/, route);
      assert.ok(text.includes(labels.viewer.offline_badge), `${route}: offline badge`);
      assertNothingPendingM3(text, route);
      assertStep1(html, route);
    }
    assertPhantom((await page("/scan/phantom")).html);
    const text = visibleText((await page("/scan/myel018-L1-d273")).html);
    assert.ok(text.includes("Lesion volume 3.24 cm³ (original scan grid"));
    assert.ok(text.includes("template · 4/4 numbers match computed values"));
  });

  test("assets still load from the bundle", async () => {
    const res = await fetch(base + "/assets/scans/phantom/strain_min_principal_ue.mock.mz3");
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("x-rb-asset-source"), "bundle");
    await res.body?.cancel();
  });
});
