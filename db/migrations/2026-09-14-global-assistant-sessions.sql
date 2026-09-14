-- rag-real-estate — Migration 2026-09-14: Global Assistant session persistence (GA-03).
--
-- Additive only: extends chat_sessions with the session-mode and active-project
-- bookkeeping needed for a single global assistant session that can carry turns
-- scoped to different projects.
--
-- Invariants:
--   * No existing table, column, or row is rewritten or dropped.
--   * Existing rows keep their legacy behaviour: session_mode = 'project',
--     active_project_key = NULL.
--   * '_global' is the reserved container key for a global parent session; it is
--     not a corpus project and must never be served as one.
--   * Per-message scope is the existing nullable chat_messages.project_key
--     (NULL = neutral clarification, 'camellia' = resolved Camellia, etc.).
--
-- Down-note (rollback — removes ONLY GA-03 objects):
--   BEGIN;
--   ALTER TABLE chat_sessions DROP COLUMN IF EXISTS session_mode;
--   ALTER TABLE chat_sessions DROP COLUMN IF EXISTS active_project_key;
--   COMMIT;

BEGIN;

ALTER TABLE chat_sessions
  ADD COLUMN IF NOT EXISTS session_mode TEXT NOT NULL DEFAULT 'project';

ALTER TABLE chat_sessions
  ADD COLUMN IF NOT EXISTS active_project_key TEXT NULL;

COMMIT;
