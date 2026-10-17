-- Senior mode, research 1.2: "rise to settle" is split. Its heart-rate half becomes the
-- heart-rate change after standing (mean heart rate 60-180 s after a stand-up minus the 60 s
-- before, daily median), counted only on stand-ups that had heart rate; its balance half is
-- shown as experimental and not scored. settle_s and settle_hr_s are no longer written.
ALTER TABLE care_daily_metrics
  ADD COLUMN stand_hr_n integer NOT NULL DEFAULT 0 CHECK (stand_hr_n >= 0),
  ADD COLUMN stand_hr_delta_bpm double precision CHECK (stand_hr_delta_bpm BETWEEN -100 AND 100);

-- Synthetic record field: switches the heart-rate rule off for a resident whose record notes
-- non-sinus rhythm or a pacemaker (NULL: the rule applies).
ALTER TABLE care_residents ADD COLUMN hr_rule_off text;
