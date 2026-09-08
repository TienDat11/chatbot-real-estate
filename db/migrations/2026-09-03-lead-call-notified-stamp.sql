-- FR-33 / BE-CALL-NOTIFY-ONCE: exactly-once client call-started push stamp.
-- CAS claim column: dispatch_call_started_once updates it inside the
-- single-statement claim (status = 'called' AND call_notified_at IS NULL),
-- so two concurrent paths (CRM PATCH, sales action, tel-link endpoint)
-- can never both win. Repeat-safe and additive-only; nullable so legacy
-- rows keep constructing and no backfill is required.
BEGIN;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS call_notified_at TIMESTAMPTZ;
COMMIT;

-- Down note: ALTER TABLE leads DROP COLUMN IF EXISTS call_notified_at;
