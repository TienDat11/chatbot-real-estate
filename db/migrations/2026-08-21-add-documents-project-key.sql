-- rag-real-estate — Migration 2026-08-21: add documents.project_key + project_config registry.
-- Story 8.2 (ISSUE-01). Runs against an EXISTING database (already seeded); fresh installs get
-- the same shape from db/schema.sql, so this migration is a no-op there (all guarded by IF NOT EXISTS /
-- DO blocks).
--
-- 3-step expand-contract (per master plan §0.4 D5 + [RV-22/08]):
--   step 1: add project_key as NULLABLE (no table rewrite, no lock on the column set)
--   step 2: backfill from campaigns (documents.source_doc_id -> campaigns.source_doc_id join);
--           docs with no campaign -> '_legacy' + a review_queue row (human must re-tag them)
--   step 3: set NOT NULL (all rows now carry a key)
--
-- Reserved keys (D5): '_legacy' = untagged legacy docs awaiting review; '_training' = training
-- namespace (story 8.6). Real projects must never use a leading underscore.
--
-- [LR-22/08] Down-note (rollback, step by step):
--   1. DROP the project_config table + the documents.project_key column + its index (see bottom).
--   2. git revert the media_config.py / config.py refactor (hardcoded Camellia videos/geo center).
--   3. re-run the pre-8.2 seed (db/seed/camellia_rumor.sql + db/seed/soleil_campaign.sql).
--   Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

-- ---------------------------------------------------------------------------
-- Step 1: add project_key as nullable
-- ---------------------------------------------------------------------------
ALTER TABLE documents ADD COLUMN IF NOT EXISTS project_key TEXT;

-- ---------------------------------------------------------------------------
-- Step 2a: backfill project_key from campaigns (documents carry the source doc
-- for a campaign, so joining on source_doc_id recovers the project of every
-- campaign-backed document). One doc may back several campaigns — they all
-- belong to the same project in the current data, so COALESCE picks one key.
-- ---------------------------------------------------------------------------
UPDATE documents d
SET project_key = sub.project_key
FROM (
  SELECT DISTINCT ON (c.source_doc_id)
         c.source_doc_id, c.project_key
  FROM campaigns c
  WHERE c.project_key IS NOT NULL
  ORDER BY c.source_doc_id, c.effective_from DESC
) sub
WHERE d.doc_id = sub.source_doc_id
  AND d.project_key IS NULL;

-- ---------------------------------------------------------------------------
-- Step 2b: documents still without a project_key have no campaign (untagged
-- legacy corpus) — mark them '_legacy' and open a review_queue ticket so a
-- human re-tags them instead of silently guessing.
-- ---------------------------------------------------------------------------
UPDATE documents
SET project_key = '_legacy'
WHERE project_key IS NULL;

INSERT INTO review_queue (kind, doc_id, payload, status)
SELECT 'fact_extract', doc_id,
       jsonb_build_object('reason', 'documents.project_key migration: no campaign join -> _legacy',
                          'migration', '2026-08-21-add-documents-project-key'),
       'open'
FROM documents
WHERE project_key = '_legacy'
  AND NOT EXISTS (
    SELECT 1 FROM review_queue rq
    WHERE rq.doc_id = documents.doc_id
      AND rq.payload->>'migration' = '2026-08-21-add-documents-project-key'
  );

-- ---------------------------------------------------------------------------
-- Step 3: all rows now carry a key — enforce NOT NULL.
-- ---------------------------------------------------------------------------
ALTER TABLE documents ALTER COLUMN project_key SET NOT NULL;

-- Project-scoped lookups (active docs for one project).
CREATE INDEX IF NOT EXISTS idx_documents_project_kind_status
  ON documents (project_key, kind, status);

-- ---------------------------------------------------------------------------
-- project_config registry table (mirror of db/schema.sql section 13).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_config (
  project_key     TEXT PRIMARY KEY,
  ten_phap_ly     TEXT NOT NULL,
  ten_thuong_mai  TEXT NOT NULL,
  vi_tri          TEXT NOT NULL,
  geo_center_lat  DOUBLE PRECISION,
  geo_center_lng  DOUBLE PRECISION,
  hotline         TEXT,
  media           JSONB NOT NULL DEFAULT '[]',
  sales_kit_file  TEXT,
  persona_file    TEXT,
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'inactive')),
  publish_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE project_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_config FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS project_config_pub_select ON project_config;
CREATE POLICY project_config_pub_select ON project_config FOR SELECT USING (status = 'active');
DROP POLICY IF EXISTS project_config_write ON project_config;
CREATE POLICY project_config_write ON project_config FOR ALL TO ragre USING (true) WITH CHECK (true);

GRANT SELECT ON project_config TO ro_query;

COMMIT;

-- ---------------------------------------------------------------------------
-- [LR-22/08] ROLLBACK (run manually, step by step, AFTER a backup):
--   BEGIN;
--   ALTER TABLE documents ALTER COLUMN project_key DROP NOT NULL;
--   DROP INDEX IF EXISTS idx_documents_project_kind_status;
--   ALTER TABLE documents DROP COLUMN IF EXISTS project_key;
--   DROP TABLE IF EXISTS project_config;
--   COMMIT;
-- Then git revert the media_config.py/config.py refactor and re-run the legacy seeds.
-- ---------------------------------------------------------------------------
