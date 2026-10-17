-- Research 1.15: the wearer chooses who may receive a duress alert, so an abusive "trusted
-- contact" never gets one. false for every contact = none chosen = every approved contact can
-- receive it (the behaviour before this column). Only the wearer can change it.
ALTER TABLE contacts ADD COLUMN duress_eligible boolean NOT NULL DEFAULT false;
