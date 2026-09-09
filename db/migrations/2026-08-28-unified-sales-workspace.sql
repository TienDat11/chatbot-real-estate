-- rag-real-estate — Migration 2026-08-28: G3-r6 unified sales workspace persistence.
-- ISSUE-G4-01 / spec §10.3 (FR-32, FR-35, FR-37).
--
-- Durable, device-independent storage for two features only:
--   1. sales_notification_reads — persistent per-(sales, lead) notification read
--      state so unread counts survive reload and a second device. Contains NO
--      raw phone: identity is (sales_id, lead_id); masked lead data stays in the
--      Firestore projection, never in this table.
--   2. Training history on the EXISTING chat_sessions/chat_messages tables —
--      additive nullable columns only (owner_firebase_uid, answer_mode,
--      context_project_key). No existing customer row is rewritten or
--      reclassified: the training checks below are satisfied by all-NULL rows,
--      which is exactly the shape of every pre-existing customer session.
--
-- Invariants honoured:
--   * Additive and idempotent (IF NOT EXISTS / guarded constraint blocks);
--     safe to run repeatedly.
--   * leads rows are NOT altered. A read row for a lead that was later
--     reassigned is retained (FK ON DELETE RESTRICT) so notification history
--     cannot be silently removed by deleting a lead; visibility is enforced by
--     live-assignment checks in the service layer, not by destructive FKs.
--   * No vector column, index, extension version, or embedding value is
--     touched. Vector parity is asserted externally by
--     scripts/verify_vector_parity.py before and after this migration;
--     this file contains no repair path (any mismatch aborts upstream).
--   * chat_sessions retains the legacy customer contract
--     (identity_key OR device_id IS NOT NULL) — enforced WITHOUT scanning or
--     rewriting pre-existing rows (NOT VALID skips the validation pass).
--
-- Down-note (rollback — removes ONLY G3-r6 objects; see the DO block for the
-- dependents guard; never deletes session, lead, message, document, fact,
-- image, or vector rows):
--   BEGIN;
--   DROP TABLE IF EXISTS sales_notification_reads;
--   ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chk_sessions_customer_identity;
--   ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chk_sessions_training_fields;
--   ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chk_sessions_training_shape;
--   ALTER TABLE chat_sessions DROP COLUMN IF EXISTS context_project_key;
--   ALTER TABLE chat_sessions DROP COLUMN IF EXISTS answer_mode;
--   ALTER TABLE chat_sessions DROP COLUMN IF EXISTS owner_firebase_uid;
--   COMMIT;
--   -- idx_chat_sessions_training_* are dropped automatically with answer_mode.
-- Run a backup BEFORE this migration per the Epic 7 runbook, and execute
-- scripts/verify_vector_parity.py pre-flight (abort before mutation on any
-- mismatch) and post-flight (same expected values).

BEGIN;

-- ============================================================
-- 1. sales_notification_reads — persistent notification read state
-- ============================================================
CREATE TABLE IF NOT EXISTS sales_notification_reads (
  sales_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
  lead_id  BIGINT NOT NULL REFERENCES leads(id) ON DELETE RESTRICT,
  read_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (sales_id, lead_id)
);

-- FK support + mark-all-through scans: read rows of one salesperson by time.
CREATE INDEX IF NOT EXISTS idx_snr_sales_read_at
  ON sales_notification_reads (sales_id, read_at DESC);

-- Unread lookups are served by the PRIMARY KEY (sales_id, lead_id): the
-- per-salesperson anti-join against assigned leads is an index-only scan on
-- that prefix. lead_id alone is indexed for assignment-revocation cleanup
-- review (never used to delete rows implicitly).
CREATE INDEX IF NOT EXISTS idx_snr_lead
  ON sales_notification_reads (lead_id);

COMMENT ON TABLE sales_notification_reads IS
  'G3-r6: per-(sales, lead) notification read state. No raw phone; masked data only lives client-side.';

COMMIT;

BEGIN;

-- ============================================================
-- 2. chat_sessions — additive training-history columns (nullable)
-- ============================================================
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS owner_firebase_uid TEXT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS answer_mode TEXT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS context_project_key TEXT;

COMMENT ON COLUMN chat_sessions.answer_mode IS
  'NULL = customer/legacy row (never reclassified); ''training'' = private staff training session (G3-r6).';
COMMENT ON COLUMN chat_sessions.owner_firebase_uid IS
  'Verified Firebase UID owning a training session; NULL on every customer row.';
COMMENT ON COLUMN chat_sessions.context_project_key IS
  'Immutable project context of a training session; mirrors project_key on training rows.';

-- Constraints are added through guarded DO blocks so re-runs converge whether
-- the column is brand new or a previous run stopped mid-way. Existing rows are
-- all-NULL, so every check below is satisfied by the current data and by all
-- future customer rows.

-- Training-only: so far the only non-NULL mode is 'training'.
DO $$
BEGIN
  ALTER TABLE chat_sessions
    ADD CONSTRAINT chk_sessions_answer_mode_training_only
    CHECK (answer_mode IS NULL OR answer_mode = 'training');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Training shape: training rows carry owner + context together; a customer
-- row may never be partially marked as training.
DO $$
BEGIN
  ALTER TABLE chat_sessions
    ADD CONSTRAINT chk_sessions_training_shape
    CHECK (answer_mode IS DISTINCT FROM 'training'
           OR (owner_firebase_uid IS NOT NULL AND context_project_key IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Context integrity: the recorded training context must equal the session's
-- own project_key, so a mixed-project transcript is structurally impossible.
DO $$
BEGIN
  ALTER TABLE chat_sessions
    ADD CONSTRAINT chk_sessions_training_fields
    CHECK (answer_mode IS NULL OR context_project_key IS NULL
           OR context_project_key = project_key);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Customer contract on the shared table: every customer (non-training) row
-- must keep an anonymous handle. IS NOT DISTINCT FROM keeps the expression
-- three-valued-safe (a plain equality would let NULL answer_mode pass the
-- CHECK by evaluating to NULL). NOT VALID skips the validation scan over
-- pre-existing rows; subsequent writes are checked, and the pending phase is
-- validated when the API lane confirms it is clean.
DO $$
BEGIN
  ALTER TABLE chat_sessions
    ADD CONSTRAINT chk_sessions_customer_identity
    CHECK (answer_mode IS NOT DISTINCT FROM 'training'
           OR identity_key IS NOT NULL OR device_id IS NOT NULL)
    NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Private training history list (owner, newest-first cursor) and
-- owner+project filter. Partial: only training rows enter the index.
CREATE INDEX IF NOT EXISTS idx_chat_sessions_training_owner_recent
  ON chat_sessions(owner_firebase_uid, last_active_at DESC, session_id)
  WHERE answer_mode = 'training';

CREATE INDEX IF NOT EXISTS idx_chat_sessions_training_owner_project
  ON chat_sessions(owner_firebase_uid, context_project_key)
  WHERE answer_mode = 'training';

COMMIT;
