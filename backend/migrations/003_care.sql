-- PointClickCare senior mode: synthetic residents, their daily metrics and the alert cards
-- the rule engine writes (app/care.py). Plain PostgreSQL; no extension is used here.
-- Kept apart from the KAZE tables: no foreign key to users, sessions, alerts or timers.
-- Every row is synthetic, and a CHECK constraint keeps it that way.

CREATE TABLE care_residents (
  id            text PRIMARY KEY,
  display_name  text NOT NULL,
  synthetic     boolean NOT NULL DEFAULT true CHECK (synthetic),
  -- The generator's script (ground truth for scoring the rules). The rules never read it.
  script        text NOT NULL,
  script_note   text NOT NULL DEFAULT '',
  expect_flag   boolean NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE care_daily_metrics (
  resident_id         text NOT NULL REFERENCES care_residents(id) ON DELETE CASCADE,
  day                 date NOT NULL,
  synthetic           boolean NOT NULL DEFAULT true CHECK (synthetic),
  wear_hours          double precision NOT NULL CHECK (wear_hours >= 0 AND wear_hours <= 24),
  -- Quiet-standing sway: RMS of 0.1-3.5 Hz horizontal acceleration, m/s^2.
  sway_rms            double precision CHECK (sway_rms >= 0),
  -- Stand-ups counted that day, and the median time after standing until balance
  -- (accelerometer) and heart rate had both settled; the two parts are kept too.
  stand_ups           integer NOT NULL DEFAULT 0 CHECK (stand_ups >= 0),
  settle_s            double precision CHECK (settle_s >= 0),
  settle_sway_s       double precision CHECK (settle_sway_s >= 0),
  settle_hr_s         double precision CHECK (settle_hr_s >= 0),
  -- The night that ended on the morning of `day` (23:00-06:00): minutes with movement,
  -- and minutes the device was worn.
  night_restless_min  double precision CHECK (night_restless_min >= 0),
  night_wear_min      double precision CHECK (night_wear_min >= 0 AND night_wear_min <= 420),
  PRIMARY KEY (resident_id, day)
);

CREATE TABLE care_alerts (
  id           bigserial PRIMARY KEY,
  resident_id  text NOT NULL REFERENCES care_residents(id) ON DELETE CASCADE,
  as_of        date NOT NULL,
  synthetic    boolean NOT NULL DEFAULT true CHECK (synthetic),
  score        integer NOT NULL CHECK (score BETWEEN 0 AND 100),
  level        text NOT NULL CHECK (level IN ('low', 'watch', 'check today')),
  card         jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (resident_id, as_of)
);
CREATE INDEX care_alerts_latest ON care_alerts (as_of DESC, score DESC);
