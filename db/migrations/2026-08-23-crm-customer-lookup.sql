-- rag-real-estate — Migration 2026-08-23: CRM customer lookup support.
-- Story 9.3 (ISSUE-09 backend). Runs against an EXISTING database; fresh
-- installs get the same shape from db/lead_schema.sql.
--
-- The CRM endpoints (GET /api/crm/customers/search, phone reveal, marketing
-- consent withdrawal) resolve customers two ways:
--   - by normalized phone (search) -> needs an index on leads.phone;
--   - by customer_id = hmac_sha256(phone, app_secret) hex (reveal/withdraw,
--     where the raw phone must NOT travel back as a lookup key) -> the PG
--     adapter recomputes the HMAC in the WHERE clause via pgcrypto, secret
--     bound as a parameter (never embedded in SQL files or migrations).
--
-- pgcrypto ships with managed Postgres offerings (Supabase included) and is
-- idempotent here; no data change is needed — phone is already stored
-- normalized by the submit-path validator.
--
-- [LR-23/08] Down-note (rollback): purely additive;
--   BEGIN;
--   DROP INDEX IF EXISTS idx_leads_phone;
--   COMMIT;
--   (pgcrypto is left installed; other extensions may depend on it.)
-- Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads (phone);

COMMIT;
