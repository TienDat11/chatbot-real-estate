-- Project image namespace migration (idempotent).
-- The project predicate is already part of image retrieval; this migration makes
-- the storage contract explicit and gives existing rows a reviewable audit trail.
BEGIN;

ALTER TABLE images ADD COLUMN IF NOT EXISTS project_key TEXT;

CREATE INDEX IF NOT EXISTS idx_images_project_kind_status
  ON images (project_key, kind, status) WHERE status = 'published';

-- Existing Camellia rows predate project tagging. Only rows with the legacy
-- source convention are backfilled; unknown rows remain NULL for human review.
UPDATE images
SET project_key = 'camellia'
WHERE project_key IS NULL
  AND source_file LIKE 'data/_processed/raw/%';

COMMIT;
