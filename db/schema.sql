-- =============================================================================
-- rag-real-estate — Schema v2 (setup TRƯỚC khi có dữ liệu)
-- PostgreSQL 16.6+ | pgvector >= 0.8 | LightRAG 1.5.6
-- Plan: .claude/plans/rag-real-estate-final.plan.md §3.3 + §3.4 + §5 (L3 guardrails)
--
-- Nguyên tắc:
--  * Facts (giá/policy/diện tích) NGOÀI vector index — vector chứa nghĩa, SQL chứa số.
--  * Interval half-open [effective_from, effective_to) — NULL effective_to = còn hiệu lực.
--  * Kỷ luật kiểu số (AD-14): tiền NUMERIC(20,0); % điểm NUMERIC(5,2);
--    lãi suất NUMERIC(6,4); CẤM float/double. NULL ≠ 0.
--  * RLS ENABLE+FORCE trên registry tables; policy SELECT MVP STATIC (doc published).
--  * Bảng graph/vector của LightRAG (PGTableGraphStorage/PGVectorStorage) tự tạo.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 0. Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector (LightRAG vector storage)
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- interval exclusion constraint

-- ---------------------------------------------------------------------------
-- 1. documents — registry metadata contract (v2)
--    Mỗi dòng = 1 tài liệu/1 bảng giá/1 campaign document.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
  id            BIGSERIAL PRIMARY KEY,
  -- ID ổn định con người đọc được: 'ldd-2024' | 'price-tower-a-2026q3'
  doc_id        TEXT        NOT NULL UNIQUE,
  kind          TEXT        NOT NULL CHECK (kind IN ('legal', 'price', 'project')),
  title         TEXT        NOT NULL,
  source_file   TEXT        NOT NULL,                 -- file gốc (PDF/Word/JSON)
  effective_from DATE       NOT NULL,                 -- half-open [from, to)
  effective_to   DATE,                                -- NULL = còn hiệu lực
  status        TEXT        NOT NULL DEFAULT 'published'
                CHECK (status IN ('published', 'expired', 'deprecated')),
  content_hash  TEXT        NOT NULL,                 -- sha256 file gốc (AD-7)
  version       INT         NOT NULL DEFAULT 1,       -- tăng khi nạp lại bản mới
  lightrag_doc_id TEXT,                               -- A2: id trong LightRAG (1:1 doc_id)
  metadata      JSONB       NOT NULL DEFAULT '{}',    -- thuộc tính theo loại (data-contract.md)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_effective
  ON documents (status, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents (kind);

-- ---------------------------------------------------------------------------
-- 2. document_chunks — provenance + text_hash (B3)
--    LightRAG tự quản vector; bảng này là bản đồ chunk -> doc để lọc hiệu lực.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
  id          BIGSERIAL PRIMARY KEY,
  doc_id      TEXT NOT NULL REFERENCES documents (doc_id) ON DELETE CASCADE,
  -- ID ổn định nội dung: doc_id:version:index — không đổi khi thêm chunk khác (AD-7)
  chunk_id    TEXT NOT NULL UNIQUE,
  chunk_index INT  NOT NULL,
  content     TEXT NOT NULL,
  text_hash   TEXT NOT NULL,                          -- SHA-256 nội dung (B3)
  section     TEXT,                                   -- điều/khoản (pháp lý) hoặc tầng/căn (giá)
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks (doc_id);

-- ---------------------------------------------------------------------------
-- 3. campaigns — đợt giá / đợt chính sách vay (B7: giá/policy BẮT BUỘC gán campaign)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
  id            BIGSERIAL PRIMARY KEY,
  campaign_key  TEXT NOT NULL UNIQUE,                 -- 'tower-a-2026q3'
  project_key   TEXT NOT NULL,
  effective_from DATE NOT NULL,
  effective_to   DATE,                                -- NULL = còn hiệu lực
  source_doc_id TEXT NOT NULL REFERENCES documents(doc_id),  -- fail-loud khi xóa doc (A7)
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 4. fact_subjects — chủ thể của fact (căn hộ/thửa đất/dự án/tax) dedup theo subject_key
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- 5. facts — trái tim: interval-validity + policy_key + quality range/approx
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
  id            BIGSERIAL PRIMARY KEY,
  subject_id    BIGINT NOT NULL REFERENCES fact_subjects(id) ON DELETE CASCADE,
  fact_key      TEXT NOT NULL,              -- 'price_vnd'|'deposit_pct'|'term_months'|'interest_rate_pct'|...
  policy_key    TEXT,                       -- 'bank_a'|'bank_b'|'support' (nhiều policy/căn)
  campaign_key  TEXT REFERENCES campaigns(campaign_key),  -- giá/policy BẮT BUỘC gán campaign (B7)
  value_num     NUMERIC,
  value_text    TEXT,
  unit          TEXT NOT NULL CHECK (unit IN ('vnd', 'm2', 'pct', 'months', 'days', 'enum')),
  quality       TEXT NOT NULL DEFAULT 'exact' CHECK (quality IN ('exact', 'range', 'approx')),
  range_min     NUMERIC,
  range_max     NUMERIC,
  volatile      BOOLEAN NOT NULL DEFAULT true,
  effective_from DATE NOT NULL,
  effective_to   DATE,                     -- NULL = còn hiệu lực; half-open '[)'
  source_doc_id   TEXT NOT NULL REFERENCES documents(doc_id),
  source_chunk_id TEXT REFERENCES document_chunks(chunk_id),
  extract_conf REAL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_pct CHECK (unit <> 'pct' OR value_num IS NULL OR (value_num >= 0 AND value_num <= 100)),
  CONSTRAINT chk_vnd CHECK (unit <> 'vnd' OR value_num IS NULL OR value_num > 0)
);

