-- rag-real-estate — Migration 2026-08-23: durable staff audit log.
-- Story 9.5 (ISSUE-11). Runs against an EXISTING database; fresh installs get
-- the same shape from db/lead_schema.sql (CREATE TABLE IF NOT EXISTS + indexes).
--
-- Replaces the stdout-only audit trail (logger.info on phone reveal) with a
-- queryable PG table covering every BE-mediated staff mutation: phone reveal,
-- CRM lead status change, marketing-consent withdrawal, re-approach run
-- trigger. Actor identity comes from the verified Firebase principal (uid +
-- role claim, story 8.3); actor_sales_id is the PG mapping when the actor is
-- a sales. detail is JSONB for action-specific context (old/new status,
-- queued counts) — it must NEVER carry raw PII (phones, names).
--
-- [LR-23/08] Down-note (rollback):
--   BEGIN;
--   DROP TABLE IF EXISTS staff_audit_log;
--   COMMIT;
-- Run a backup BEFORE this migration per the Epic 7 runbook.

BEGIN;

CREATE TABLE IF NOT EXISTS staff_audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_firebase_uid TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    actor_sales_id INTEGER REFERENCES sales(id),
    action TEXT NOT NULL,
    customer_id TEXT,
    lead_id INTEGER,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Admin audit views page newest-first; actor-scoped reviews filter by uid.
CREATE INDEX IF NOT EXISTS idx_staff_audit_created_at ON staff_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_staff_audit_actor ON staff_audit_log (actor_firebase_uid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_staff_audit_lead ON staff_audit_log (lead_id) WHERE lead_id IS NOT NULL;

COMMENT ON TABLE staff_audit_log IS 'Durable audit trail of staff mutations (story 9.5); detail JSONB must never contain raw PII';

COMMIT;
