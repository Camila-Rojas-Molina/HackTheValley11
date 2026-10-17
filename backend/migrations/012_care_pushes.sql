-- Senior mode, research 1.6: the alert budget. A flagged card is pushed at most once per resident
-- per 24 h, and an identical alert (same level and rules) is muted for 3 days unless it gets
-- worse; everything else goes into the shift-start digest (GET /v1/care/digest).
CREATE TABLE care_pushes (
  resident_id  text NOT NULL REFERENCES care_residents(id) ON DELETE CASCADE,
  as_of        date NOT NULL,
  pushed_at    timestamptz NOT NULL DEFAULT now(),
  level        text NOT NULL,
  score        integer NOT NULL,
  signature    text NOT NULL,
  synthetic    boolean NOT NULL DEFAULT true CHECK (synthetic),
  PRIMARY KEY (resident_id, as_of)
);
CREATE INDEX care_pushes_recent ON care_pushes (resident_id, pushed_at DESC);
