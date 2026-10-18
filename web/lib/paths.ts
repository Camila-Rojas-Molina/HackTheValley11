import path from "node:path";

// Server-side locations. Each can be overridden by an environment variable:
// - RB_API_URL: the FastAPI base URL, without /api/v1. Unset means no API: the app reads its fixtures and the
//   demo bundle directly (scripts/dev.sh sets it only when this worktree's API runs, so another worktree's API
//   on a default port is never read; a deploy sets it explicitly);
// - RB_CONTRACTS_DIR: the contracts folder whose fixtures/ back the offline mode (same name the API reads);
// - RB_DEMO_DIR: the demo bundle (demo/<scan_id>/<asset name>, formats.md), served when the API cannot serve an asset.
// `next build`/`next start` run in web/, so the repo root is the parent of the working directory.
const repoRoot = path.resolve(process.cwd(), "..");

export function apiBase(): string | null {
  const url = process.env.RB_API_URL?.trim();
  return url ? url.replace(/\/+$/, "") : null;
}

export function contractsDir(): string {
  return process.env.RB_CONTRACTS_DIR || path.join(repoRoot, "contracts");
}

export function demoDir(): string {
  return process.env.RB_DEMO_DIR || path.join(repoRoot, "demo");
}
