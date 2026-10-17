# RainbowBone architecture

> Research prototype on public, de-identified data. Not a medical device. Not for clinical use or diagnosis.

Data: Nohel et al. 2026, Spinal-Multiple-Myeloma-SEG, TCIA, DOI 10.7937/k4qv-hh78, CC BY 4.0.

## The system

```
Browser (web/, Next.js 16 + NiiVue 0.69)
  care-team list + "Flagged because" + feedback · 3D: spine context → L1, CT + lesion
  + compressive-strain volume overlay + cut-away · tiles: X / Y / background, ladder,
  AI-vs-expert mask · timeline (SIMULATED dashed) · AI card + provider badge + outage drill
  · patient view + reading grade · copilot drawer · Tiger panel · cost per analysis
        │ HTTPS <domain> (Caddy: /api/* → api, everything else → web)
        ▼
FastAPI (api/)  routes · admin auth · audit hash chain · job queue (lease) · Tiger queries
  └─ AI layer (api/app/ai/) ── (c) ONE Provider interface, ordered fallback:
       Gemini gemini-3.8-flash → Groq openai/gpt-oss-120b → Vultr qwen3.8-flash-next
       → local Qwen3-4B (HF weights, mlx_lm.server) → deterministic template
       (b) explanation: care-team summary · patient explanation · copilot (5 read-only tools)
       guardrail: numbers-in-facts check, number words, blocklist, length, fixed footer
       provenance: provider, model, latency, attempts, verdict, tokens, cost → ai_generation
        │ jobs (claimed with a lease, run outside any transaction)
        ▼
worker/ (separate process)
  (a) Bone Finder: TotalSegmentator (deep learning) — vertebra + vertebral-body masks
      CPU/mps; Vultr A16 GPU when available
  physics (NOT AI): full-scan calibration → HU→density→E → body-mask CTRA (X, Y, ladder)
      → voxel FE (PyAMG) → min/max principal strain, p99 in lesion region
  export: strain .nii.gz + .mz3 + context mesh + .vtp + .vti + .stl + metrics.json
        ▼
Tiger Data (new service): patients, scans, jobs, audit_log, alert_feedback (relational)
  hypertables scan_metrics · fe_element_results (~1.0M real rows) · ai_generation
  continuous aggregates (grouped by provenance): scan_metrics_monthly · fe_level_daily
  gapfill/locf at query time (filled=true) · columnstore stats · pg_database_size
Hosting: Vultr Compute vhf-4c-16gb (Toronto). Vultr A16 GPU (ewr/ord) for (a) and MedGemma.
```

**AI never computes the number. Physics does; AI finds the bone and explains the result.**

- X = the vertebra's total stiffness change since the earlier scan; Y = the points of X the lesion accounts for
  (the Day-0 bone put back into the lesion and re-run); background = the same measure on every level T11-L5.
- The AI layer only ever sees `Facts`: a small JSON of numbers our code computed. The checker rejects any number
  that is not in the facts, spelled-out quantities, blocked wording and over-length text; the template tier is
  always available and correct by construction.
- Until Galit's gate (G1) the pipeline stages are typed stubs that raise `NotImplementedError`, and `AI_CHAIN`
  is pinned to `template`.

## Contracts index

Streams build in parallel against these files. Only S0 changes them, as a separate commit announced in the chat;
snapshot tests in `tests/repo/contracts/` (and S4's `api/tests/`) catch silent drift.

| # | Contract | File(s) | Fixes |
|---|---|---|---|
| C1 | API | `api/app/schemas.py` → `contracts/openapi.json` (snapshot), golden payloads `contracts/fixtures/` (index: `contracts/fixtures/index.json`) | 18 routes under `/api/v1`, models with `extra="forbid"`; the web mock data is these fixtures |
| C2 | File formats | `contracts/formats.md` | grids, affines, asset names, media types, `.mz3`, colour scales, `metrics.json`, the demo bundle |
| C3 | DB schema and storage budget | `contracts/db.md` | tables, hypertables, continuous aggregates grouped by provenance, the 300 MiB budget |
| C4 | AI provider interface | `contracts/ai-interface.md` (+ `api/app/ai/providers/base.py`) | `Provider`, `Chain.run`, `check`, `template`, env names, the reading-grade formula |
| C5 | Jobs | `contracts/jobs.md` | kinds, payloads, lease semantics, timeouts, `seconds` and `cost_usd` |
| C6 | Labels and wording | `contracts/labels.json` | every tile label, unit, badge, disclaimer, the flag rule, the protocol line, banned claims |
| C7 | Pipeline functions | `contracts/pipeline.md` + `pipeline/rb_pipeline/*/__init__.py` | typed signatures and shared types of every stage |
| — | Ownership | `contracts/ownership.json` | which stream owns which path (`scripts/check_ownership.py`) |

Two provenance ideas are kept apart: `provenance` is where data came from (`real_scan`, `synthetic_insilico`,
`synthetic_metric_interp`; any synthetic value shows SIMULATED), and `basis` is how one number was obtained
(`measured`, `simulation`, `mock`, `pending`). There is no forecast or projection value anywhere.

## Streams and the build loop

| Stream | Owns |
|---|---|
| S0 integrator | contracts, `api/app/schemas.py`, README, ARCHITECTURE, `.gitignore`, `scripts/`, `tests/repo/`, `ci-core.yml` |
| S1 web | `web/` |
| S2 data and Bone Finder | `pipeline/rb_pipeline/{io,segment,longitudinal}/`, `demo/` |
| S3 physics | `pipeline/rb_pipeline/{physics,export}/` |
| S4 API, jobs, Tiger | `api/` except the AI layer and the schemas, `worker/` |
| S5 AI layer | `api/app/ai/`, `api/tests/ai/` |
| S6 deploy | `deploy/`, `.dockerignore` |

- Each stream registers its suites in `suites/<stream>.toml` (format: `scripts/test_all.py`) and its CI in
  `.github/workflows/<stream>-*.yml`; `scripts/test_all.sh all` runs every registration.
- Isolation per worktree: its own `.venv` (`scripts/bootstrap.sh`), test database `rb_test_<hash>`, PGDATA in
  `<worktree>/.pgdata/`, TimescaleDB container `rb-tsdb-<hash>` on a free port (`scripts/rb_env.py`).
- `scripts/integrate.sh <m>`: ownership check and squash-merge per stream (S0, S4, S5, S2, S3, S1, S6), the full
  suite, an adversarial review, then one commit on `main` (fast-forward only), CI watch and
  `scripts/donecheck/<m>.sh`. Streams continue with `git rebase --onto origin/main landed/S<N>/<m> <branch>`.
- `scripts/watchdog.sh` prints every worktree's last-commit age, `.rb-status.md` and last test result.
