-- rag-real-estate — Migration 2026-08-24: durable anonymous quota + per-IP rate limit.
-- Forward-only, idempotent, and data-preserving. The migration expands legacy tables
-- in place; it never drops or rebuilds live quota data.
-- Embedding dimensions remain locked at 1024; this migration does not touch embeddings.
-- Application values are bound parameters. Literals below are schema defaults/checks only.

BEGIN;

-- anon_quota is the identity/project quota authority. A legacy deployment may have
-- used identity_key as its only key. Such rows are retained in the empty-project
-- bucket; callers can subsequently use an explicit project key without losing usage.
DO $$
DECLARE
  identity_type text;
  project_type text;
BEGIN
  IF to_regclass('public.anon_quota') IS NULL THEN
    CREATE TABLE public.anon_quota (
      identity_key TEXT NOT NULL,
      project_key TEXT NOT NULL DEFAULT '',
      used_turns INTEGER NOT NULL DEFAULT 0,
      bonus_turns INTEGER NOT NULL DEFAULT 0,
      granted_turns INTEGER NOT NULL DEFAULT 0,
      bonus_granted BOOLEAN NOT NULL DEFAULT false,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT anon_quota_pkey PRIMARY KEY (identity_key, project_key)
    );
    RETURN;
  END IF;

  SELECT c.data_type INTO identity_type
  FROM information_schema.columns c
  WHERE c.table_schema = 'public' AND c.table_name = 'anon_quota' AND c.column_name = 'identity_key';
  IF identity_type IS DISTINCT FROM 'text' THEN
    RAISE EXCEPTION 'anon_quota.identity_key must be text; found %', COALESCE(identity_type, '<missing>');
  END IF;

  SELECT c.data_type INTO project_type
  FROM information_schema.columns c
  WHERE c.table_schema = 'public' AND c.table_name = 'anon_quota' AND c.column_name = 'project_key';
  IF project_type IS NOT NULL AND project_type IS DISTINCT FROM 'text' THEN
    RAISE EXCEPTION 'anon_quota.project_key must be text; found %', project_type;
  END IF;

  ALTER TABLE public.anon_quota ADD COLUMN IF NOT EXISTS project_key TEXT;
  ALTER TABLE public.anon_quota ADD COLUMN IF NOT EXISTS used_turns INTEGER;
  ALTER TABLE public.anon_quota ADD COLUMN IF NOT EXISTS bonus_turns INTEGER;
  ALTER TABLE public.anon_quota ADD COLUMN IF NOT EXISTS granted_turns INTEGER;
  ALTER TABLE public.anon_quota ADD COLUMN IF NOT EXISTS bonus_granted BOOLEAN;
  ALTER TABLE public.anon_quota ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
  ALTER TABLE public.anon_quota ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'anon_quota'
      AND ((column_name IN ('used_turns', 'bonus_turns', 'granted_turns') AND data_type <> 'integer')
        OR (column_name = 'bonus_granted' AND data_type <> 'boolean')
        OR (column_name IN ('created_at', 'updated_at') AND data_type <> 'timestamp with time zone'))
  ) THEN
    RAISE EXCEPTION 'anon_quota has incompatible counter, flag, or timestamp types; refusing unsafe migration';
  END IF;

  UPDATE public.anon_quota SET project_key = '' WHERE project_key IS NULL;
  UPDATE public.anon_quota SET used_turns = 0 WHERE used_turns IS NULL;
  UPDATE public.anon_quota SET bonus_turns = 0 WHERE bonus_turns IS NULL;
  UPDATE public.anon_quota SET granted_turns = 0 WHERE granted_turns IS NULL;
  UPDATE public.anon_quota SET bonus_granted = false WHERE bonus_granted IS NULL;
  UPDATE public.anon_quota SET created_at = now() WHERE created_at IS NULL;
  UPDATE public.anon_quota SET updated_at = now() WHERE updated_at IS NULL;

  IF EXISTS (SELECT 1 FROM public.anon_quota WHERE identity_key IS NULL OR project_key IS NULL
             OR used_turns < 0 OR bonus_turns < 0 OR granted_turns < 0) THEN
    RAISE EXCEPTION 'anon_quota contains null keys or negative counters; refusing unsafe migration';
  END IF;
  IF EXISTS (SELECT identity_key, project_key FROM public.anon_quota
             GROUP BY identity_key, project_key HAVING count(*) > 1) THEN
    RAISE EXCEPTION 'anon_quota has duplicate identity/project rows; refusing to merge or lose data';
  END IF;

  ALTER TABLE public.anon_quota ALTER COLUMN project_key SET DEFAULT '';
  ALTER TABLE public.anon_quota ALTER COLUMN project_key SET NOT NULL;
  ALTER TABLE public.anon_quota ALTER COLUMN used_turns SET DEFAULT 0;
  ALTER TABLE public.anon_quota ALTER COLUMN used_turns SET NOT NULL;
  ALTER TABLE public.anon_quota ALTER COLUMN bonus_turns SET DEFAULT 0;
  ALTER TABLE public.anon_quota ALTER COLUMN bonus_turns SET NOT NULL;
  ALTER TABLE public.anon_quota ALTER COLUMN granted_turns SET DEFAULT 0;
  ALTER TABLE public.anon_quota ALTER COLUMN granted_turns SET NOT NULL;
  ALTER TABLE public.anon_quota ALTER COLUMN bonus_granted SET DEFAULT false;
  ALTER TABLE public.anon_quota ALTER COLUMN bonus_granted SET NOT NULL;
  ALTER TABLE public.anon_quota ALTER COLUMN created_at SET DEFAULT now();
  ALTER TABLE public.anon_quota ALTER COLUMN created_at SET NOT NULL;
  ALTER TABLE public.anon_quota ALTER COLUMN updated_at SET DEFAULT now();
  ALTER TABLE public.anon_quota ALTER COLUMN updated_at SET NOT NULL;
