-- RainbowBone time series (contracts/db.md, C3): the scan_metrics and fe_element_results hypertables and
-- their continuous aggregates, grouped by provenance.
--
-- TimescaleDB path (the extension is installed, or installable, in this database):
--   * scan_metrics (90-day chunks) and fe_element_results (30-day chunks) are hypertables, with columnstore
--     settings (segmentby) but no columnstore policy: backdated chunks are converted on purpose (M7);
--   * scan_metrics_monthly and fe_level_daily are real-time continuous aggregates created WITH NO DATA;
--   * no retention policy, no refresh policy and no refresh-window cap: the API refreshes the bucket range
--     of what it just wrote (app/tiger.py refresh_range), so a scan dated months back still reaches them.
-- Plain PostgreSQL path: the same names and columns as plain tables and views (date_trunc for time_bucket).
-- Every TimescaleDB statement sits behind a pg_extension check, and the TimescaleDB setup runs in one
-- sub-transaction: if any part fails it is rolled back and the plain objects are created instead.
-- Volumes are stored in mm3 (the API serves cm3).

-- 0. The extension, only when its library is preloaded: on a server where the package is installed but not
--    in shared_preload_libraries, CREATE EXTENSION ends the session (FATAL), which no handler can catch. The
--    preloaded loader registers its settings (e.g. timescaledb.disable_load), which pg_settings lists.
DO $ext$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb')
     AND (EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')
          OR EXISTS (SELECT 1 FROM pg_settings WHERE name = 'timescaledb.disable_load')) THEN
    BEGIN
      CREATE EXTENSION IF NOT EXISTS timescaledb;
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'timescaledb not enabled (%); using plain PostgreSQL objects', SQLERRM;
    END;
  END IF;
END
$ext$;

-- 1. Raw tables. No unique keys: a hypertable's unique keys must include the time column.
CREATE TABLE scan_metrics (
  time                         timestamptz NOT NULL,
  patient_id                   text NOT NULL,
  scan_id                      text,
  site                         text NOT NULL,
  provenance                   text NOT NULL
                               CHECK (provenance IN ('real_scan', 'synthetic_insilico', 'synthetic_metric_interp')),
  generator_version            text,
  mask_source                  text NOT NULL CHECK (mask_source IN ('expert', 'ai')),
  lesion_model                 text CHECK (lesion_model IN ('weak_bone', 'void')),
  statistic                    text CHECK (statistic IN ('central80_mean', 'weakest_5mm')),
  lesion_grid                  text CHECK (lesion_grid IN ('original', 'crop_1mm')),
  lesion_volume_mm3            double precision CHECK (lesion_volume_mm3 >= 0),
  vertebra_volume_mm3          double precision CHECK (vertebra_volume_mm3 > 0),
  body_volume_mm3              double precision CHECK (body_volume_mm3 > 0),
  stiffness_change_axial_pct   double precision,
  stiffness_change_bending_pct double precision,
  lesion_share_pts             double precision,
  background_change_pct        double precision,
  calib_low_pct                double precision,
  calib_high_pct               double precision,
  strain_ratio                 double precision,
  strain_p99_lesion_ue         double precision,
  vonmises_p99_mpa             double precision,
  modulus_lesion_gpa           double precision,
  modulus_ref_gpa              double precision,
  dice_ai_vs_expert            double precision CHECK (dice_ai_vs_expert BETWEEN 0 AND 1),
  run_id                       uuid,
  solver_version               text,
  created_at                   timestamptz NOT NULL DEFAULT now(),
  CHECK (provenance = 'real_scan' OR generator_version IS NOT NULL),
  CHECK (lesion_volume_mm3 IS NULL OR lesion_grid IS NOT NULL),
  CHECK (calib_low_pct IS NULL OR calib_high_pct IS NULL OR calib_low_pct <= calib_high_pct)
);
CREATE INDEX scan_metrics_patient_site_time ON scan_metrics (patient_id, site, time DESC);
CREATE INDEX scan_metrics_scan ON scan_metrics (scan_id, time DESC);

CREATE TABLE fe_element_results (
  time          timestamptz NOT NULL,
  run_id        uuid NOT NULL,
  patient_id    text NOT NULL,
  site          text NOT NULL,
  provenance    text NOT NULL
                CHECK (provenance IN ('real_scan', 'synthetic_insilico', 'synthetic_metric_interp')),
  mask_source   text NOT NULL CHECK (mask_source IN ('expert', 'ai')),
  lesion_model  text NOT NULL CHECK (lesion_model IN ('weak_bone', 'void')),
  i             smallint NOT NULL,
  j             smallint NOT NULL,
  k             smallint NOT NULL,
  e_mpa         real,
  strain_min_ue real,
  strain_max_ue real,
  vonmises_mpa  real
);
CREATE INDEX fe_element_results_run ON fe_element_results (run_id, time DESC);

-- 2. Hypertables and continuous aggregates, or the plain views.
DO $tsdb$
DECLARE
  use_ts boolean := EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb');
