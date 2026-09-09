-- Atomic lead mutation support. Safe to run repeatedly.
-- Advisory locks serialize only the same normalized phone; no permanent phone uniqueness.
CREATE INDEX IF NOT EXISTS idx_leads_phone_created_at ON leads (phone, created_at DESC);
