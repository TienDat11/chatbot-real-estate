-- rag-real-estate — Schema v2: run BEFORE any ingest. Requires PostgreSQL 16.6+,
-- pgvector >= 0.8, LightRAG 1.5.6. Plan refs: .claude/plans/rag-real-estate-final.plan.md §3.3 + §3.4 + §5 (L3 guardrails).
-- Why: facts (price/policy/area) live OUTSIDE the vector index (vector = meaning, SQL = numbers);
-- intervals are half-open [effective_from, effective_to), NULL effective_to = open-ended;
-- numeric discipline (AD-14): money NUMERIC(20,0), pct NUMERIC(5,2), interest NUMERIC(6,4), no float/double, NULL != 0;
-- RLS ENABLE+FORCE on registry tables with a static published-doc SELECT policy; LightRAG graph/vector tables self-create.

-- 0. Extensions
CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector (LightRAG vector storage)
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- interval exclusion constraint
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- sha256() for seed content hashing

-- 1. documents — registry metadata contract (v2); one row per document/price table/campaign doc
CREATE TABLE IF NOT EXISTS documents (
  id            BIGSERIAL PRIMARY KEY,
  -- Stable human-readable id: 'ldd-2024' | 'price-tower-a-2026q3'
  doc_id        TEXT        NOT NULL UNIQUE,
  kind          TEXT        NOT NULL CHECK (kind IN ('legal', 'price', 'project')),
  title         TEXT        NOT NULL,
  source_file   TEXT        NOT NULL,                 -- original file (PDF/Word/JSON)
  effective_from DATE       NOT NULL,                 -- half-open [from, to)
  effective_to   DATE,                                -- NULL = open-ended interval
  status        TEXT        NOT NULL DEFAULT 'published'
                CHECK (status IN ('published', 'expired', 'deprecated')),
  content_hash  TEXT        NOT NULL,                 -- sha256 of original file (AD-7)
  version       INT         NOT NULL DEFAULT 1,       -- bumped on re-import of a new version
  lightrag_doc_id TEXT,                               -- A2: id in LightRAG (1:1 with doc_id)
  project_key    TEXT,                               -- project registry key ('camellia'|'soleil'|...)
  metadata      JSONB       NOT NULL DEFAULT '{}',    -- per-kind attributes (data-contract.md)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_effective
  ON documents (status, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents (kind);
-- Project-scoped lookups: active docs for one project (story 8.2 registry)
CREATE INDEX IF NOT EXISTS idx_documents_project_kind_status
  ON documents (project_key, kind, status);

-- 2. document_chunks — provenance + text_hash (B3); maps chunk -> doc so the
--    validity filter can drop expired chunks (the vector store is LightRAG-managed)
CREATE TABLE IF NOT EXISTS document_chunks (
  id          BIGSERIAL PRIMARY KEY,
  doc_id      TEXT NOT NULL REFERENCES documents (doc_id) ON DELETE CASCADE,
  -- Content-stable id: doc_id:version:index — unchanged when other chunks are added (AD-7)
  chunk_id    TEXT NOT NULL UNIQUE,
  chunk_index INT  NOT NULL,
  content     TEXT NOT NULL,
  text_hash   TEXT NOT NULL,                          -- SHA-256 of content (B3)
  section     TEXT,                                   -- article/clause (legal) or floor/unit (price)
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks (doc_id);

-- 11. images — image metadata registry (vision not wired yet, so caption stays nullable)
CREATE TABLE IF NOT EXISTS images (
  id                 BIGSERIAL PRIMARY KEY,
  -- Stable id: 'matbang-trang-06' | 'banggia-trang-1' | 'toroi-trang-1' | 'thanh-toan-chuan'
  image_id           TEXT NOT NULL UNIQUE,
  kind               TEXT NOT NULL CHECK (kind IN ('matbang', 'banggia', 'toroi', 'thanh-toan', 'phaply', 'qna', 'other')),
  title              TEXT NOT NULL,
  caption            TEXT,
  alt_text           TEXT,
  url_cdn            TEXT NOT NULL UNIQUE,
  width              INT,
  height             INT,
  content_hash       TEXT NOT NULL,       -- sha256 of the original file
  status             TEXT NOT NULL DEFAULT 'published'
                     CHECK (status IN ('published', 'expired', 'deprecated')),
  source_file        TEXT,
  doc_id             TEXT REFERENCES documents(doc_id) ON DELETE CASCADE,
  chunk_id           TEXT REFERENCES document_chunks(chunk_id) ON DELETE CASCADE,
  linked_subject_key TEXT,
  project_key        TEXT,                  -- project scoping for image enrichment (story 10.4)
  metadata           JSONB NOT NULL DEFAULT '{}',
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_images_kind ON images (kind);
CREATE INDEX IF NOT EXISTS idx_images_doc ON images (doc_id);
CREATE INDEX IF NOT EXISTS idx_images_link ON images (linked_subject_key);
CREATE INDEX IF NOT EXISTS idx_images_status ON images (status) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_images_project_status
  ON images (project_key, status) WHERE status = 'published';

-- 12. image_embeddings — per-image vector store (cosine); dims 1024 = text-embedding-v4 LOCK
CREATE TABLE IF NOT EXISTS image_embeddings (
  embedding_id BIGSERIAL PRIMARY KEY,
  image_id     TEXT NOT NULL REFERENCES images(image_id) ON DELETE CASCADE,
  caption_hash TEXT NOT NULL,             -- sha256(caption) to dedupe identical embedded captions
  embedding    vector(1024) NOT NULL,
  model        TEXT NOT NULL DEFAULT 'text-embedding-v4',
  dims         INT NOT NULL DEFAULT 1024,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_image_emb_hnsw
  ON image_embeddings USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_image_emb_img ON image_embeddings (image_id, caption_hash);

-- 13. project_config — per-project registry hub (story 8.2 / master plan D4).
--     One row per project key; carries the display/legal identity, geo center
--     (replaces the hardcoded Camellia coordinates in config), hotline, the
--     R2 video/media bundle the greeting widget plays, and sales-kit/persona
--     file names. status='inactive' hides the project from the frontend switcher
--     and the media endpoint; publish_at stamps when it went live.
CREATE TABLE IF NOT EXISTS project_config (
  project_key     TEXT PRIMARY KEY,
  ten_phap_ly     TEXT NOT NULL,
  ten_thuong_mai  TEXT NOT NULL,
  vi_tri          TEXT NOT NULL,
  -- Story 10.3: detailed Vietnamese address served to the FE project picker
  -- (falls back to vi_tri for rows seeded before the location migration).
  location        TEXT,
  -- HOT-project flag: marks Camellia so the picker leads with it (is_hot DESC).
  is_hot          BOOLEAN NOT NULL DEFAULT false,
  geo_center_lat  DOUBLE PRECISION,
  geo_center_lng  DOUBLE PRECISION,
  hotline         TEXT,
  media           JSONB NOT NULL DEFAULT '[]',    -- R2 video/media contract (media_config list)
  sales_kit_file  TEXT,
  persona_file    TEXT,
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'inactive')),
  publish_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. campaigns — price / loan-policy periods (B7: price & policy MUST belong to a campaign)
CREATE TABLE IF NOT EXISTS campaigns (
  id            BIGSERIAL PRIMARY KEY,
  campaign_key  TEXT NOT NULL UNIQUE,                 -- 'tower-a-2026q3'
  project_key   TEXT NOT NULL,
  effective_from DATE NOT NULL,
  effective_to   DATE,                                -- NULL = open-ended interval
  source_doc_id TEXT NOT NULL REFERENCES documents(doc_id),  -- fail-loud when the doc is deleted (A7)
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. fact_subjects — fact subject (unit/parcel/project/taxon) deduped by subject_key
CREATE TABLE IF NOT EXISTS fact_subjects (
  id           BIGSERIAL PRIMARY KEY,
  -- 'unit:tower-a/A10-01' | 'tax:le-phi-truoc-ba'
  subject_key  TEXT NOT NULL UNIQUE,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('unit', 'parcel', 'project', 'legal_fact', 'taxon')),
  display_name TEXT NOT NULL,
  project_key  TEXT,
  attrs        JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. facts — core table: interval validity + policy_key + quality (exact/range/approx)
CREATE TABLE IF NOT EXISTS facts (
  id            BIGSERIAL PRIMARY KEY,
  subject_id    BIGINT NOT NULL REFERENCES fact_subjects(id) ON DELETE CASCADE,
  fact_key      TEXT NOT NULL,              -- 'price_vnd'|'deposit_pct'|'term_months'|'interest_rate_pct'|...
  policy_key    TEXT,                       -- 'bank_a'|'bank_b'|'support' (a unit may carry several policies)
  campaign_key  TEXT REFERENCES campaigns(campaign_key),  -- price/policy MUST belong to a campaign (B7)
  value_num     NUMERIC,
  value_text    TEXT,
  unit          TEXT NOT NULL CHECK (unit IN ('vnd', 'm2', 'pct', 'months', 'days', 'enum')),
  quality       TEXT NOT NULL DEFAULT 'exact' CHECK (quality IN ('exact', 'range', 'approx')),
  range_min     NUMERIC,
  range_max     NUMERIC,
  volatile      BOOLEAN NOT NULL DEFAULT true,
  effective_from DATE NOT NULL,
  effective_to   DATE,                     -- NULL = open-ended interval; half-open '[)'
  source_doc_id   TEXT NOT NULL REFERENCES documents(doc_id),
  source_chunk_id TEXT REFERENCES document_chunks(chunk_id),
  extract_conf REAL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_pct CHECK (unit <> 'pct' OR value_num IS NULL OR (value_num >= 0 AND value_num <= 100)),
  CONSTRAINT chk_vnd CHECK (unit <> 'vnd' OR value_num IS NULL OR value_num > 0)
);

-- Prevent overlapping validity intervals: same (subject, fact_key, policy_key) must not overlap in time
ALTER TABLE facts DROP CONSTRAINT IF EXISTS facts_no_overlap;
ALTER TABLE facts ADD CONSTRAINT facts_no_overlap
  EXCLUDE USING gist (
    subject_id WITH =,
    fact_key WITH =,
    COALESCE(policy_key, '') WITH =,
    daterange(effective_from, COALESCE(effective_to, 'infinity'::date), '[)') WITH &&
  );

CREATE INDEX IF NOT EXISTS idx_facts_lookup
  ON facts (subject_id, fact_key, policy_key) WHERE effective_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_value ON facts (fact_key, value_num);

-- 6. chunk_fact_refs — which chunk references which fact (placeholder tracking)
CREATE TABLE IF NOT EXISTS chunk_fact_refs (
  chunk_id TEXT NOT NULL REFERENCES document_chunks(chunk_id) ON DELETE CASCADE,
  fact_id  BIGINT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  PRIMARY KEY (chunk_id, fact_id)
);

-- 7. fact_aliases — normalize categorical values to canonicals (e.g. 'in effect' -> 'active')
CREATE TABLE IF NOT EXISTS fact_aliases (
  alias     TEXT NOT NULL,
  canonical TEXT NOT NULL,
  field     TEXT NOT NULL,
  PRIMARY KEY (field, alias)
);

-- 8. v_unit_offers — derived cross-row offer (AD-14), core "2 billion VND" case.
--    security_invoker=true: runs as the caller, no RLS bypass (CRITICAL fix).
--    Not a materialized view in MVP. Logic lives in v_unit_offers_as_of(as_of) so
--    historical as_of binds; the view keeps CURRENT_DATE for existing consumers.
CREATE OR REPLACE FUNCTION v_unit_offers_as_of(as_of date)
RETURNS TABLE (
  subject_id bigint,
  policy_key text,
  price_vnd numeric(20,0),
  deposit_pct numeric(5,2),
  term_months integer,
  interest_rate_pct numeric(6,4),
  required_down_payment_vnd numeric(20,0),
  loan_amount_vnd numeric(20,0),
  monthly_principal_vnd numeric(20,0),
  monthly_interest_estimate_vnd numeric(20,0)
)
LANGUAGE sql
STABLE
AS $$
  WITH cur AS (
    SELECT subject_id, policy_key, fact_key, value_num FROM facts
    WHERE effective_from <= as_of
      AND (effective_to IS NULL OR effective_to > as_of) AND quality = 'exact'
  )
  SELECT pol.subject_id, pol.policy_key,
    price.value_num::NUMERIC(20,0) AS price_vnd,
    dep.value_num::NUMERIC(5,2)    AS deposit_pct,
    term.value_num::INTEGER        AS term_months,
    int_pct.value_num::NUMERIC(6,4) AS interest_rate_pct,  -- NULL = not offered (distinct from 0%)
    CEIL(price.value_num * dep.value_num / 100.0)::NUMERIC(20,0) AS required_down_payment_vnd, -- CEIL
    ROUND(price.value_num * (100.0 - dep.value_num) / 100.0, 0)::NUMERIC(20,0) AS loan_amount_vnd,
    ROUND((price.value_num * (100.0 - dep.value_num) / 100.0) / NULLIF(term.value_num, 0), 0)::NUMERIC(20,0) AS monthly_principal_vnd,
    ROUND((price.value_num * (100.0 - dep.value_num) / 100.0) * int_pct.value_num / 100.0 / 12.0, 0)::NUMERIC(20,0) AS monthly_interest_estimate_vnd -- ESTIMATE on initial principal outstanding
  FROM (SELECT DISTINCT subject_id, policy_key FROM cur WHERE policy_key IS NOT NULL) pol
  JOIN cur dep   ON dep.subject_id = pol.subject_id AND dep.policy_key = pol.policy_key AND dep.fact_key = 'deposit_pct'
  JOIN cur term  ON term.subject_id = pol.subject_id AND term.policy_key = pol.policy_key AND term.fact_key = 'term_months'
  LEFT JOIN cur int_pct ON int_pct.subject_id = pol.subject_id AND int_pct.policy_key = pol.policy_key AND int_pct.fact_key = 'interest_rate_pct'
  JOIN cur price ON price.subject_id = pol.subject_id AND price.fact_key = 'price_vnd'
$$;

CREATE OR REPLACE VIEW v_unit_offers
WITH (security_invoker = true) AS
SELECT * FROM v_unit_offers_as_of(CURRENT_DATE);

-- 9. ingest_log — data-loading journal
CREATE TABLE IF NOT EXISTS ingest_log (
  id          BIGSERIAL PRIMARY KEY,
  doc_id      TEXT NOT NULL,
  action      TEXT NOT NULL CHECK (action IN ('insert', 'update', 'expire', 'delete')),
  version     INT  NOT NULL,
  chunk_count INT  NOT NULL DEFAULT 0,
  detail      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 10. review_queue — low-confidence fact extraction + high-stakes review
CREATE TABLE IF NOT EXISTS review_queue (
  id          BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('fact_extract', 'high_stakes')),
  doc_id      TEXT,
  payload     JSONB NOT NULL DEFAULT '{}',   -- context + flagged values
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

-- ROLES + RLS (A4 + A9)
--   ragre        : owner, ingest (DDL + write)
--   ro_query     : SELECT-only, switched via SET LOCAL ROLE inside with_rls_identity()
--   audit_append : INSERT-only audit (append-only architecture)
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ro_query') THEN
    CREATE ROLE ro_query NOLOGIN;
  END IF;
END
$$;

-- RLS: ENABLE + FORCE on all registry tables
ALTER TABLE documents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents       FORCE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE campaigns       ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns       FORCE ROW LEVEL SECURITY;
ALTER TABLE fact_subjects   ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_subjects   FORCE ROW LEVEL SECURITY;
ALTER TABLE facts           ENABLE ROW LEVEL SECURITY;
ALTER TABLE facts           FORCE ROW LEVEL SECURITY;
ALTER TABLE chunk_fact_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk_fact_refs FORCE ROW LEVEL SECURITY;
ALTER TABLE images            ENABLE ROW LEVEL SECURITY;
ALTER TABLE images            FORCE ROW LEVEL SECURITY;
ALTER TABLE image_embeddings  ENABLE ROW LEVEL SECURITY;
ALTER TABLE image_embeddings  FORCE ROW LEVEL SECURITY;
ALTER TABLE project_config    ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_config    FORCE ROW LEVEL SECURITY;

-- SELECT policy: only rows backed by a published doc (MVP static)
DROP POLICY IF EXISTS facts_pub_select ON facts;
CREATE POLICY facts_pub_select ON facts FOR SELECT USING (
  EXISTS (SELECT 1 FROM documents d
          WHERE d.doc_id = facts.source_doc_id AND d.status = 'published'));

DROP POLICY IF EXISTS docs_pub_select ON documents;
CREATE POLICY docs_pub_select ON documents FOR SELECT USING (status = 'published');

DROP POLICY IF EXISTS chunks_pub_select ON document_chunks;
CREATE POLICY chunks_pub_select ON document_chunks FOR SELECT USING (
  EXISTS (SELECT 1 FROM documents d
          WHERE d.doc_id = document_chunks.doc_id AND d.status = 'published'));

DROP POLICY IF EXISTS subjects_pub_select ON fact_subjects;
CREATE POLICY subjects_pub_select ON fact_subjects FOR SELECT USING (true);

DROP POLICY IF EXISTS campaigns_pub_select ON campaigns;
CREATE POLICY campaigns_pub_select ON campaigns FOR SELECT USING (
  EXISTS (SELECT 1 FROM documents d
          WHERE d.doc_id = campaigns.source_doc_id AND d.status = 'published'));

DROP POLICY IF EXISTS refs_pub_select ON chunk_fact_refs;
CREATE POLICY refs_pub_select ON chunk_fact_refs FOR SELECT USING (
  EXISTS (SELECT 1 FROM document_chunks c
          JOIN documents d ON d.doc_id = c.doc_id
          WHERE c.chunk_id = chunk_fact_refs.chunk_id AND d.status = 'published'));

-- Write policies: only the owner/ingest role (ragre) may write — FORCE still applies to the owner
DROP POLICY IF EXISTS docs_write ON documents;
CREATE POLICY docs_write ON documents FOR ALL TO ragre USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS chunks_write ON document_chunks;
CREATE POLICY chunks_write ON document_chunks FOR ALL TO ragre USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS campaigns_write ON campaigns;
CREATE POLICY campaigns_write ON campaigns FOR ALL TO ragre USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS subjects_write ON fact_subjects;
CREATE POLICY subjects_write ON fact_subjects FOR ALL TO ragre USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS facts_write ON facts;
CREATE POLICY facts_write ON facts FOR ALL TO ragre USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS refs_write ON chunk_fact_refs;
CREATE POLICY refs_write ON chunk_fact_refs FOR ALL TO ragre USING (true) WITH CHECK (true);

-- Image SELECT policy: only published images (vision/embedding consumers read published only)
DROP POLICY IF EXISTS images_pub_select ON images;
CREATE POLICY images_pub_select ON images FOR SELECT USING (status = 'published');
DROP POLICY IF EXISTS image_emb_pub_select ON image_embeddings;
CREATE POLICY image_emb_pub_select ON image_embeddings FOR SELECT USING (
  EXISTS (SELECT 1 FROM images i
          WHERE i.image_id = image_embeddings.image_id AND i.status = 'published'));

-- project_config SELECT policy: only active (published) projects are visible
DROP POLICY IF EXISTS project_config_pub_select ON project_config;
CREATE POLICY project_config_pub_select ON project_config FOR SELECT USING (status = 'active');
DROP POLICY IF EXISTS project_config_write ON project_config;
CREATE POLICY project_config_write ON project_config FOR ALL TO ragre USING (true) WITH CHECK (true);

-- Image write policies: only the owner/ingest role (ragre) may write
DROP POLICY IF EXISTS images_write ON images;
CREATE POLICY images_write ON images FOR ALL TO ragre USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS image_emb_write ON image_embeddings;
CREATE POLICY image_emb_write ON image_embeddings FOR ALL TO ragre USING (true) WITH CHECK (true);

-- Query role: base SELECT grants on registry + view; RLS policies still filter rows.
-- GRANT ro_query TO ragre enables SET LOCAL ROLE ro_query inside with_rls_identity()
-- (without it the query leg fails with 'permission denied to set role').
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ragre') THEN
    EXECUTE 'GRANT ro_query TO ragre';
  END IF;
END
$$;
GRANT SELECT ON documents, document_chunks, campaigns, fact_subjects, facts,
  chunk_fact_refs, fact_aliases, v_unit_offers, images, image_embeddings,
  project_config TO ro_query;
GRANT EXECUTE ON FUNCTION v_unit_offers_as_of(date) TO ro_query;
GRANT USAGE ON SCHEMA public TO ro_query;

-- Deploy notes:
-- 1. Requires PostgreSQL 16.6+ (hard requirement for LightRAG 1.5.6).
-- 2. Run this schema BEFORE any ingest.
-- 3. LightRAG's PGTableGraphStorage / PGVectorStorage self-create graph + vector tables — leave them alone.
-- 4. Price updates: expire old facts + insert new facts + new campaign in ONE transaction (scripts/update_price.sh) — do not touch vectors.
-- 5. Audit tables: db/audit.sql (append-only role).
