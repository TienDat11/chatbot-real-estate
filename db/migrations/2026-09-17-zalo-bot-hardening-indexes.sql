-- Outside any transaction. Backfill and compatible-schema preflight precede this.
-- Existing invalid/wrong-definition indexes must be rejected, never skipped.
DO $preflight$
DECLARE r record; obj regclass;
BEGIN
  FOR r IN SELECT * FROM (VALUES
    ('zalo_bot_events_ingress_key_uidx',ARRAY['ingress_key'],true,NULL),
    ('zalo_bot_events_source_key_uidx',ARRAY['source_event_key'],true,NULL),
    ('zalo_bot_events_due_idx',ARRAY['next_attempt_at','received_at','id'],false,
      '(status = ANY (ARRAY[''pending''::text, ''failed''::text, ''processing''::text]))'),
    ('zalo_bot_events_terminal_idx',ARRAY['completed_at','id'],false,
      '(status = ANY (ARRAY[''done''::text, ''dead''::text, ''rejected''::text]))')
  ) AS x(idx,cols,uniq,pred) LOOP
    obj:=to_regclass('public.' || r.idx);
    IF obj IS NULL THEN CONTINUE; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indexrelid=obj
      AND i.indrelid='public.zalo_bot_events'::regclass AND i.indisvalid AND i.indisready
      AND i.indimmediate AND i.indisunique=r.uniq AND i.indexprs IS NULL
      AND pg_get_expr(i.indpred,i.indrelid) IS NOT DISTINCT FROM r.pred
      AND (SELECT array_agg(a.attname::text ORDER BY k.ord)
        FROM unnest(i.indkey) WITH ORDINALITY k(num,ord)
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
        WHERE k.ord<=i.indnkeyatts)=r.cols) THEN
      RAISE EXCEPTION 'ZALO_INDEX_INCOMPATIBLE';
    END IF;
  END LOOP;
END
$preflight$;
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS zalo_bot_events_ingress_key_uidx
  ON zalo_bot_events (ingress_key);
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS zalo_bot_events_source_key_uidx
  ON zalo_bot_events (source_event_key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS zalo_bot_events_due_idx
  ON zalo_bot_events (next_attempt_at, received_at, id)
  WHERE status IN ('pending', 'failed', 'processing');
CREATE INDEX CONCURRENTLY IF NOT EXISTS zalo_bot_events_terminal_idx
  ON zalo_bot_events (completed_at, id)
  WHERE status IN ('done', 'dead', 'rejected');
