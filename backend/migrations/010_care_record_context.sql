-- Senior mode, research 1.7: synthetic record context on the card. Medication starts or
-- changes ([{class, change, on}]) in the last 30 days raise a card's priority one level and
-- never start an alert on their own. Synthetic only, like every other care row.
ALTER TABLE care_residents ADD COLUMN med_changes jsonb NOT NULL DEFAULT '[]'::jsonb;
