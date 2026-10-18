# RainbowBone web

Next.js 16 (App Router, TypeScript, Tailwind 4) with shadcn/ui. Node 24 and npm 11; every dependency is pinned to an exact version (`.npmrc` sets `save-exact`).

Every visible label, disclaimer and badge is read from `../contracts/labels.json`, never typed into a component. The app imports it through the `@contracts/*` path alias, which is why `next.config.ts` sets the Turbopack root to the repo root.

```bash
cd web
npm ci
npm run dev          # http://localhost:3000
npm run lint
npm run typecheck    # next typegen, then tsc
npm run build
npm test             # needs a build: starts next start on a free port and checks the pages
```

The same steps run in `.github/workflows/s1-web.yml` and in the `web` suite of `suites/s1.toml`.

## Step 1 pages (M3)

- `/scan/<scan_id>` (`myel018-L1-d273`, `myel018-L1-d0`, `phantom`): NiiVue `0.69.0` 2×2 views, the metrics panel, the timeline and the AI slots. The page reads the API on the server; if the API cannot be reached it answers from `contracts/fixtures` and shows the "offline · fixtures" badge.
- `/assets/scans/<scan_id>/<name>`: the viewer's asset files. It asks the API first and serves the demo bundle when the API cannot.

| Variable | Default | Used for |
|---|---|---|
| `RB_API_URL` | unset: no API, fixtures and `demo/` only | the FastAPI base URL (without `/api/v1`), read on the server only |
| `RB_CONTRACTS_DIR` | `../contracts` | the fixtures behind the offline mode (same variable the API reads) |
| `RB_DEMO_DIR` | `../demo` | the demo bundle, `demo/<scan_id>/<asset name>` (`contracts/formats.md`) |

Browser smoke test (headless Chrome with software WebGL; Chrome from `CHROME_PATH` or the usual install path). It is not in CI, because headless WebGL on the runners is unverified, so run it locally before every push:

```bash
node web/tests/smoke.mjs "http://localhost:3000"            # one JSON line per route: "status":"ready", "errors":[]
node web/tests/smoke.mjs "http://localhost:3000" /tmp/shots # also saves a full-page screenshot per route
```
