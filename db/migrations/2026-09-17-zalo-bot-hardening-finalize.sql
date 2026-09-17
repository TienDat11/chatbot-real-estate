-- Apply only after approved backfill and valid concurrent indexes, writers stopped.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $gate$
BEGIN
  IF EXISTS (SELECT 1 FROM zalo_bot_events e
    LEFT JOIN zalo_bot_ingress_keys k ON k.ingress_key=e.ingress_key
    LEFT JOIN zalo_bot_admissions a ON a.source_event_key=e.source_event_key
    WHERE e.identity_key IS NULL OR e.ingress_key IS NULL OR e.source_event_key IS NULL
      OR k.event_id IS DISTINCT FROM e.id OR k.source_event_key IS DISTINCT FROM e.source_event_key
      OR e.source_event_key <> 'zalo-bot:event:v1:' || e.id::text
      OR (a.identity_key IS NOT NULL AND a.identity_key<>e.identity_key)
      OR ((e.assistant_outcome IS NOT NULL OR e.outbound_message_id IS NOT NULL)
        AND a.source_event_key IS NULL)) THEN
    RAISE EXCEPTION 'ZALO_BACKFILL_REQUIRED';
  END IF;
  IF (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname IN ('zalo_bot_events_ingress_key_uidx',
      'zalo_bot_events_source_key_uidx','zalo_bot_events_due_idx','zalo_bot_events_terminal_idx')
      AND i.indrelid='public.zalo_bot_events'::regclass
      AND i.indisvalid AND i.indisready) <> 4 THEN
    RAISE EXCEPTION 'ZALO_INDEX_INVALID';
  END IF;
END
$gate$;
ALTER TABLE zalo_bot_events
  ALTER COLUMN ingress_key SET NOT NULL,
  ALTER COLUMN identity_key SET NOT NULL,
  ALTER COLUMN source_event_key SET NOT NULL;
COMMIT;
