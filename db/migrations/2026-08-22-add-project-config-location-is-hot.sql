-- rag-real-estate — Migration 2026-08-22: project_config.location + project_config.is_hot.
-- Story 10.3: the FE project picker reads a real project catalogue from
-- GET /api/projects. `location` carries the detailed Vietnamese address shown in
-- the picker popup (the popup previously had no address despite vi_tri holding
-- the data), and `is_hot` marks the HOT project (Camellia) so it leads the list.
--
-- Additive + idempotent: ALTER ... ADD COLUMN IF NOT EXISTS is safe to run
-- repeatedly, and a no-op on fresh installs where db/schema.sql already defines
-- the columns.
--
-- Backfill: the new seed (db/seed/project_config.sql) writes the corrected
-- detailed addresses for camellia/soleil; until that seed re-runs, every row
-- falls back to its existing vi_tri so the endpoint never serves a NULL
-- location.
--
-- [LR-22/08] Down-note (rollback):
--   BEGIN;
--   ALTER TABLE project_config DROP COLUMN IF EXISTS is_hot;
--   ALTER TABLE project_config DROP COLUMN IF EXISTS location;
--   COMMIT;
-- Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

ALTER TABLE project_config ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE project_config ADD COLUMN IF NOT EXISTS is_hot BOOLEAN NOT NULL DEFAULT false;

-- Until the project_config seed is re-run, mirror vi_tri so the picker never
-- reads a NULL address (the seed overwrites the two real projects afterwards).
UPDATE project_config SET location = vi_tri WHERE location IS NULL;

COMMIT;
