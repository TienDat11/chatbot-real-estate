-- rag-real-estate — Audit tables (append-only, AD-10). Plan §4.8:
-- trace_id, routing + structured_path, sql_spec (literal-redacted), sql_query R2 (redacted),
-- fact_ids, chunk_ids, rerank scores, prompt hash, model + version, answer hash, confidence,
-- guard verdicts, degraded. Run after db/schema.sql.

-- query_audit — one row per query, written AFTER the pipeline finishes (in a finally block)
CREATE TABLE IF NOT EXISTS query_audit (
  id              BIGSERIAL PRIMARY KEY,
  trace_id        TEXT NOT NULL UNIQUE,
  session_id      TEXT,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  query           TEXT NOT NULL,                 -- raw user query
  rewritten_query TEXT,
  routing         JSONB NOT NULL DEFAULT '{}',   -- needs_rag/needs_sql/high_stakes
  structured_path TEXT,                          -- spec | nl2sql | none
  sql_spec        JSONB,                         -- REDACTED literals (AD-10)
  sql_query       TEXT,                          -- R2: redacted
  fact_ids        BIGINT[],
  chunk_ids       TEXT[],
  rerank_scores   JSONB,
  prompt_hash     TEXT,                          -- sha256 of prompt sent to the LLM
  model           TEXT,
  model_version   TEXT,
  answer_hash     TEXT,                          -- sha256 of answer (replay matching)
  confidence      TEXT,                          -- HIGH/MEDIUM/LOW
  guard_verdicts  JSONB NOT NULL DEFAULT '{}',   -- per-guard L1-L4 pass/fail
  degraded        JSONB NOT NULL DEFAULT '{}',   -- leg-degraded flag + reason
  latency_ms      INT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_query_audit_ts ON query_audit (ts DESC);
CREATE INDEX IF NOT EXISTS idx_query_audit_session ON query_audit (session_id);

-- Append-only: role audit_append is INSERT + SELECT only, no update/delete/truncate
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audit_append') THEN
    CREATE ROLE audit_append NOLOGIN;
  END IF;
END
$$;

REVOKE ALL ON query_audit FROM PUBLIC;
GRANT INSERT, SELECT ON query_audit TO audit_append;
GRANT USAGE, SELECT ON SEQUENCE query_audit_id_seq TO audit_append;

-- App role (ragre) also gets INSERT (the app itself writes the audit)
GRANT INSERT, SELECT ON query_audit TO ragre;
GRANT USAGE, SELECT ON SEQUENCE query_audit_id_seq TO ragre;

REVOKE UPDATE, DELETE, TRUNCATE ON query_audit FROM ragre;
REVOKE UPDATE, DELETE, TRUNCATE ON query_audit FROM audit_append;

-- Notes:
--  * Never store secrets/API keys — audit keeps only hashes + metadata.
--  * sql_spec/sql_query: the app redacts sensitive numeric/string literals BEFORE writing.
--  * Replay: query_audit(answer_hash) is compared against the actual answer to detect tampering.
