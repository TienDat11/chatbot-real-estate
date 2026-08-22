-- rag-real-estate — Migration 2026-08-22: add images.project_key (story 10.4).
-- Scopes the illustrative-image enrichment so a Soleil query never surfaces
-- Camellia imagery (isolation acceptance of 10.4). The media lane's image
-- search (api/application/services/image_search.py) is NOT touched: the
-- project filter is a post-filter in the workflow merge step that reads this
-- column.
--
-- Backfill note: every image in the current corpus is Camellia (the manifest
-- ingest/image_captions_manifest.json carries project "The Camellia Son Tra -
-- Da Nang"), so the whole existing set is tagged 'camellia'. Future projects
-- are tagged by the ingest lane at write time; any image that has no project
-- tag is dropped by filter_images_by_project (isolation first — an untagged
-- image must never leak across projects).
--
-- The column stays NULLABLE: the images ingest (ingest/images_ingest.py) is
-- media-lane owned and may lag this change, so a NULL tag is tolerated and
-- simply excluded from every project's results.
--
-- [LR-22/08] Down-note (rollback):
--   BEGIN;
--   DROP INDEX IF EXISTS idx_images_project_status;
--   ALTER TABLE images DROP COLUMN IF EXISTS project_key;
--   COMMIT;
-- Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

ALTER TABLE images ADD COLUMN IF NOT EXISTS project_key TEXT;

-- Project-scoped image lookups for filter_images_by_project.
CREATE INDEX IF NOT EXISTS idx_images_project_status
  ON images (project_key, status) WHERE status = 'published';

-- One-time backfill: the current corpus predates the project tag and is
-- entirely Camellia; tagging it here keeps existing Camellia enrichment
-- working under the new filter (expand-then-tag, never guess later).
-- The NOT EXISTS guard makes a re-run a no-op once ANY image carries a
-- project tag: rows that arrived NULL after the original run (ingest lag)
-- stay untagged and are dropped by filter_images_by_project instead of
-- being mislabelled Camellia.
UPDATE images SET project_key = 'camellia'
WHERE project_key IS NULL
  AND NOT EXISTS (SELECT 1 FROM images WHERE project_key IS NOT NULL);

COMMIT;
