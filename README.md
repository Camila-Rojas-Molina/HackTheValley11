# RainbowBone

A web dashboard that turns a patient's CT scans into a 3D model of a vertebra weakened by cancer. It:

1. shows where the bone is strained under load, **inside** the bone where the lesion is;
2. estimates how much stiffness the vertebra lost since an earlier scan, and how much of that loss the lesion
   accounts for, as opposed to bone loss across the whole spine;
3. tracks the lesion's volume over time;
4. explains the change in plain English for the care team and for the patient.

> Research prototype on public, de-identified data. Not a medical device. Not for clinical use or diagnosis.

Estimates how much stiffness the vertebra lost since an earlier scan, and how much of that the lesion accounts
for. Adapted from CT rigidity analysis; not validated. **AI never computes the number**: physics does, and AI
finds the bone and explains the result ([ARCHITECTURE.md](ARCHITECTURE.md)).

Data: Nohel et al. 2026, Spinal-Multiple-Myeloma-SEG, TCIA, DOI 10.7937/k4qv-hh78, CC BY 4.0.

## Status

Hack the Valley 11 build, in milestones. **M1:** the module layout, frozen contracts, typed pipeline stubs, AI
interfaces with a template-only chain and per-stream CI. **M2:** database-backed routes, the rb_ migrations and
Tiger schema (tested on throwaway local databases only; Tiger Cloud after G1), the jobs queue and the audit chain.
**M3:** `/scan/<id>`, a 2×2 NiiVue view of the real Myel_018 L1 crops (Day 0 and Day 273 side by side), the
synthetic phantom with a mock strain field, an offline mode on the fixtures, and `demo/` with its attribution and provenance.
Segmentation, finite-element physics, live AI calls and deployment arrive after the team's Step 1 review (G1).
The StillHere code this repo held before is kept at the tag `stillhere-final`.

## Layout

| Path | What | Stream |
|---|---|---|
| `web/` | Next.js dashboard and 3D viewer (NiiVue) | S1 |
| `api/` | FastAPI: routes, jobs, audit chain, Tiger Data queries | S4 |
| `api/app/ai/` | AI layer: one provider interface, fallback chain, number checker | S5 |
| `pipeline/` | `rb_pipeline`: ingest, Bone Finder masks, calibration, physics, export | S2, S3 |
| `worker/` | the job worker, a separate process | S4 |
| `deploy/` | Dockerfiles, compose, Caddy | S6 |
| `contracts/` | C1-C7: API, file formats, DB, AI interface, jobs, labels, pipeline | S0 |
| `demo/` | the curated demo bundle (real TCIA crops plus labelled synthetic files) | S2 |
| `suites/`, `scripts/`, `tests/repo/` | test registration, tooling, repo-wide tests | S0 + each stream |

Who may edit what is in [contracts/ownership.json](contracts/ownership.json).

## Quick start (macOS or Linux, Python 3.12, Node 24)

```sh
scripts/bootstrap.sh                                  # this worktree's .venv, from python3.12
scripts/test_all.sh all                               # every registered suite: PASS/FAIL/SKIP table
.venv/bin/python scripts/check_ownership.py --all     # every tracked file has one owner
.venv/bin/python scripts/build_contracts.py --check   # fixtures + openapi.json match api/app/schemas.py
.venv/bin/python api/scripts/run.py                   # the API (routes under /api/v1)
cd web && npm ci && npm run dev                       # the dashboard
scripts/dev.sh                                        # throwaway DB + API + web, detached; prints PIDs and ports
scripts/dev.sh stop                                   # stop them (also: api, web, status, stop-api, stop-db)
```

Large data (full CTs, model weights) never enters the repo: set `RB_DATA_DIR` to a folder outside it.
Secrets go in `api/.env` (never committed); `api/.env.example` lists every name, the AI ones included.

## How the team builds it

- **One worktree and one branch per stream** (`rb/s<N>[-topic]/<milestone>`), each with its own `.venv`, test
  database `rb_test_<hash>`, `.pgdata/` and ports (`.venv/bin/python scripts/rb_env.py`).
- **Contracts first.** Everyone builds against `contracts/`; only S0 changes a contract, as its own commit.
- **Each stream registers its suites** in `suites/<stream>.toml` and its CI in `.github/workflows/`.
- **Landing a push:** `scripts/integrate.sh <milestone>` checks ownership, squash-merges every stream branch,
  runs the full suite, and after review lands one commit on `main` (fast-forward only). Try it safely with
  `scripts/integrate.sh m1 --dry-run`. Each milestone has a DONE CHECK in `scripts/donecheck/`.
- **Status:** each worktree keeps a gitignored `.rb-status.md`; `scripts/watchdog.sh` summarises them all.
- **Wording:** every visible label, unit and disclaimer comes from
  [contracts/labels.json](contracts/labels.json); a lint enforces it over the docs and the web text.

## Credits and licences

- CT data and expert masks: Nohel M, Válek V, Rohan T, Štork M, Jakubíček R, Chmelík J, Dostál M (2026).
  Spinal-Multiple-Myeloma-SEG (Version 1). The Cancer Imaging Archive. DOI 10.7937/k4qv-hh78. CC BY 4.0.
- Bone Finder: TotalSegmentator, Wasserthal et al. 2023, DOI 10.1148/ryai.230024 (Apache-2.0).
