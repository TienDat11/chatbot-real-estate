-- rag-real-estate — Migration 2026-08-25: widen facts.unit enum with 'm' and 'count'.
--
-- Why: the extraction LLM (qwen3.8-max) legitimately emits unit 'm' for
-- distance facts (e.g. "Khoảng cách giữa hai toà A1 và A2 là 40m") and unit
-- 'count' for tower-count facts (e.g. "Tổ hợp Ánh Dương – Soleil gồm 4 tháp").
-- The previous enum ('vnd','m2','pct','months','days','enum') rejected both,
-- so extract_facts raised, load_document loaded chunks only (facts=0), and
-- every non-seed Soleil/Camellia document lost its extracted facts on the
-- 08-24 re-ingest wave. This migration widens the DB CHECK so those facts can
-- land; ingest/fact_extract.py FACT_UNITS + _normalize_unit() must stay in
-- lockstep (see module docstring).
--
-- Idempotent: DROP CONSTRAINT IF EXISTS + re-ADD on the same line is safe to
-- re-run; a fresh schema (db/schema.sql) already carries the widened set.

BEGIN;

ALTER TABLE facts DROP CONSTRAINT IF EXISTS facts_unit_check;

ALTER TABLE facts
  ADD CONSTRAINT facts_unit_check
  CHECK (unit IN ('vnd', 'm2', 'pct', 'months', 'days', 'enum', 'm', 'count'));

COMMIT;