BEGIN
  IF use_ts THEN
    BEGIN
      PERFORM create_hypertable('scan_metrics', by_range('time', INTERVAL '90 days'));
      PERFORM create_hypertable('fe_element_results', by_range('time', INTERVAL '30 days'));

      ALTER TABLE scan_metrics SET (timescaledb.enable_columnstore = true,
                                    timescaledb.segmentby = 'patient_id, site',
                                    timescaledb.orderby = 'time DESC');
      ALTER TABLE fe_element_results SET (timescaledb.enable_columnstore = true,
                                          timescaledb.segmentby = 'patient_id, site, run_id',
                                          timescaledb.orderby = 'k, j, i');

      CREATE MATERIALIZED VIEW scan_metrics_monthly
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket(INTERVAL '1 month', time) AS bucket, patient_id, site, provenance, mask_source,
               lesion_model, lesion_grid, statistic,
               count(*) AS n,
               count(lesion_volume_mm3) AS lesion_volume_mm3_n, sum(lesion_volume_mm3) AS lesion_volume_mm3_sum,
               avg(lesion_volume_mm3) AS lesion_volume_mm3_avg, min(lesion_volume_mm3) AS lesion_volume_mm3_min,
               max(lesion_volume_mm3) AS lesion_volume_mm3_max,
               count(vertebra_volume_mm3) AS vertebra_volume_mm3_n,
               sum(vertebra_volume_mm3) AS vertebra_volume_mm3_sum,
               avg(vertebra_volume_mm3) AS vertebra_volume_mm3_avg,
               count(body_volume_mm3) AS body_volume_mm3_n, sum(body_volume_mm3) AS body_volume_mm3_sum,
               avg(body_volume_mm3) AS body_volume_mm3_avg,
               count(stiffness_change_axial_pct) AS stiffness_change_axial_pct_n,
               sum(stiffness_change_axial_pct) AS stiffness_change_axial_pct_sum,
               avg(stiffness_change_axial_pct) AS stiffness_change_axial_pct_avg,
               count(stiffness_change_bending_pct) AS stiffness_change_bending_pct_n,
               sum(stiffness_change_bending_pct) AS stiffness_change_bending_pct_sum,
               avg(stiffness_change_bending_pct) AS stiffness_change_bending_pct_avg,
               count(lesion_share_pts) AS lesion_share_pts_n, sum(lesion_share_pts) AS lesion_share_pts_sum,
               avg(lesion_share_pts) AS lesion_share_pts_avg,
               count(background_change_pct) AS background_change_pct_n,
               sum(background_change_pct) AS background_change_pct_sum,
               avg(background_change_pct) AS background_change_pct_avg,
               count(calib_low_pct) AS calib_low_pct_n, sum(calib_low_pct) AS calib_low_pct_sum,
               avg(calib_low_pct) AS calib_low_pct_avg,
               count(calib_high_pct) AS calib_high_pct_n, sum(calib_high_pct) AS calib_high_pct_sum,
               avg(calib_high_pct) AS calib_high_pct_avg,
               count(strain_ratio) AS strain_ratio_n, sum(strain_ratio) AS strain_ratio_sum,
               avg(strain_ratio) AS strain_ratio_avg,
               count(strain_p99_lesion_ue) AS strain_p99_lesion_ue_n,
               sum(strain_p99_lesion_ue) AS strain_p99_lesion_ue_sum,
               avg(strain_p99_lesion_ue) AS strain_p99_lesion_ue_avg,
               count(vonmises_p99_mpa) AS vonmises_p99_mpa_n, sum(vonmises_p99_mpa) AS vonmises_p99_mpa_sum,
               avg(vonmises_p99_mpa) AS vonmises_p99_mpa_avg,
               count(modulus_lesion_gpa) AS modulus_lesion_gpa_n, sum(modulus_lesion_gpa) AS modulus_lesion_gpa_sum,
               avg(modulus_lesion_gpa) AS modulus_lesion_gpa_avg,
               count(modulus_ref_gpa) AS modulus_ref_gpa_n, sum(modulus_ref_gpa) AS modulus_ref_gpa_sum,
               avg(modulus_ref_gpa) AS modulus_ref_gpa_avg,
               count(dice_ai_vs_expert) AS dice_ai_vs_expert_n, sum(dice_ai_vs_expert) AS dice_ai_vs_expert_sum,
               avg(dice_ai_vs_expert) AS dice_ai_vs_expert_avg
          FROM scan_metrics
         GROUP BY time_bucket(INTERVAL '1 month', time), patient_id, site, provenance, mask_source, lesion_model,
                  lesion_grid, statistic
        WITH NO DATA;

      CREATE MATERIALIZED VIEW fe_level_daily
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket(INTERVAL '1 day', time) AS bucket, patient_id, site, provenance, mask_source,
               lesion_model, run_id,
               count(*) AS n,
               avg(strain_min_ue) AS strain_min_ue_avg, min(strain_min_ue) AS strain_min_ue_min,
               max(strain_min_ue) AS strain_min_ue_max,
               avg(e_mpa) AS e_mpa_avg, min(e_mpa) AS e_mpa_min, max(e_mpa) AS e_mpa_max
          FROM fe_element_results
         GROUP BY time_bucket(INTERVAL '1 day', time), patient_id, site, provenance, mask_source, lesion_model,
                  run_id
        WITH NO DATA;
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'TimescaleDB setup failed (%); using plain PostgreSQL objects instead', SQLERRM;
      use_ts := false;
    END;
  END IF;

  IF NOT use_ts THEN
    CREATE VIEW scan_metrics_monthly AS
      SELECT date_trunc('month', time, 'UTC') AS bucket, patient_id, site, provenance, mask_source, lesion_model,
             lesion_grid, statistic,
             count(*) AS n,
             count(lesion_volume_mm3) AS lesion_volume_mm3_n, sum(lesion_volume_mm3) AS lesion_volume_mm3_sum,
             avg(lesion_volume_mm3) AS lesion_volume_mm3_avg, min(lesion_volume_mm3) AS lesion_volume_mm3_min,
             max(lesion_volume_mm3) AS lesion_volume_mm3_max,
             count(vertebra_volume_mm3) AS vertebra_volume_mm3_n,
             sum(vertebra_volume_mm3) AS vertebra_volume_mm3_sum,
             avg(vertebra_volume_mm3) AS vertebra_volume_mm3_avg,
             count(body_volume_mm3) AS body_volume_mm3_n, sum(body_volume_mm3) AS body_volume_mm3_sum,
             avg(body_volume_mm3) AS body_volume_mm3_avg,
             count(stiffness_change_axial_pct) AS stiffness_change_axial_pct_n,
             sum(stiffness_change_axial_pct) AS stiffness_change_axial_pct_sum,
             avg(stiffness_change_axial_pct) AS stiffness_change_axial_pct_avg,
             count(stiffness_change_bending_pct) AS stiffness_change_bending_pct_n,
             sum(stiffness_change_bending_pct) AS stiffness_change_bending_pct_sum,
             avg(stiffness_change_bending_pct) AS stiffness_change_bending_pct_avg,
             count(lesion_share_pts) AS lesion_share_pts_n, sum(lesion_share_pts) AS lesion_share_pts_sum,
             avg(lesion_share_pts) AS lesion_share_pts_avg,
             count(background_change_pct) AS background_change_pct_n,
             sum(background_change_pct) AS background_change_pct_sum,
             avg(background_change_pct) AS background_change_pct_avg,
             count(calib_low_pct) AS calib_low_pct_n, sum(calib_low_pct) AS calib_low_pct_sum,
             avg(calib_low_pct) AS calib_low_pct_avg,
             count(calib_high_pct) AS calib_high_pct_n, sum(calib_high_pct) AS calib_high_pct_sum,
             avg(calib_high_pct) AS calib_high_pct_avg,
             count(strain_ratio) AS strain_ratio_n, sum(strain_ratio) AS strain_ratio_sum,
             avg(strain_ratio) AS strain_ratio_avg,
             count(strain_p99_lesion_ue) AS strain_p99_lesion_ue_n,
             sum(strain_p99_lesion_ue) AS strain_p99_lesion_ue_sum,
             avg(strain_p99_lesion_ue) AS strain_p99_lesion_ue_avg,
             count(vonmises_p99_mpa) AS vonmises_p99_mpa_n, sum(vonmises_p99_mpa) AS vonmises_p99_mpa_sum,
             avg(vonmises_p99_mpa) AS vonmises_p99_mpa_avg,
             count(modulus_lesion_gpa) AS modulus_lesion_gpa_n, sum(modulus_lesion_gpa) AS modulus_lesion_gpa_sum,
             avg(modulus_lesion_gpa) AS modulus_lesion_gpa_avg,
             count(modulus_ref_gpa) AS modulus_ref_gpa_n, sum(modulus_ref_gpa) AS modulus_ref_gpa_sum,
             avg(modulus_ref_gpa) AS modulus_ref_gpa_avg,
             count(dice_ai_vs_expert) AS dice_ai_vs_expert_n, sum(dice_ai_vs_expert) AS dice_ai_vs_expert_sum,
             avg(dice_ai_vs_expert) AS dice_ai_vs_expert_avg
        FROM scan_metrics
       GROUP BY date_trunc('month', time, 'UTC'), patient_id, site, provenance, mask_source, lesion_model,
                lesion_grid, statistic;

    CREATE VIEW fe_level_daily AS
      SELECT date_trunc('day', time, 'UTC') AS bucket, patient_id, site, provenance, mask_source, lesion_model,
             run_id,
             count(*) AS n,
             avg(strain_min_ue) AS strain_min_ue_avg, min(strain_min_ue) AS strain_min_ue_min,
             max(strain_min_ue) AS strain_min_ue_max,
             avg(e_mpa) AS e_mpa_avg, min(e_mpa) AS e_mpa_min, max(e_mpa) AS e_mpa_max
        FROM fe_element_results
       GROUP BY date_trunc('day', time, 'UTC'), patient_id, site, provenance, mask_source, lesion_model, run_id;
  END IF;
END
$tsdb$;
