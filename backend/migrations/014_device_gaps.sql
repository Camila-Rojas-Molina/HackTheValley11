-- Research 4.3: a "device sent no data" timeline for the care team (care profile), read-only. The
-- server's check-in countdown stays the authority; nothing here changes a session.
-- With TimescaleDB and Toolkit 1.15 or later: an hourly real-time continuous aggregate of
-- heartbeat_agg (a sample keeps the device "live" for 60 s), rolled up and read with live_ranges()
-- (app/timeseries.py: the window minus the live ranges).
-- Verified on TimescaleDB 2.30.2 + Toolkit 1.26.0, including inside a continuous aggregate.
-- Anywhere else, app/timeseries.py computes the same ranges with lead() over the raw rows.
ALTER TABLE timeseries_setup ADD COLUMN gaps_method text NOT NULL DEFAULT 'plain_lag';

DO $hb$
BEGIN
  IF EXISTS (SELECT 1 FROM timeseries_setup WHERE mode = 'timescaledb')
     AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb_toolkit'
                 AND string_to_array(split_part(extversion, '-', 1), '.')::int[] >= ARRAY[1, 15, 0]) THEN
    BEGIN
      CREATE MATERIALIZED VIEW telemetry_heartbeat_1h
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket(INTERVAL '1 hour', time) AS bucket, user_id,
               heartbeat_agg(time, time_bucket(INTERVAL '1 hour', time), INTERVAL '1 hour', INTERVAL '60 seconds') AS hb
          FROM scrunchie_telemetry
         WHERE profile = 'care'
         GROUP BY time_bucket(INTERVAL '1 hour', time), user_id
        WITH NO DATA;
      PERFORM add_continuous_aggregate_policy('telemetry_heartbeat_1h', start_offset => INTERVAL '1 day',
              end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes');
      PERFORM add_retention_policy('telemetry_heartbeat_1h', drop_after => INTERVAL '30 days');
      UPDATE timeseries_setup SET gaps_method = 'toolkit_heartbeat_agg';
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'heartbeat_agg view not created (%); gaps use the plain computation', SQLERRM;
    END;
  END IF;
END
$hb$;
