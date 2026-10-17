# C5: jobs

Table `jobs` (C3). API: `POST /api/v1/scans/{scan_id}/runs` (admin) queues jobs; `GET /api/v1/jobs/{job_id}` returns `Job` (schemas.py). The worker (`worker/rb_worker`, S4) is a **separate process**: no CPU-bound job ever runs in the API's event loop.

## Kinds
| Kind | Payload (`params`) | Result (`result`) | Timeout | Max attempts |
|---|---|---|---|---|
| `segment` | `{scan_id, levels: [Level], device: "cpu"\|"mps"\|"cuda", resolution_mm: 1.5, tasks: ["total --roi_subset vertebrae_*", "vertebrae_body"]}` | `{masks: [{name, sha256, voxels}], dice_vs_expert: {Level: float}, model_seconds, device}` | **900 s** | 2 |
| `physics` | `{scan_id, level, baseline_scan_id \| null, mask_source, lesion_models: [weak_bone, void], load_n: 800, voxel_mm: 1.0}` | `{run_id, metrics: [Metric], assets: [{name, sha256, bytes}], dof, cg_iterations, peak_rss_mb, element_rows}` | **120 s** | 2 |
| `summarize` | `{scan_id, task: care_team_summary\|patient_explanation, chain: [AISource] \| null, bypass_cache: bool}` | `{generation_id, source, verified}` | 60 s | 1 |
| `synth_series` | `{patient_id, scan_a, scan_b, days: [int], volume_curve: "linear", seed: int, generator_version: str}` | `{scan_ids: [str]}` | 900 s | 1 |

- The two physics/segment timeouts are PLAN values; the `summarize`/`synth_series` timeouts and every max-attempts value are defaults S4 may tune.
- `physics` refuses a crop without a calibration block (C7) and fails the job with `error = "calibration missing"`.
- `segment` and `physics` refuse Myel_018 T1–T6 (metal implant C6–T6): `error = "implant region"`.
- A run (`run_id`) groups the jobs one `POST …/runs` created; `params.steps` order is `segment → physics → summarize`, each queued when the previous one succeeds.

## Statuses
`queued → running → succeeded | failed`; `cancelled` only by an admin. A lease that expires puts a `running` job back to `queued` (if attempts remain) or `failed` with `error = "lease expired"`.

## Lease semantics (adapted from StillHere `outbox.py`, reuse.md §1 row 11)
1. **Claim** in one short transaction, then commit:
   ```sql
   UPDATE jobs SET status = 'running', claimed_by = %(token)s, locked_until = now() + interval '60 seconds',
                   attempts = attempts + 1, started_at = coalesce(started_at, now())
   WHERE job_id = (SELECT job_id FROM jobs
                   WHERE status = 'queued' AND kind = ANY(%(kinds)s)
                   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
   RETURNING *;
   ```
2. **Run outside any transaction.** `db.py` sets `idle_in_transaction_session_timeout = '10s'`, so work inside an open transaction would be killed.
3. **Extend** the lease every 20 s while running (`UPDATE … SET locked_until = now() + 60 s WHERE job_id = … AND claimed_by = token`). A zero-row update means the lease was lost: stop and discard.
4. **Finish** with a guarded update (`WHERE claimed_by IS NOT DISTINCT FROM token`): `status`, `result` or `error`, `finished_at`, `seconds`, `cost_usd`, `cost`.
5. **Reap** expired leases on every poll; **reclaim orphans** at worker start-up.
6. M2 test: a 30 s job survives the 10 s idle-in-transaction timeout.

## Seconds and cost (every job)
- `seconds` = wall time from claim to finish on the worker.
- `cost_usd` = compute_seconds × compute rate + gpu_seconds × GPU rate + tokens_in × input price + tokens_out × output price. The breakdown and the rates used go in `cost` (`CostBreakdown`), so the number can be re-derived.
- **Rates** (list prices read 2026-10-17; solver.md §7, ai.md §4–§5, §13):

  | Item | Rate |
  |---|---|
  | Vultr `vhf-4c-16gb` (Toronto) | $0.132 / h |
  | Vultr `vcg-a16-6c-60g-16vram` (ewr/ord) | $0.471 / h |
  | Gemini `gemini-3.8-flash` | $0.75 in / $3.75 out per 1M tokens |
  | Gemini `gemini-3.5-flash-lite` | $0.30 in / $2.50 out per 1M tokens |
  | Groq `openai/gpt-oss-120b` | $0.15 in / $0.60 out per 1M tokens |
  | Vultr Inference `qwen3.8-flash-next` | $0.10 in / $0.20 out per 1M tokens (research pack; prompt price confirmed live) |
  | local, template | $0 |

- `runner` names the host (`vultr-vhf-4c-16gb-yto`, `vultr-a16-<region>`, `mac-dev`). A runner with no cloud rate (the Mac) records `cost_usd = null` with `cost.note = "local run: no cloud cost"`; it is never priced as if it ran on Vultr.
- The M5 cost test: the formula matches a hand calculation.

## Audit
Each step appends to the scan's audit chain (C3 `audit_log`): action = kind, detail = input sha256s + params + result sha256s.
