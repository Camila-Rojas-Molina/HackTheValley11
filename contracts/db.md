# C3: database schema and storage budget

Push 2 implements this as `api/migrations/rb_001_core.sql`, `rb_002_timeseries.sql`, `rb_003_ai.sql` (S4), tested on throwaway local databases only. Applying it to Tiger Cloud waits for G1. Migrations run with `api/scripts/migrate.py` and a new advisory `LOCK_ID`.

## Rules
- **`provenance` ∈ {`real_scan`, `synthetic_insilico`, `synthetic_metric_interp`}** on every data row, as a `CHECK`. There is no `projection` value and no forecast row.
- A synthetic row must carry `generator_version` (`CHECK (provenance = 'real_scan' OR generator_version IS NOT NULL)`); the API also refuses one without it.
- **No column is called "risk".** No retention policy on any scan table; no ingest-age window; no refresh-window cap (the StillHere 30 d / 47 h / 7 d limits are gone).
- **Time column = demo calendar.** De-identified study dates are shifted (data.md §4), so the UI shows "Day N". `time` = `2026-01-17T00:00:00Z` + `day` days (Day 273 = 2026-10-17). Every payload carries both `day` and `time`.
- Every TimescaleDB statement is guarded by a `pg_extension` check; without TimescaleDB, hypertables are plain tables and continuous aggregates are plain views with the same columns (`date_trunc` instead of `time_bucket`). `/tiger/info` reports `backend: "postgres"` then.
- The audit trigger function is **`rb_audit_guard()`** (StillHere's `audit_guard()` name would collide); it refuses UPDATE, DELETE and TRUNCATE on `audit_log`.

## Relational tables (`rb_001_core.sql`)
| Table | Columns (type) | Notes |
|---|---|---|
| `patients` | `patient_id text PK` (`^[a-z0-9][a-z0-9-]{0,62}$`), `label text`, `dataset text`, `dataset_case text`, `cohort text CHECK IN ('demo','synthetic_cohort')`, `focus_level text`, `created_at timestamptz` | `/patients` lists `cohort='demo'` only; the synthetic cohort is never shown as a patient |
| `scans` | `scan_id text PK`, `patient_id text FK NULL` (null = phantom), `acquisition_id text`, `level text NULL`, `day int NULL`, `time timestamptz NULL`, `label text`, `provenance text`, `generator_version text NULL`, `anchor_scan_ids text[]`, `seed bigint NULL`, `series_instance_uid text NULL`, `series_description text NULL`, `kvp real NULL`, `source_shape int[]`, `source_spacing_mm real[]`, `grid jsonb NULL`, `files jsonb` (name → {sha256, bytes, status}), `calibration jsonb NULL`, `registration jsonb`, `mask_sources jsonb`, `color_scales jsonb`, `created_at` | one row per **viewable scan** = (acquisition, focus level), e.g. `myel018-L1-d273`; the manifest is built from this row + `scan_metrics` |
| `jobs` | `job_id uuid PK`, `run_id uuid NULL`, `kind text CHECK`, `status text CHECK`, `scan_id text FK NULL`, `patient_id text NULL`, `params jsonb`, `result jsonb NULL`, `error text NULL`, `attempts int`, `claimed_by uuid NULL`, `locked_until timestamptz NULL`, `runner text NULL`, `device text NULL`, `created_at`, `started_at`, `finished_at`, `seconds double precision NULL`, `cost_usd numeric(12,6) NULL`, `cost jsonb NULL` | C5; index on `(status, kind, created_at)` |
| `audit_log` | `id bigserial PK`, `chain_key text` (`scan:<scan_id>`), `patient_id text NULL`, `scan_id text NULL`, `run_id uuid NULL`, `at timestamptz`, `actor text`, `action text`, `detail jsonb`, `prev_hash text`, `hash text` | `hash = sha256(prev_hash ‖ canonical_json({chain_key, at, actor, action, patient_id, scan_id, run_id, detail}))`, canonical = `sort_keys`, `separators=(",",":")`, `ensure_ascii=False`; `at` as UTC ISO with microseconds; genesis `prev_hash = ""`. Append under `pg_advisory_xact_lock(hashtextextended(chain_key, 0))` |
| `alert_feedback` | `feedback_id bigserial PK`, `alert_id text` (`al-<scan_id>`), `patient_id text`, `scan_id text`, `feedback text CHECK IN ('useful','not_useful','snooze')`, `snooze_until timestamptz NULL`, `note text NULL` (≤ 280), `actor text`, `created_at` | |
| `slice_rigidity` | `run_id uuid`, `scan_id text`, `site text`, `mask_source text`, `lesion_model text`, `refilled boolean`, `slice_index int`, `z_mm real`, `ea double precision`, `ei_min double precision`, `gj double precision`, `provenance text` | plain table (a few thousand rows) |

## Hypertables (`rb_002_timeseries.sql`, `rb_003_ai.sql`)
| Hypertable | Columns | Chunk interval (default) | Columnstore |
|---|---|---|---|
| `scan_metrics` | `time`, `patient_id`, `scan_id NULL`, `site`, `provenance`, `generator_version NULL`, `mask_source`, `lesion_model NULL`, `statistic NULL`, `lesion_grid`, `lesion_volume_mm3`, `vertebra_volume_mm3` (incl. lesion), `body_volume_mm3`, `stiffness_change_axial_pct`, `stiffness_change_bending_pct`, `lesion_share_pts`, `background_change_pct`, `calib_low_pct`, `calib_high_pct`, `strain_ratio`, `strain_p99_lesion_ue`, `vonmises_p99_mpa`, `modulus_lesion_gpa`, `modulus_ref_gpa`, `dice_ai_vs_expert`, `run_id NULL`, `solver_version NULL`, `created_at` | 90 days | segmentby `patient_id, site` |
| `fe_element_results` | `time`, `run_id uuid`, `patient_id`, `site`, `provenance`, `mask_source`, `lesion_model`, `i smallint`, `j smallint`, `k smallint`, `e_mpa real`, `strain_min_ue real`, `strain_max_ue real`, `vonmises_mpa real` | 30 days | segmentby `patient_id, site, run_id`; orderby `k, j, i`. Backdated chunks are converted with the manual procedure (tiger-vultr-mlh.md §2c) |
| `ai_generation` | `time`, `generation_id uuid`, `scan_id`, `patient_id`, `task`, `provider`, `model`, `prompt_version`, `facts_sha256`, `output_sha256`, `verdict`, `attempts jsonb`, `ms`, `tokens_in`, `tokens_out`, `cost_usd`, `chain_override boolean`, `cache_hit boolean`, `audit_hash text` | 7 days | none |

- Volumes are stored in **mm³** and served in **cm³** (÷ 1000, 2 decimals). `lesion_grid` names the grid of `lesion_volume_mm3` (`original` or `crop_1mm`).
- `scan_metrics` additions over the PLAN §3.3 list (`scan_id`, `generator_version`, `statistic`, `lesion_grid`, `strain_p99_lesion_ue`, `vonmises_p99_mpa`, `dice_ai_vs_expert`) are what the C1 `Metric` model needs. `fe_element_results` carries `provenance`, `mask_source` and `lesion_model` so its aggregate can group by them without a join.

## Continuous aggregates (grouped by provenance)
| Aggregate | Over | Bucket | Group by | Columns |
|---|---|---|---|---|
| `scan_metrics_monthly` | `scan_metrics` | 1 month | bucket, patient_id, site, provenance, mask_source, lesion_model, lesion_grid, statistic | `count`, `sum` and `avg` of each metric, `min`/`max` of lesion volume (the moving average is computed at query time from `sum`/`count`). `lesion_grid` and `statistic` keep the original and 1 mm crop grids, and the two stiffness statistics, in separate rows |
| `fe_level_daily` | `fe_element_results` | 1 day | bucket, patient_id, site, provenance, mask_source, lesion_model, run_id | `count`, `avg`/`min`/`max` of `strain_min_ue`, `e_mpa`; `percentile_agg` only if it works inside a cagg on our service (else min/avg/max) |

- No automatic refresh policy reaches history: after inserting a scan, the API calls `refresh_continuous_aggregate` over that scan's bucket range (M2 test: a scan 9 months back shows up after a range refresh).
- Query-time combination: `time_bucket_gapfill` + `locf`; gap-filled rows carry `filled = true`, and any series with a synthetic or filled point sets `contains_synthetic = true`. Raw vs aggregate `query_ms` are measured side by side.

## Storage budget (first estimate; M2 measures it, a test enforces it)
| Table | Bytes/row (uncompressed, incl. tuple header) | Rows | Size |
|---|---|---|---|
| `fe_element_results` | ≈ 110 B (8 time + 16 uuid + 5 short texts ≈ 40 + 3 smallint 6 + 4 real 16 + 24 header + alignment) | ≈ 1.35M (T11–L5 × 2 scans, L1 AI-mask runs, 3 synthetic months) | ≈ 150 MB + index ≈ 30 MB |
| `scan_metrics` | ≈ 250 B | ≤ 60,000 (real ≈ 120; synthetic months ≈ 500; cohort ≤ 50,000) | ≤ 15 MB |
| `slice_rigidity` | ≈ 120 B | ≤ 10,000 | ≤ 1.2 MB |
| `ai_generation` | ≈ 2 KB (attempts jsonb) | ≤ 5,000 | ≤ 10 MB |
| `audit_log` | ≈ 1 KB | ≤ 10,000 | ≤ 10 MB |
| `jobs` | ≈ 2 KB | ≤ 2,000 | ≤ 4 MB |
| `scans`, `patients`, `alert_feedback` | ≈ 4 KB, 200 B, 300 B | ≤ 1,100, ≤ 1,000, ≤ 1,000 | ≤ 5 MB |
| caggs + catalog + other indexes | | | ≤ 40 MB |
| **Total budget** | | | **300 MiB** (`budget_bytes = 314572800`) |

- The cap comes before compression; columnstore on `fe_element_results` should shrink it further (measured in M7, before/after bytes on `/tiger/info`).
- The Tiger free-tier figure of 750 MiB is from a search snippet and **not verified** (tiger-vultr-mlh.md §2d); at the cap the service turns read-only. 300 MiB leaves headroom either way.
- Tests: the seeded DB (M2) and the DB after the cohort (M7) both stay under `budget_bytes` via `pg_database_size()`; the cohort size is capped by a test.
