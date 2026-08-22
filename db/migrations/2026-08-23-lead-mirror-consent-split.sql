-- rag-real-estate — Migration 2026-08-23: lead mirror columns + consent split.
-- Story 9.2 (ISSUE-08). Runs against an EXISTING database; fresh installs get
-- the same shape from db/lead_schema.sql (CREATE TABLE IF NOT EXISTS + indexes).
--
-- Adds the columns the PG->Firestore dual-write mirror needs:
--   - mirror_status tracks per-row mirror convergence ('pending' | 'done' |
--     'failed'); the reconciliation sweep retries stale pending/failed rows.
--   - consent_service / consent_marketing / consent_at / consent_version split
--     the legacy single `consent` flag. Backfill: consent_service = consent.
--     The legacy column stays during the transition (commented below); the API
--     keeps writing it until the FE contract carries the split flags.
--   - rejection_reason / reengage_at / marketing_withdrawn_at are nullable
--     future-state columns (re-approach 9.4, marketing withdrawal) so the
--     base schema stops drifting behind the mirror document contract.
--
-- project_key is re-guarded (IF NOT EXISTS) because some databases may not
-- have run the 2026-08-22 migration yet — this file must be self-sufficient.
--
-- [LR-23/08] Down-note (rollback): every change is additive or a backfill;
--   BEGIN;
--   DROP INDEX IF EXISTS idx_leads_mirror_stale;
--   ALTER TABLE leads DROP COLUMN IF EXISTS mirror_status;
--   ALTER TABLE leads DROP COLUMN IF EXISTS consent_service;
--   ALTER TABLE leads DROP COLUMN IF EXISTS consent_marketing;
--   ALTER TABLE leads DROP COLUMN IF EXISTS consent_at;
--   ALTER TABLE leads DROP COLUMN IF EXISTS consent_version;
--   ALTER TABLE leads DROP COLUMN IF EXISTS marketing_withdrawn_at;
--   ALTER TABLE leads DROP COLUMN IF EXISTS rejection_reason;
--   ALTER TABLE leads DROP COLUMN IF EXISTS reengage_at;
--   COMMENT ON COLUMN leads.consent IS NULL;
--   COMMIT;
-- Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

ALTER TABLE leads ADD COLUMN IF NOT EXISTS project_key TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS reengage_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS mirror_status TEXT NOT NULL DEFAULT 'pending'
  CHECK (mirror_status IN ('pending', 'done', 'failed'));
ALTER TABLE leads ADD COLUMN IF NOT EXISTS consent_service BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS consent_marketing BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS consent_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE leads ADD COLUMN IF NOT EXISTS consent_version TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS marketing_withdrawn_at TIMESTAMPTZ;

-- One-time backfill: the legacy single flag meant "service contact allowed".
-- Marketing consent is a separate opt-in and is NOT inherited (stays false).
UPDATE leads SET consent_service = consent WHERE consent AND NOT consent_service;

-- Transition marker: legacy column kept until the FE contract carries the
-- split flags; new code writes consent_service alongside it.
COMMENT ON COLUMN leads.consent IS 'LEGACY single consent flag (9.2 transition): kept for rollback safety; authoritative split lives in consent_service/consent_marketing';

CREATE INDEX IF NOT EXISTS idx_leads_project ON leads (project_key);
CREATE INDEX IF NOT EXISTS idx_leads_device ON leads (device_id);
-- Reconciliation sweep: bounded scans of unconverged mirror rows only.
CREATE INDEX IF NOT EXISTS idx_leads_mirror_stale ON leads (created_at)
  WHERE mirror_status IN ('pending', 'failed');

COMMIT;