END $$;

-- Replace only an obsolete key constraint, never rows. Any unexpected primary key
-- shape is a hard failure rather than an implicit table rebuild.
DO $$
DECLARE
  pk_name text;
  pk_columns text[];
BEGIN
  SELECT c.conname, array_agg(a.attname ORDER BY k.ord)
    INTO pk_name, pk_columns
  FROM pg_constraint c
  JOIN unnest(c.conkey) WITH ORDINALITY k(attnum, ord) ON true
  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
  WHERE c.conrelid = 'public.anon_quota'::regclass AND c.contype = 'p'
  GROUP BY c.conname;

  IF pk_name IS NULL THEN
    ALTER TABLE public.anon_quota ADD CONSTRAINT anon_quota_pkey PRIMARY KEY (identity_key, project_key);
  ELSIF pk_columns = ARRAY['identity_key']::text[] THEN
    EXECUTE format('ALTER TABLE public.anon_quota DROP CONSTRAINT %I', pk_name);
    ALTER TABLE public.anon_quota ADD CONSTRAINT anon_quota_pkey PRIMARY KEY (identity_key, project_key);
  ELSIF pk_columns <> ARRAY['identity_key', 'project_key']::text[] THEN
    RAISE EXCEPTION 'anon_quota has unexpected primary key columns: %', pk_columns;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_anon_quota_updated_at ON public.anon_quota (updated_at);

CREATE TABLE IF NOT EXISTS public.quota_reservations (
  reservation_id UUID PRIMARY KEY,
  identity_key TEXT NOT NULL,
  project_key TEXT NOT NULL,
  allowance_class TEXT NOT NULL CHECK (allowance_class IN ('base', 'bonus', 'granted', 'unlimited')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_quota_reservations_identity_project
  ON public.quota_reservations (identity_key, project_key, created_at);

CREATE TABLE IF NOT EXISTS public.quota_grant_audit (
  request_id TEXT PRIMARY KEY,
  identity_key TEXT NOT NULL,
  project_key TEXT NOT NULL DEFAULT '',
  granted_turns INTEGER NOT NULL CHECK (granted_turns > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.quota_grant_audit ADD COLUMN IF NOT EXISTS project_key TEXT;
ALTER TABLE public.quota_grant_audit ADD COLUMN IF NOT EXISTS granted_turns INTEGER;
ALTER TABLE public.quota_grant_audit ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'quota_grant_audit'
      AND ((column_name = 'granted_turns' AND data_type <> 'integer')
        OR (column_name = 'created_at' AND data_type <> 'timestamp with time zone'))
  ) THEN
    RAISE EXCEPTION 'quota_grant_audit has incompatible grant or timestamp types; refusing unsafe migration';
  END IF;
END $$;
UPDATE public.quota_grant_audit SET project_key = '' WHERE project_key IS NULL;
UPDATE public.quota_grant_audit SET created_at = now() WHERE created_at IS NULL;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.quota_grant_audit WHERE project_key IS NULL OR granted_turns IS NULL OR granted_turns <= 0) THEN
    RAISE EXCEPTION 'quota_grant_audit contains invalid grant rows; refusing unsafe migration';
  END IF;
END $$;
ALTER TABLE public.quota_grant_audit ALTER COLUMN project_key SET DEFAULT '';
ALTER TABLE public.quota_grant_audit ALTER COLUMN project_key SET NOT NULL;
ALTER TABLE public.quota_grant_audit ALTER COLUMN granted_turns SET NOT NULL;
ALTER TABLE public.quota_grant_audit ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE public.quota_grant_audit ALTER COLUMN created_at SET NOT NULL;

CREATE TABLE IF NOT EXISTS public.ip_rate_limit (
  ip INET NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('query', 'mint', 'lead')),
  window_start TIMESTAMPTZ NOT NULL,
  counter INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ip, kind, window_start)
);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.ip_rate_limit WHERE counter < 0) THEN
    RAISE EXCEPTION 'ip_rate_limit contains negative counters; refusing unsafe migration';
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_ip_rate_limit_window_start ON public.ip_rate_limit (window_start);

COMMENT ON TABLE public.anon_quota IS 'System of record for identity/project chat quota; migrated in place without data loss';
COMMENT ON TABLE public.ip_rate_limit IS 'Fixed-window per-IP request counters; swept by TTL cleanup';

COMMIT;
