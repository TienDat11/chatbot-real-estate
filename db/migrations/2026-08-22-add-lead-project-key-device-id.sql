-- rag-real-estate — Migration 2026-08-22: add leads.project_key + leads.device_id.
-- Story 10.1 (ISSUE-03). Runs against an EXISTING database; fresh installs get
-- the same shape from db/lead_schema.sql (CREATE TABLE IF NOT EXISTS + indexes).
--
-- Additive, both columns NULLABLE on purpose:
--   - device_id is anonymous identity (D7) — old rows never carried it, and
--     making it NOT NULL would force a backfill that cannot be invented.
--   - project_key becomes REQUIRED at the API layer (G1) but existing rows
--     predate the field — forcing NOT NULL now would break the migration on
--     databases that already hold leads. API enforcement lands first; a later
--     data-quality migration can tighten the column once old rows are backfilled.
--
-- Indexes: idx_leads_project for per-project CRM filtering (9.3); idx_leads_device
-- for "khách này đã từ chối dự án nào" re-approach lookups (9.4). Dedup stays on
-- phone (idx_leads_phone) — device_id is never a dedup key.
--
-- [LR-22/08] Down-note (rollback): the columns are additive and nullable, so
-- dropping them is safe for any database that has not yet backfilled them:
--   BEGIN;
--   DROP INDEX IF EXISTS idx_leads_project;
--   DROP INDEX IF EXISTS idx_leads_device;
--   ALTER TABLE leads DROP COLUMN IF EXISTS project_key;
--   ALTER TABLE leads DROP COLUMN IF EXISTS device_id;
--   COMMIT;
-- Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

ALTER TABLE leads ADD COLUMN IF NOT EXISTS project_key TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS device_id TEXT;

CREATE INDEX IF NOT EXISTS idx_leads_project ON leads (project_key);
CREATE INDEX IF NOT EXISTS idx_leads_device ON leads (device_id);

COMMIT;
