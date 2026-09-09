-- 2026-09-03-crm-leads-page.sql
-- Purpose: supporting indexes for GET /api/crm/leads (plan issue
--   BE-LEADS-QUERY, spec FR-31 / contract C1).
--   idx_leads_crm_page     serves the sales-scoped listing: equality on the
--                          owner column then the keyset order
--                          (created_at DESC, id DESC) for cursor paging.
--   idx_leads_created_desc serves the unscoped admin listing with the same
--                          keyset order.
-- Repeat-safety: CREATE INDEX IF NOT EXISTS; additive only — no table, column
--   or data change. Re-running this file is a no-op.
-- Rollback:
--   DROP INDEX IF EXISTS idx_leads_crm_page;
--   DROP INDEX IF EXISTS idx_leads_created_desc;

CREATE INDEX IF NOT EXISTS idx_leads_crm_page
  ON leads (assigned_sales_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_leads_created_desc
  ON leads (created_at DESC, id DESC);