-- Chặn interval chồng: (subject, fact_key, policy_key) không được 2 bản ghi hiệu lực đè nhau
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

-- ---------------------------------------------------------------------------
-- 6. chunk_fact_refs — chunk nào tham chiếu fact nào (placeholder tracking)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunk_fact_refs (
  chunk_id TEXT NOT NULL REFERENCES document_chunks(chunk_id) ON DELETE CASCADE,
  fact_id  BIGINT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  PRIMARY KEY (chunk_id, fact_id)
);

-- ---------------------------------------------------------------------------
-- 7. fact_aliases — normalize categorical: 'còn hiệu lực' -> 'active', ...
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_aliases (
  alias     TEXT NOT NULL,
  canonical TEXT NOT NULL,
  field     TEXT NOT NULL,
  PRIMARY KEY (field, alias)
);

-- ---------------------------------------------------------------------------
-- 8. v_unit_offers — derived cross-row (AD-14), trái tim case "2 tỉ".
--    security_invoker=true: chạy với quyền người gọi, không bypass RLS (fix CRITICAL).
--    KHÔNG materialized view ở MVP.
--    Logic lives in v_unit_offers_as_of(as_of) so historical as_of binds; the view
--    keeps CURRENT_DATE for existing consumers.
-- ---------------------------------------------------------------------------
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
RETURN (
  WITH cur AS (
    SELECT subject_id, policy_key, fact_key, value_num FROM facts
    WHERE effective_from <= as_of
      AND (effective_to IS NULL OR effective_to > as_of) AND quality = 'exact'
  )
  SELECT pol.subject_id, pol.policy_key,
    price.value_num::NUMERIC(20,0) AS price_vnd,
    dep.value_num::NUMERIC(5,2)    AS deposit_pct,
    term.value_num::INTEGER        AS term_months,
    int_pct.value_num::NUMERIC(6,4) AS interest_rate_pct,  -- NULL = chưa có (khác 0%)
    CEIL(price.value_num * dep.value_num / 100.0)::NUMERIC(20,0) AS required_down_payment_vnd, -- CEIL
    ROUND(price.value_num * (100.0 - dep.value_num) / 100.0, 0)::NUMERIC(20,0) AS loan_amount_vnd,
    ROUND((price.value_num * (100.0 - dep.value_num) / 100.0) / NULLIF(term.value_num, 0), 0)::NUMERIC(20,0) AS monthly_principal_vnd,
    ROUND((price.value_num * (100.0 - dep.value_num) / 100.0) * int_pct.value_num / 100.0 / 12.0, 0)::NUMERIC(20,0) AS monthly_interest_estimate_vnd -- ƯỚC TÍNH dư nợ gốc ban đầu
  FROM (SELECT DISTINCT subject_id, policy_key FROM cur WHERE policy_key IS NOT NULL) pol
  JOIN cur dep   ON dep.subject_id = pol.subject_id AND dep.policy_key = pol.policy_key AND dep.fact_key = 'deposit_pct'
  JOIN cur term  ON term.subject_id = pol.subject_id AND term.policy_key = pol.policy_key AND term.fact_key = 'term_months'
  LEFT JOIN cur int_pct ON int_pct.subject_id = pol.subject_id AND int_pct.policy_key = pol.policy_key AND int_pct.fact_key = 'interest_rate_pct'
  JOIN cur price ON price.subject_id = pol.subject_id AND price.fact_key = 'price_vnd'
);

