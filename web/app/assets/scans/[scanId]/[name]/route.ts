// GET /assets/scans/<scan_id>/<name>: one scan asset for the viewer (formats.md names and media types).
// 1. The API's asset route (C1 GET /api/v1/scans/{id}/assets/{name}) when it answers 200.
// 2. Otherwise the same file from the demo bundle (RB_DEMO_DIR, default <repo>/demo), so the page still works
//    offline and before the API serves assets. The response says which with an x-rb-asset-source header.
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import path from "node:path";
import { Readable } from "node:stream";
import { API_PREFIX } from "@/lib/api";
import { isValidAssetRef, mediaType } from "@/lib/assets";
import { apiBase, demoDir } from "@/lib/paths";

const API_TIMEOUT_MS = 4000;

function notFound(): Response {
  return Response.json({ detail: "unknown asset" }, { status: 404 });
}

export async function GET(_req: Request, ctx: { params: Promise<{ scanId: string; name: string }> }) {
  const { scanId, name } = await ctx.params;
  if (!isValidAssetRef(scanId, name)) return notFound();
  const type = mediaType(name)!;
  const headers = { "content-type": type, "cache-control": "no-store", "x-content-type-options": "nosniff" };

  const base = apiBase(); // null: no API configured, serve the bundle
  if (base !== null) {
    // The timeout covers the wait for the response headers only, so a large file is never cut off mid-stream.
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), API_TIMEOUT_MS);
    try {
      const res = await fetch(`${base}${API_PREFIX}/scans/${scanId}/assets/${name}`, {
        cache: "no-store",
        signal: ac.signal,
      });
      clearTimeout(timer);
      if (res.ok && res.body) {
        return new Response(res.body, { headers: { ...headers, "x-rb-asset-source": "api" } });
      }
      await res.body?.cancel();
    } catch {
      clearTimeout(timer); // the API is unreachable: fall through to the bundle
    }
  }

  const root = path.resolve(demoDir());
  const file = path.resolve(root, scanId, name);
  if (!file.startsWith(root + path.sep)) return notFound();
  try {
    const st = await stat(file);
    if (!st.isFile()) return notFound();
    const body = Readable.toWeb(createReadStream(file)) as ReadableStream<Uint8Array>;
    return new Response(body, {
      headers: { ...headers, "content-length": String(st.size), "x-rb-asset-source": "bundle" },
    });
  } catch {
    return notFound();
  }
}
