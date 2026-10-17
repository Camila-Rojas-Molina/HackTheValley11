// Runs against the production build (`npm run build` first): starts `next start` on a free port,
// fetches pages and checks what a visitor sees. No dependencies beyond Node 24.
import { spawn } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";
import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const labels = JSON.parse(
  readFileSync(path.join(webDir, "..", "contracts", "labels.json"), "utf8"),
);

const escapeHtml = (s) =>
  s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#x27;");

const visibleText = (html) =>
  html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
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

let server;
let base;
const serverLog = [];

before(async () => {
  assert.ok(
    existsSync(path.join(webDir, ".next", "BUILD_ID")),
    "no production build: run `npm run build` in web/ first",
  );
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  server = spawn(
    process.execPath,
    [path.join(webDir, "node_modules", "next", "dist", "bin", "next"), "start", "-p", String(port), "-H", "127.0.0.1"],
    { cwd: webDir, env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" }, stdio: ["ignore", "pipe", "pipe"] },
  );
  server.stdout.on("data", (d) => serverLog.push(String(d)));
  server.stderr.on("data", (d) => serverLog.push(String(d)));
  const deadline = Date.now() + 30_000;
  for (;;) {
    if (server.exitCode !== null) {
      throw new Error(`next start exited early (${server.exitCode}):\n${serverLog.join("")}`);
    }
    try {
      const res = await fetch(base + "/");
      if (res.ok) break;
    } catch {
      // not listening yet
    }
    if (Date.now() > deadline) {
      throw new Error(`next start not ready after 30 s:\n${serverLog.join("")}`);
    }
    await new Promise((r) => setTimeout(r, 250));
  }
});

after(() => {
  if (server && server.exitCode === null) server.kill("SIGTERM");
});

describe("placeholder page", () => {
  test("GET / renders the status line and the TCIA attribution from labels.json", async () => {
    const res = await fetch(base + "/");
    assert.equal(res.status, 200);
    const html = await res.text();
    assert.ok(html.includes(escapeHtml(labels.status_line)), "status line missing");
    assert.ok(html.includes(escapeHtml(labels.attribution.short)), "attribution missing");
    assert.match(html, /<title>RainbowBone<\/title>/);
  });

  test("an unknown path (404) still carries the status line and the attribution", async () => {
    const res = await fetch(base + "/no-such-page");
    assert.equal(res.status, 404);
    const html = await res.text();
    assert.ok(html.includes(escapeHtml(labels.status_line)), "status line missing on 404");
    assert.ok(html.includes(escapeHtml(labels.attribution.short)), "attribution missing on 404");
  });

  test("rendered text uses no banned claim (labels.json wording.banned_claims)", async () => {
    for (const route of ["/", "/no-such-page"]) {
      const text = visibleText(await (await fetch(base + route)).text());
      for (const pattern of labels.wording.banned_claims) {
        const m = text.match(new RegExp(pattern, "i"));
        assert.equal(m, null, `${route}: banned wording ${JSON.stringify(m?.[0])} (pattern ${pattern})`);
      }
    }
  });
});

describe("single source of wording", () => {
  // The status line and the attribution must be read from labels.json, never pasted into source.
  const sourceDirs = ["app", "components", "lib"];
  const files = [];
  const walk = (dir) => {
    if (!existsSync(dir)) return;
    for (const name of readdirSync(dir)) {
      const p = path.join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (/\.(tsx?|jsx?|mjs|css|md)$/.test(name)) files.push(p);
    }
  };
  for (const d of sourceDirs) walk(path.join(webDir, d));

  test("no web source file contains the status line or the attribution text", () => {
    assert.ok(files.length > 0);
    for (const f of files) {
      const src = readFileSync(f, "utf8");
      for (const s of [labels.status_line, labels.attribution.short]) {
        assert.ok(!src.includes(s), `${path.relative(webDir, f)} hardcodes ${JSON.stringify(s)}`);
      }
    }
  });
});