CREATE OR REPLACE VIEW v_unit_offers
WITH (security_invoker = true) AS
SELECT * FROM v_unit_offers_as_of(CURRENT_DATE);

-- ---------------------------------------------------------------------------
-- 9. ingest_log — nhật ký nạp dữ liệu
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_log (
  id          BIGSERIAL PRIMARY KEY,
  doc_id      TEXT NOT NULL,
  action      TEXT NOT NULL CHECK (action IN ('insert', 'update', 'expire', 'delete')),
  version     INT  NOT NULL,
  chunk_count INT  NOT NULL DEFAULT 0,
  detail      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 10. review_queue — fact extraction low-conf (fact_review_queue) + HIGH-stakes review
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_queue (
  id          BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('fact_extract', 'high_stakes')),
  doc_id      TEXT,
  payload     JSONB NOT NULL DEFAULT '{}',   -- context + giá trị nghi ngờ
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

-- =============================================================================
-- ROLES + RLS (A4 + A9)
--   ragre        : owner, ingest (DDL + write)
--   ro_query     : SELECT-only, chạy qua SET LOCAL ROLE trong with_rls_identity()
--   audit_append : INSERT-only audit (kiến trúc append-only)
-- =============================================================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ro_query') THEN
    CREATE ROLE ro_query NOLOGIN;
  END IF;
END
$$;

-- RLS: ENABLE + FORCE trên toàn bộ registry tables
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

-- SELECT policy: chỉ thấy dữ liệu thuộc tài liệu published (MVP STATIC)
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

-- Write policies: chỉ role sở hữu / ingest (ragre) được ghi — FORCE vẫn áp cho owner
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

-- Query role: grant SELECT trên registry + view (quyền gốc; policy RLS sẽ lọc tiếp).
-- GRANT ro_query TO ragre: cho phép SET LOCAL ROLE ro_query trong with_rls_identity()
-- (thiếu grant này → 'permission denied to set role' khi chạy query leg).
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ragre') THEN
    EXECUTE 'GRANT ro_query TO ragre';
  END IF;
END
$$;
GRANT SET ON ROLE ro_query TO ragre;
GRANT SELECT ON documents, document_chunks, campaigns, fact_subjects, facts,
  chunk_fact_refs, fact_aliases, v_unit_offers TO ro_query;
GRANT EXECUTE ON FUNCTION v_unit_offers_as_of(date) TO ro_query;
GRANT USAGE ON SCHEMA public TO ro_query;

-- =============================================================================
-- LƯU Ý TRIỂN KHAI:
-- 1. LightRAG 1.5.6 cần PostgreSQL 16.6+ (bắt buộc).
-- 2. Chạy schema này TRƯỚC khi ingest bất kỳ dữ liệu nào.
-- 3. Bảng graph (entity/relation) + vector (chunk embedding) do
--    PGTableGraphStorage / PGVectorStorage của LightRAG tự tạo — không đụng.
-- 4. Update bảng giá mới: expire facts cũ + insert mới + campaign mới CÙNG 1 transaction
--    (xem scripts/update_price.sh) — KHÔNG đụng vector.
-- 5. Audit tables: db/audit.sql (append-only role).
-- =============================================================================
