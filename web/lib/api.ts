// Server-side API client (C1). Every read goes to the FastAPI service first. If the service cannot be reached
// (a network error, a timeout, or a 502/503/504 from a proxy in front of it), the same request is answered
// from the golden fixtures in contracts/fixtures (index.json maps each request path to its file), and the
// result is marked `source: "fixtures"`, which the page shows as the "offline · fixtures" badge.
// A 404 or 501 from a reachable API is an answer, not an outage: it is returned as `missing`.
import { readFile } from "node:fs/promises";
import path from "node:path";
import { connection } from "next/server";
import { apiBase, contractsDir } from "./paths";

export const API_PREFIX = "/api/v1";
const TIMEOUT_MS = 2500;
// After a network failure, skip the API for this long, so one page render (up to five reads) does not wait
// for a timeout per read when the API host is down.
const DOWN_MS = 5000;
let apiDownUntil = 0;

export type Fetched<T> =
  | { ok: true; data: T; source: "api" | "fixtures" }
  | { ok: false; status: number; source: "api" | "fixtures" };

interface FixtureEntry {
  file: string;
  method: string;
  path: string | null;
  status: number;
  kind: string;
}

async function fixtureFor<T>(requestPath: string): Promise<T | null> {
  const dir = contractsDir();
  let index: { fixtures: FixtureEntry[] };
  try {
    index = JSON.parse(await readFile(path.join(dir, "fixtures", "index.json"), "utf8"));
  } catch {
    return null;
  }
  const entry = index.fixtures.find(
    (e) => e.kind === "response" && e.method === "GET" && e.path === requestPath && e.status >= 200 && e.status < 300,
  );
  if (!entry) return null;
  const file = path.resolve(dir, "fixtures", entry.file);
  if (!file.startsWith(path.resolve(dir, "fixtures") + path.sep)) return null;
  try {
    return JSON.parse(await readFile(file, "utf8")) as T;
  } catch {
    return null;
  }
}

/** GET `${API_PREFIX}${p}` (p starts with "/", query included), falling back to the fixtures when the API is down. */
export async function getJson<T>(p: string): Promise<Fetched<T>> {
  await connection(); // request-time data: nothing below runs during prerendering (it reads the clock)
  const requestPath = API_PREFIX + p;
  let res: Response | null = null;
  const base = apiBase(); // null: no API configured, read the fixtures
  if (base !== null && Date.now() >= apiDownUntil) {
    // Two tries: the first can fail on a reused keep-alive socket the server has just closed.
    for (let attempt = 0; attempt < 2 && res === null; attempt++) {
      try {
        res = await fetch(base + requestPath, {
          cache: "no-store",
          headers: { accept: "application/json" },
          signal: AbortSignal.timeout(TIMEOUT_MS),
        });
      } catch (e) {
        if (e instanceof DOMException && e.name === "TimeoutError") break; // do not wait twice
      }
    }
    // refused, reset twice, DNS failure or timeout: the API is unreachable
    if (res === null) apiDownUntil = Date.now() + DOWN_MS;
  }
  if (res && ![502, 503, 504].includes(res.status)) {
    if (!res.ok) return { ok: false, status: res.status, source: "api" };
    try {
      return { ok: true, data: (await res.json()) as T, source: "api" };
    } catch {
      return { ok: false, status: 502, source: "api" };
    }
  }
  const data = await fixtureFor<T>(requestPath);
  return data === null ? { ok: false, status: 404, source: "fixtures" } : { ok: true, data, source: "fixtures" };
}
