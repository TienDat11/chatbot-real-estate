-- rag-real-estate — Migration 2026-09-09: multi-device identity linking.
-- Forward-only, idempotent, and data-preserving (expand-and-contract).
--
-- Context: one Firebase account may be signed in from several devices or
-- browsers. The anonymous identity token is stored per browser, so EVERY
-- device carries its OWN anon identity key, and each of them must be able to
-- link into the shared account. The 2026-08-25 model declared
-- identity_links.firebase_uid UNIQUE, i.e. exactly one anon identity per
-- account; the second device's link therefore conflicted and never merged its
-- pre-login quota usage or chat history into the account identity.
--
-- Change: drop the UNIQUE constraint so the mapping becomes
-- N anon_identity_key -> 1 firebase_uid. The PRIMARY KEY stays on
-- anon_identity_key, which is what keeps one anon identity bound to exactly
-- ONE account (rebinding an already-linked anon key to a different uid stays a
-- hard conflict, answered 409 by POST /api/auth/link-anon).
--
-- Existing rows are never modified or evicted: the first device's link row is
-- preserved, and later devices simply add theirs.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.identity_links') IS NOT NULL THEN
    -- Postgres auto-names a column UNIQUE constraint <table>_<column>_key.
    ALTER TABLE identity_links
      DROP CONSTRAINT IF EXISTS identity_links_firebase_uid_key;
  END IF;
END
$$;

COMMIT;
