-- AI provenance (contracts/db.md C3, contracts/ai-interface.md C4): one ai_generation row per Chain.run,
-- with provider, model, latency, attempts, verdict, tokens, cost, chain override and cache hit, linked to the
-- scan's audit chain by audit_hash. A hypertable with 7-day chunks on TimescaleDB (no columnstore, no
-- retention), a plain table otherwise. Column names match api/app/ai/provenance.generation_row().

CREATE TABLE ai_generation (
  time           timestamptz NOT NULL,
  generation_id  uuid NOT NULL,
  scan_id        text,
  patient_id     text,
  task           text NOT NULL CHECK (task IN ('care_team_summary', 'patient_explanation', 'copilot')),
  provider       text NOT NULL CHECK (provider IN ('gemini', 'groq', 'vultr', 'local', 'template')),
  model          text,
  prompt_version text NOT NULL,
  facts_sha256   text NOT NULL CHECK (facts_sha256 ~ '^[0-9a-f]{64}$'),
  output_sha256  text CHECK (output_sha256 ~ '^[0-9a-f]{64}$'),
  verdict        text NOT NULL CHECK (verdict IN ('accepted', 'rejected')),
  attempts       jsonb NOT NULL DEFAULT '[]'::jsonb,
  ms             double precision CHECK (ms >= 0),
  tokens_in      int CHECK (tokens_in >= 0),
  tokens_out     int CHECK (tokens_out >= 0),
  cost_usd       numeric(14, 8) CHECK (cost_usd >= 0),
  chain_override boolean NOT NULL DEFAULT false,
  cache_hit      boolean NOT NULL DEFAULT false,
  audit_hash     text
);
CREATE INDEX ai_generation_scan ON ai_generation (scan_id, time DESC);

DO $tsdb$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    BEGIN
      PERFORM create_hypertable('ai_generation', by_range('time', INTERVAL '7 days'));
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'ai_generation stays a plain table (%)', SQLERRM;
    END;
  END IF;
END
$tsdb$;
