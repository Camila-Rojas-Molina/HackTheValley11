-- Each contact gets their own token for an alert's released location (the alert's release row keeps
-- contact_id NULL). Removing a contact ends their links at once; the other contacts keep theirs.
ALTER TABLE location_shares ADD COLUMN contact_id bigint REFERENCES contacts(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX location_shares_alert_contact ON location_shares (alert_id, contact_id)
  WHERE contact_id IS NOT NULL;
CREATE INDEX location_shares_contact ON location_shares (contact_id) WHERE contact_id IS NOT NULL;
