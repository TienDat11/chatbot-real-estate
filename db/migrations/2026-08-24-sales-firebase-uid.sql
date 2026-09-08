-- rag-real-estate — Migration 2026-08-24: add sales.firebase_uid.
-- Secure-wave issue 4F: provisioning maps an Identity Toolkit account to its
-- PG sales row so staff tooling can resolve the verified Firebase uid without
-- relying on the legacy access_key==uid equality assumption.
--
-- Additive and NULLABLE on purpose: rows 1-5 predate Firebase accounts and a
-- NOT NULL would force a backfill that cannot be invented. One Firebase
-- account maps to at most one sales row, hence the UNIQUE partial index.
--
-- Down-note (rollback): the column is additive and nullable, so dropping it
-- is safe for any database that has not yet backfilled mappings:
--   BEGIN;
--   DROP INDEX IF EXISTS uq_sales_firebase_uid;
--   ALTER TABLE sales DROP COLUMN IF EXISTS firebase_uid;
--   COMMIT;

BEGIN;

ALTER TABLE sales ADD COLUMN IF NOT EXISTS firebase_uid TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sales_firebase_uid
  ON sales (firebase_uid) WHERE firebase_uid IS NOT NULL;

COMMIT;
