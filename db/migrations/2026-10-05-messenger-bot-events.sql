-- rag-real-estate — Migration 2026-10-05: Messenger Bot ingress event log.
-- Forward-only, idempotent, and data-preserving.
--
-- Context: Messenger channel mirrors the Zalo Bot webhook ingress. The webhook
-- route persists every accepted webhook into a durable event table so a later
-- processor can fan out the actual RAG/provider work without the webhook
-- blocking on it.
--
-- Change: create messenger_bot_events with a composite UNIQUE on (psid,
-- event_ref) so the atomic INSERT ... ON CONFLICT DO NOTHING dedupe in the
-- Postgres events adapter is provably one statement per event. Unlike Zalo,
-- text defaults to '' (never NULL) and selected_project_key carries the
-- numbered-picker project choice. No retry/identity/outcome columns here —
-- those belong to the roundtrip migration.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.messenger_bot_events') IS NULL THEN
    CREATE TABLE messenger_bot_events (
      id BIGSERIAL PRIMARY KEY,
      psid TEXT NOT NULL,
      event_ref TEXT NOT NULL,
      event_kind TEXT NOT NULL CHECK (event_kind IN ('message', 'select_project', 'get_started')),
      text TEXT NOT NULL DEFAULT '',
      selected_project_key TEXT,
      status TEXT NOT NULL DEFAULT 'pending',
      received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (psid, event_ref)
    );
    CREATE INDEX messenger_bot_events_status_received_at_idx
      ON messenger_bot_events (status, received_at);
  END IF;

  -- Reuse any valid, immediate, non-partial unique index with these exact
  -- key columns (either order, optionally INCLUDE columns). Constraint names
  -- can differ.
  IF NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = 'public.messenger_bot_events'::regclass
      AND i.indisunique AND i.indisvalid AND i.indimmediate
      AND i.indpred IS NULL AND i.indexprs IS NULL
      AND i.indnkeyatts = 2
      AND (
        SELECT array_agg(a.attname::text ORDER BY a.attname::text)
        FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinal)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE k.ordinal <= i.indnkeyatts
      ) = ARRAY['event_ref', 'psid']::text[]
  ) THEN
    -- Existing duplicate keys fail atomically; never delete or merge data.
    CREATE UNIQUE INDEX messenger_bot_events_psid_event_ref_uidx
      ON public.messenger_bot_events (psid, event_ref);
  END IF;
END
$$;

COMMIT;
