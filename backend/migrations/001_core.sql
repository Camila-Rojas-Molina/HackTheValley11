-- StillHere core schema (KAZE). Plain PostgreSQL 14+; no extension required.
-- Relational tables stay plain tables: a hypertable's unique keys must include
-- the time column, and these tables need their own keys.
-- hr_samples is created here as a plain table so the core runs anywhere; a later
-- migration may convert it to a hypertable when TimescaleDB is present.

CREATE TABLE users (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name       text NOT NULL,
  api_key_hash       text UNIQUE,
  pin_hash           text,
  duress_pin_hash    text,
  telegram_chat_id   bigint UNIQUE,
  tg_link_token_hash text UNIQUE,
  tg_link_expires_at timestamptz,
  is_demo            boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE devices (
  id                text PRIMARY KEY,
  user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_key_hash   text NOT NULL,
  boot_id           text,
  last_seq          bigint,
  lease_expires_at  timestamptz,
  last_heartbeat_at timestamptz,
  wear_mode         text,
  last_enmo60_mg    real,
  last_vigorous_at  timestamptz,
  offbody           boolean,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contacts (
  id                bigserial PRIMARY KEY,
  user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name              text NOT NULL DEFAULT '',
  priority          int NOT NULL DEFAULT 100,
  channel           text NOT NULL DEFAULT 'pending' CHECK (channel IN ('pending', 'telegram', 'web')),
  telegram_chat_id  bigint,
  web_token_hash    text UNIQUE,
  web_token_nonce   text,
  invite_token_hash text UNIQUE,
  invite_expires_at timestamptz,
  invite_used_at    timestamptz,
  joined_at         timestamptz,
  approved_at       timestamptz,
  revoked_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX contacts_user ON contacts (user_id);

CREATE TABLE sessions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_id          text REFERENCES devices(id) ON DELETE SET NULL,
  state              text NOT NULL DEFAULT 'ARMED',
  public_state       text NOT NULL DEFAULT 'ARMED',
  link_state         text NOT NULL DEFAULT 'OK' CHECK (link_state IN ('OK', 'LOST_CONTACT')),
  version            int NOT NULL DEFAULT 0,
  profile            text NOT NULL DEFAULT 'demo' CHECK (profile IN ('demo', 'real')),
  mode               text NOT NULL DEFAULT 'wrist' CHECK (mode IN ('wrist', 'hair')),
  preset             text,
  interval_s         int NOT NULL CHECK (interval_s > 0),
  window_multiplier  real NOT NULL DEFAULT 1 CHECK (window_multiplier >= 1 AND window_multiplier <= 10),
  challenge_n        smallint,
  challenge_id       text,
  prompt_cause       text,
  retry_used         boolean NOT NULL DEFAULT false,
  prompted_at        timestamptz,
  reminded_at        timestamptz,
  lost_contact_at    timestamptz,
  lost_contact_told  boolean NOT NULL DEFAULT false,
  hr_deviating_since timestamptz,
  pull_forward_last  jsonb NOT NULL DEFAULT '{}'::jsonb,
  end_at             timestamptz,
  ended_at           timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
-- One active session per wearer.
CREATE UNIQUE INDEX sessions_one_active ON sessions (user_id) WHERE state <> 'ENDED';

-- Durable timers: the database clock is the only clock. A kill -9 before COMMIT
-- rolls the whole transition back and the next process fires it again.
CREATE TABLE timers (
  id               bigserial PRIMARY KEY,
  session_id       uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind             text NOT NULL,
  fire_at          timestamptz NOT NULL,
  expected_version int,
  payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
  attempts         int NOT NULL DEFAULT 0,
  last_error       text,
  result           text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  fired_at         timestamptz,
  cancelled_at     timestamptz
);
CREATE INDEX timers_due ON timers (fire_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;
CREATE INDEX timers_session_pending ON timers (session_id) WHERE fired_at IS NULL AND cancelled_at IS NULL;

CREATE TABLE alerts (
  id             bigserial PRIMARY KEY,
  session_id     uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind           text NOT NULL CHECK (kind IN ('NO_RESPONSE', 'DURESS', 'LOST_CONTACT')),
  level          int NOT NULL DEFAULT 1,
  is_final       boolean NOT NULL DEFAULT false,
  masked         boolean NOT NULL DEFAULT false,
  status         text NOT NULL DEFAULT 'OPEN'
                 CHECK (status IN ('OPEN', 'ACKED', 'RESOLVED', 'FALSE_ALARM', 'RESTORED', 'RESET')),
  acked_by       bigint REFERENCES contacts(id),
  acked_at       timestamptz,
  renotify_count int NOT NULL DEFAULT 0,
  resolved_by    text,
  opened_at      timestamptz NOT NULL DEFAULT now(),
  closed_at      timestamptz
);
CREATE UNIQUE INDEX alerts_one_open ON alerts (session_id) WHERE status IN ('OPEN', 'ACKED');

CREATE TABLE alert_recipients (
  alert_id          bigint NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  contact_id        bigint NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  first_notified_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (alert_id, contact_id)
);

CREATE TABLE acks (
  id                 bigserial PRIMARY KEY,
  alert_id           bigint NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  contact_id         bigint NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  action             text NOT NULL CHECK (action IN ('ack', 'pass', 'safe', 'cant_reach')),
  source             text NOT NULL CHECK (source IN ('telegram', 'web')),
  telegram_update_id bigint UNIQUE,
  result             text,
  at                 timestamptz NOT NULL DEFAULT now()
);

-- Device events: event_id = boot_id:seq. A replayed event_id returns the cached result.
CREATE TABLE device_events (
  device_id   text NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  event_id    text NOT NULL,
  type        text NOT NULL,
  payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
  device_ms   bigint,
  received_at timestamptz NOT NULL DEFAULT now(),
  result      jsonb,
  PRIMARY KEY (device_id, event_id)
);

-- Outbox: at-least-once delivery, deduplicated at enqueue time by dedupe_key.
CREATE TABLE outbox (
  id                  bigserial PRIMARY KEY,
  dedupe_key          text NOT NULL UNIQUE,
  session_id          uuid,
  alert_id            bigint,
  user_id             uuid,
  contact_id          bigint,
  recipient           text NOT NULL CHECK (recipient IN ('wearer', 'contact', 'chat')),
  chat_id             bigint,
  method              text NOT NULL DEFAULT 'sendMessage' CHECK (method IN ('sendMessage', 'editMessageText')),
  edit_message_id     bigint,
  purpose             text NOT NULL,
  template            text,
  text                text NOT NULL,
  reply_markup        jsonb,
  protect_content     boolean NOT NULL DEFAULT false,
  timer_id            bigint,
  prompt_window_s     real,
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
  channel             text,
  attempts            int NOT NULL DEFAULT 0,
  next_attempt_at     timestamptz NOT NULL DEFAULT now(),
  locked_until        timestamptz,
  provider_message_id text,
  last_error          text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  sent_at             timestamptz
);
CREATE INDEX outbox_ready ON outbox (next_attempt_at) WHERE status = 'pending';
CREATE INDEX outbox_sending ON outbox (locked_until) WHERE status = 'sending';
CREATE INDEX outbox_session ON outbox (session_id);

-- Personal timeline: append-only and hash-chained per chain_key.
-- hash = sha256(prev_hash || canonical_json(row)).
CREATE TABLE audit_log (
  id         bigserial PRIMARY KEY,
  chain_key  text NOT NULL,
  user_id    uuid,
  session_id uuid,
  alert_id   bigint,
  at         timestamptz NOT NULL,
  actor      text NOT NULL,
  kind       text NOT NULL,
  from_state text,
  to_state   text,
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
  masked     boolean NOT NULL DEFAULT false,
  prev_hash  text NOT NULL,
  hash       text NOT NULL
);
CREATE INDEX audit_chain ON audit_log (chain_key, id);
CREATE INDEX audit_session ON audit_log (session_id, id);
CREATE INDEX audit_user ON audit_log (user_id);

CREATE FUNCTION audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('stillhere.erase', true) = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'audit_log is append-only';
END $$;

CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_guard();
-- TRUNCATE bypasses row triggers, so block it too.
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_guard();

-- Last-known location: coordinates are encrypted by the app (AES-GCM).
CREATE TABLE location_snapshots (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id  uuid,
  coords_enc  bytea NOT NULL,
  accuracy_m  real,
  source      text NOT NULL,
  simulated   boolean NOT NULL DEFAULT false,
  captured_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX location_snapshots_user ON location_snapshots (user_id, captured_at DESC);

-- A share is released at alert time; GET /l/{token} returns 410 after expires_at.
CREATE TABLE location_shares (
  id          bigserial PRIMARY KEY,
  token_hash  text NOT NULL UNIQUE,
  token_nonce text NOT NULL,
  snapshot_id bigint REFERENCES location_snapshots(id) ON DELETE CASCADE,
  alert_id    bigint REFERENCES alerts(id) ON DELETE CASCADE,
  released_at timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz
);
CREATE INDEX location_shares_alert ON location_shares (alert_id);

CREATE TABLE bot_state (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_baselines (
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind        text NOT NULL,
  value       real NOT NULL,
  simulated   boolean NOT NULL DEFAULT false,
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, kind)
);

-- Heart rate from any source (HeartCast via the BLE bridge, a streaming API,
-- a Shortcut, the simulator). Plain table; no unique key so it can become a
-- hypertable later.
CREATE TABLE hr_samples (
  time         timestamptz NOT NULL,
  user_id      uuid NOT NULL,
  source       text NOT NULL,
  simulated    boolean NOT NULL DEFAULT false,
  bpm          smallint NOT NULL CHECK (bpm BETWEEN 20 AND 250),
  rr_ms        real[],
  rr_synthetic boolean
);
CREATE INDEX hr_samples_user_time ON hr_samples (user_id, time DESC);
