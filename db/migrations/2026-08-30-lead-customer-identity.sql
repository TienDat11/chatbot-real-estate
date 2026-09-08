-- Server-side recipient resolution for FCM call-started pushes.
-- customer_identity stores the verified anonymous identity subject (uuid4 from
-- the signed anon token) when present, else the HMAC-SHA256 phone digest the
-- realtime mirror already uses — never the raw phone.
BEGIN;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS customer_identity TEXT;
CREATE INDEX IF NOT EXISTS idx_leads_customer_identity
  ON leads (customer_identity)
  WHERE customer_identity IS NOT NULL;
COMMIT;
