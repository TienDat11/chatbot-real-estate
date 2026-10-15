-- Apply only after approved backfill and valid concurrent indexes, writers stopped.
--
-- Diverges from Zalo: the receipt gate exempts event_kind = 'get_started'.
-- Get-started taps carry no assistant output by design, so requiring a
-- receipt for them would block finalization forever. Identity, tombstone,
-- lease, and terminal-state checks still apply to every kind.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $gate$
BEGIN
  IF EXISTS (SELECT 1 FROM messenger_bot_events e
    LEFT JOIN messenger_bot_ingress_keys k ON k.ingress_key=e.ingress_key
    LEFT JOIN messenger_bot_admissions a ON a.source_event_key=e.source_event_key
    WHERE e.identity_key IS NULL OR e.ingress_key IS NULL OR e.source_event_key IS NULL
      OR k.event_id IS DISTINCT FROM e.id OR k.source_event_key IS DISTINCT FROM e.source_event_key
      OR e.source_event_key <> 'messenger-bot:event:v1:' || e.id::text
      OR (a.identity_key IS NOT NULL AND a.identity_key<>e.identity_key)
      OR ((e.assistant_outcome IS NOT NULL OR e.outbound_message_ids IS NOT NULL)
        AND a.source_event_key IS NULL AND e.event_kind <> 'get_started')) THEN
    RAISE EXCEPTION 'MESSENGER_BACKFILL_REQUIRED';
  END IF;
  IF (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname IN ('messenger_bot_events_ingress_key_uidx',
      'messenger_bot_events_source_key_uidx','messenger_bot_events_due_idx','messenger_bot_events_terminal_idx')
      AND i.indrelid='public.messenger_bot_events'::regclass
      AND i.indisvalid AND i.indisready) <> 4 THEN
    RAISE EXCEPTION 'MESSENGER_INDEX_INVALID';
  END IF;
END
$gate$;
ALTER TABLE messenger_bot_events
  ALTER COLUMN ingress_key SET NOT NULL,
  ALTER COLUMN identity_key SET NOT NULL,
  ALTER COLUMN source_event_key SET NOT NULL;
COMMIT;
