-- RainbowBone relational schema (contracts/db.md, C3): patients, scans, jobs, audit_log, alert_feedback,
-- slice_rigidity. Plain PostgreSQL; the time-series hypertables are in rb_002_timeseries.sql.
--
-- Rules from C3 enforced here:
--   * provenance is one of real_scan, synthetic_insilico, synthetic_metric_interp (no forecast value);
--   * a synthetic row carries generator_version;
--   * no column is called "risk";
--   * audit_log is append-only: rb_audit_guard() refuses UPDATE, DELETE and TRUNCATE.

CREATE TABLE patients (
  patient_id   text PRIMARY KEY CHECK (patient_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  label        text NOT NULL,
  dataset      text NOT NULL,
  dataset_case text,
  cohort       text NOT NULL DEFAULT 'demo' CHECK (cohort IN ('demo', 'synthetic_cohort')),
  focus_level  text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- One row per viewable scan = (acquisition, focus level), e.g. myel018-L1-d273. The manifest
-- (GET /api/v1/scans/{scan_id}) is built from this row plus scan_metrics.
-- files: {name: {sha256, bytes, status, basis, available_in}}; kind, variant, field and media type
-- follow from the name (contracts/formats.md).
CREATE TABLE scans (
  scan_id            text PRIMARY KEY CHECK (scan_id ~ '^[a-z0-9][a-zA-Z0-9-]{0,62}$'),
  patient_id         text REFERENCES patients (patient_id),
  acquisition_id     text NOT NULL,
  level              text,
  day                int CHECK (day >= 0),
  time               timestamptz,
  label              text NOT NULL,
  provenance         text NOT NULL
                     CHECK (provenance IN ('real_scan', 'synthetic_insilico', 'synthetic_metric_interp')),
  generator_version  text,
  anchor_scan_ids    text[] NOT NULL DEFAULT '{}',
  seed               bigint,
  series_instance_uid text,
  series_description text,
  kvp                real,
  source_shape       int[],
  source_spacing_mm  real[],
  grid               jsonb,
  files              jsonb NOT NULL DEFAULT '{}'::jsonb,
  calibration        jsonb,
  registration       jsonb NOT NULL DEFAULT '{"status": "not_registered"}'::jsonb,
  mask_sources       jsonb NOT NULL DEFAULT '{}'::jsonb,
  color_scales       jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (provenance = 'real_scan' OR generator_version IS NOT NULL),
  CHECK ((day IS NULL) = (time IS NULL))
);
CREATE INDEX scans_patient_day ON scans (patient_id, level, day);

-- C5: the job queue. Claimed with FOR UPDATE SKIP LOCKED and a lease (locked_until), committed, then run
-- outside any transaction by the worker process (api/app/jobs.py).
CREATE TABLE jobs (
  job_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       uuid,
  kind         text NOT NULL CHECK (kind IN ('segment', 'physics', 'summarize', 'synth_series')),
  status       text NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  scan_id      text REFERENCES scans (scan_id),
  patient_id   text,
  params       jsonb NOT NULL DEFAULT '{}'::jsonb,
  result       jsonb,
  error        text,
  attempts     int NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claimed_by   uuid,
  locked_until timestamptz,
  runner       text,
  device       text CHECK (device IN ('cpu', 'mps', 'cuda')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  started_at   timestamptz,
  finished_at  timestamptz,
  seconds      double precision CHECK (seconds >= 0),
  cost_usd     numeric(12, 6) CHECK (cost_usd >= 0),
  cost         jsonb
);
CREATE INDEX jobs_status_kind_created ON jobs (status, kind, created_at);
CREATE INDEX jobs_run ON jobs (run_id);

-- Hash-chained audit trail, one chain per scan (chain_key = 'scan:<scan_id>'):
-- hash = sha256(prev_hash || canonical_json({chain_key, at, actor, action, patient_id, scan_id, run_id,
-- detail})), genesis prev_hash = ''. Appended under pg_advisory_xact_lock(hashtextextended(chain_key, 0)).
CREATE TABLE audit_log (
  id         bigserial PRIMARY KEY,
  chain_key  text NOT NULL,
  patient_id text,
  scan_id    text,
  run_id     uuid,
  at         timestamptz NOT NULL,
  actor      text NOT NULL,
  action     text NOT NULL CHECK (action IN ('seed_demo', 'upload', 'ingest', 'calibrate', 'register', 'segment',
                                             'physics', 'store', 'summarize', 'regenerate', 'feedback',
                                             'synth_series')),
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
  prev_hash  text NOT NULL,
  hash       text NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX audit_log_chain ON audit_log (chain_key, id);

CREATE FUNCTION rb_audit_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only: % refused', TG_OP USING ERRCODE = 'insufficient_privilege';
END
$fn$;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION rb_audit_guard();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION rb_audit_guard();

-- Care-team alert feedback (PLAN 6.6): Useful / Not useful / Snooze.
CREATE TABLE alert_feedback (
  feedback_id  bigserial PRIMARY KEY,
  alert_id     text NOT NULL,
  patient_id   text NOT NULL REFERENCES patients (patient_id),
  scan_id      text NOT NULL REFERENCES scans (scan_id),
  feedback     text NOT NULL CHECK (feedback IN ('useful', 'not_useful', 'snooze')),
  snooze_until timestamptz,
  note         text CHECK (char_length(note) <= 280),
  actor        text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK ((feedback = 'snooze') = (snooze_until IS NOT NULL))
);
CREATE INDEX alert_feedback_alert ON alert_feedback (alert_id, created_at);

-- Per-slice section stiffness (CTRA: EA, EI_min, GJ), a few thousand rows: a plain table.
CREATE TABLE slice_rigidity (
  run_id       uuid NOT NULL,
  scan_id      text NOT NULL,
  site         text NOT NULL,
  mask_source  text NOT NULL CHECK (mask_source IN ('expert', 'ai')),
  lesion_model text CHECK (lesion_model IN ('weak_bone', 'void')),
  refilled     boolean NOT NULL DEFAULT false,
  slice_index  int NOT NULL,
  z_mm         real,
  ea           double precision,
  ei_min       double precision,
  gj           double precision,
  provenance   text NOT NULL
               CHECK (provenance IN ('real_scan', 'synthetic_insilico', 'synthetic_metric_interp'))
);
CREATE INDEX slice_rigidity_run ON slice_rigidity (run_id, site, slice_index);
