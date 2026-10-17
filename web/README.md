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
