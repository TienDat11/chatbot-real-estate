-- Atomic lead reassignment support (no_answer / LRU routing).
-- Safe to run repeatedly; forward-only, nothing destructive.
--
-- Correctness of the atomic assignment decision lives in the application
-- transaction (per-lead row lock via SELECT ... FOR UPDATE closes the
-- TOCTOU between ownership predicate, candidate selection and log
-- insertion). This migration only adds the supporting access paths:
--   1. Composite (lead_id, sales_id) so the tried-sales NOT EXISTS probe and
--      the candidate join resolve without two single-column index hops.
--   2. A per-lead advisory-lock registration is NOT needed; the row lock
--      already serializes concurrent decisions for the same lead.
CREATE INDEX IF NOT EXISTS idx_sal_log_lead_sales
  ON sales_assignment_log (lead_id, sales_id, created_at);

-- Lower-cost LRU probe for candidate ordering: per-sales assign/escalate
-- recency is queried for every candidate, so a filtered partial index beats
-- the full non-partial (sales_id) index on hot history.
CREATE INDEX IF NOT EXISTS idx_sal_sales_assign_recency
  ON sales_assignment_log (sales_id, created_at DESC)
  WHERE action IN ('assign', 'escalate');