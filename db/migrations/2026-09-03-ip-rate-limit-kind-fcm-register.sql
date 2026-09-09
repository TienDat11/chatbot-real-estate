-- ip_rate_limit.kind gains 'fcm-register' so POST /api/notifications/device-token
-- can be rate limited per-IP like query/mint/lead. Forward-only repair of
-- ip_rate_limit_kind_check created by 2026-08-24-anon-quota-and-ip-rate-limit.sql
-- (which omitted the kind that notifications service now writes). Existing rows
-- remain valid because the new check is a strict superset of the old one.
--
-- Strict validation: the live constraint is compared semantically against
-- exactly two accepted states — the pre-migration narrow membership check
-- ('query','mint','lead') and the widened target ('query','mint','lead',
-- 'fcm-register'). Two conditions must hold, both derived from
-- pg_get_constraintdef(oid, true):
--   (a) canonical skeleton: quoted literals collapsed to '?' (their ::text
--       cast included), then any residual ::text cast, ALL parentheses, and
--       all whitespace stripped — must equal the exact membership-check
--       skeleton for 3 (narrow) or 4 (target) values. Stripping parentheses
--       also accepts the non-pretty rendering CHECK ((kind = ANY (ARRAY[...])))
--       and any balanced redundant wrapping, so a valid constraint is never
--       falsely rejected on formatting;
--   (b) the sorted list of quoted literals WITHOUT distinct-collapsing —
--       must equal the exact expected kind list, AND the total literal
--       count must equal its distinct count. A duplicated literal (e.g.
--       IN('query','mint','lead','query'), whose skeleton still matches
--       the target shape) is drift and fails closed; DISTINCT collapsing
--       would silently normalize it into the expected set.
-- Literal order and IN() vs = ANY() spelling canonicalize identically, so a
-- valid constraint is never falsely rejected; any other shape, extra value,
-- missing value, or duplicate entry raises and rolls the transaction back —
-- drift is never dropped or repaired silently.
--
-- Isolation: SET TRANSACTION ISOLATION LEVEL READ COMMITTED is the first
-- statement after BEGIN, so even a caller that opened REPEATABLE READ or
-- SERIALIZABLE downgrades before any snapshot is taken, and the DO block
-- re-reads the catalog with a fresh snapshot after any advisory-lock wait.
-- If a caller runs this script after executing a query inside its own
-- transaction, PostgreSQL rejects the isolation change (SQLSTATE 25001,
-- active_sql_transaction) and the migration fails closed before any DDL —
-- never an unsafe continuation on a stale snapshot.
--
-- Concurrency: the advisory lock is taken BEFORE the pg_constraint read and
-- held until COMMIT. Without it, two concurrent applies could both read the
-- old definition inside the same DO-block snapshot; the loser's DROP
-- CONSTRAINT would then hit undefined_object against the winner's
-- replacement, an error no exception handler could have covered. With READ
-- COMMITTED pinned and the lock held as a separate statement, the DO block
-- gets a fresh snapshot after any lock wait, so the second apply observes
-- the widened definition and no-ops.
BEGIN;

-- WHY first statement: PostgreSQL only allows changing the isolation level
-- before any query or lock in the transaction; placed here it both pins READ
-- COMMITTED for default callers and downgrades non-default callers safely.
SET TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- WHY: 5393038 is the fixed decimal encoding of ASCII "RAG" as a repo-wide
-- advisory-lock namespace; 1 is the slot for the ip_rate_limit kind widening.
-- Constant two-int key (int4, int4) — deterministic, no input, no hashing.
SELECT pg_advisory_xact_lock(5393038, 1);

DO $$
DECLARE
  current_def     TEXT;
  skeleton        TEXT;
  kinds           TEXT[];
  all_kinds       TEXT[];
  narrow_kinds    CONSTANT TEXT[] := ARRAY['lead', 'mint', 'query'];
  target_kinds    CONSTANT TEXT[] := ARRAY['fcm-register', 'lead', 'mint', 'query'];
  narrow_skeleton CONSTANT TEXT := 'CHECKkind=ANYARRAY[?,?,?]';
  target_skeleton CONSTANT TEXT := 'CHECKkind=ANYARRAY[?,?,?,?]';
BEGIN
  SELECT pg_get_constraintdef(oid, true) INTO current_def
    FROM pg_constraint
   WHERE conname = 'ip_rate_limit_kind_check'
     AND conrelid = 'public.ip_rate_limit'::regclass;

  IF current_def IS NULL THEN
    RAISE EXCEPTION 'ip_rate_limit_kind_check missing on public.ip_rate_limit; refusing unsafe migration';
  END IF;

  -- Canonicalize the SHAPE GUARD only (exactness comes from the literal list
  -- below): quoted literals — with or without the ::text cast PostgreSQL
  -- renders for text values — collapse to '?', residual ::text casts are
  -- dropped, then ALL parentheses and whitespace are stripped. This accepts
  -- IN() vs = ANY() spellings, literal order, the non-pretty double-wrapped
  -- rendering CHECK ((kind = ANY (ARRAY[...]))) and any balanced redundant
  -- wrapping, while any extra conjunct, operator, or cast outside the
  -- membership expression still fails the guard.
  skeleton := regexp_replace(current_def, '''[^'']*''::text', '?', 'g');
  skeleton := regexp_replace(skeleton, '''[^'']*''', '?', 'g');
  skeleton := replace(skeleton, '::text', '');
  skeleton := translate(skeleton, '()', '');
  skeleton := regexp_replace(skeleton, '\s+', '', 'g');

  -- Extract the full (NON-distinct) sorted literal list, then compare the
  -- total literal count against its distinct count: a constraint carrying a
  -- duplicated literal has identical counts but a non-equal aggregate only
  -- after collapsing, so count-vs-distinct is the fail-closed tripwire.
  SELECT coalesce(array_agg(m ORDER BY m), ARRAY[]::TEXT[])
    INTO all_kinds
    FROM (SELECT (regexp_matches(current_def, '''([^'']*)''', 'g'))[1] AS m) sub;

  kinds := (SELECT array_agg(DISTINCT m ORDER BY m)
              FROM (SELECT unnest(all_kinds) AS m) sub2);

  IF skeleton = target_skeleton AND all_kinds = target_kinds
     AND array_length(all_kinds, 1) = coalesce(array_length(kinds, 1), 0) THEN
    RAISE NOTICE 'ip_rate_limit_kind_check already widened to the four kinds; no-op';
  ELSIF skeleton = narrow_skeleton AND all_kinds = narrow_kinds
        AND array_length(all_kinds, 1) = coalesce(array_length(kinds, 1), 0) THEN
    ALTER TABLE public.ip_rate_limit
      DROP CONSTRAINT ip_rate_limit_kind_check;
    ALTER TABLE public.ip_rate_limit
      ADD CONSTRAINT ip_rate_limit_kind_check
      CHECK (kind IN ('query', 'mint', 'lead', 'fcm-register'));
  ELSE
    RAISE EXCEPTION 'ip_rate_limit_kind_check drift on public.ip_rate_limit; refusing unsafe migration; observed: %', current_def;
  END IF;
  -- No exception handler on purpose: the advisory lock serializes concurrent
  -- applies, so any error here is real schema drift and must fail the
  -- transaction closed rather than be swallowed.
END $$;

COMMENT ON CONSTRAINT ip_rate_limit_kind_check ON public.ip_rate_limit IS
  'Allowed per-IP rate limit kinds; widened additively for fcm-register (2026-09-03)';

COMMIT;
