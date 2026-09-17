-- rag-real-estate — Migration 2026-09-17: Zalo Bot ingress event log.
-- Forward-only, idempotent, and data-preserving.
--
-- Context: ZB-01 adds authenticated Zalo Bot webhook ingress. The webhook
-- route persists every accepted webhook into a durable event table so a later
-- processor (ZB-02) can fan out the actual RAG/provider work without the
-- webhook blocking on it.
--
-- Change: create zalo_bot_events with a composite UNIQUE on (chat_id,
-- message_id) so the atomic INSERT ... ON CONFLICT DO NOTHING
-- dedupe in postgres_zalo_events.py is provably one statement per event.
-- No retry/identity/outcome columns here — those belong to ZB-02.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.zalo_bot_events') IS NULL THEN
    CREATE TABLE zalo_bot_events (
      id BIGSERIAL PRIMARY KEY,
      chat_id TEXT NOT NULL,
      message_id TEXT NOT NULL,
      event_name TEXT NOT NULL,
      text TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (chat_id, message_id)
    );
    CREATE INDEX zalo_bot_events_status_received_at_idx
      ON zalo_bot_events (status, received_at);
  END IF;

  -- Rename only the legacy schema; PostgreSQL preserves rows and indexes.
  IF EXISTS (
    SELECT 1 FROM pg_attribute
    WHERE attrelid = 'public.zalo_bot_events'::regclass
      AND attname = 'provider_message_id' AND NOT attisdropped
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_attribute
    WHERE attrelid = 'public.zalo_bot_events'::regclass
      AND attname = 'message_id' AND NOT attisdropped
  ) THEN
    ALTER TABLE public.zalo_bot_events RENAME COLUMN provider_message_id TO message_id;
  END IF;

  -- Reuse any valid, immediate, non-partial unique index with these exact
  -- key columns (either order, optionally INCLUDE columns). Constraint names
  -- can differ, including indexes retained by the legacy-column rename.
  IF NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = 'public.zalo_bot_events'::regclass
      AND i.indisunique AND i.indisvalid AND i.indimmediate
      AND i.indpred IS NULL AND i.indexprs IS NULL
      AND i.indnkeyatts = 2
      AND (
        SELECT array_agg(a.attname::text ORDER BY a.attname::text)
        FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinal)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE k.ordinal <= i.indnkeyatts
      ) = ARRAY['chat_id', 'message_id']::text[]
  ) THEN
    -- Existing duplicate keys fail atomically; never delete or merge data.
    CREATE UNIQUE INDEX zalo_bot_events_chat_message_uidx
      ON public.zalo_bot_events (chat_id, message_id);
  END IF;
END
$$;

COMMIT;
