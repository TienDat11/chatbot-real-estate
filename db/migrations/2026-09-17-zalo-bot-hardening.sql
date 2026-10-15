-- ZB-03 forward-only. Apply after ZB-02 while all Zalo writers are stopped.
BEGIN;
SET LOCAL lock_timeout = '5s';

-- Validate existing objects BEFORE changing anything. Missing new objects are
-- expected; IF NOT EXISTS must never silently accept incompatible definitions.
DO $preflight$
DECLARE r record; col record; obj regclass; definition text; actual text[];
BEGIN
  IF to_regclass('public.zalo_bot_events') IS NULL
     OR to_regclass('public.zalo_sales_bindings') IS NULL THEN
    RAISE EXCEPTION 'ZALO_SCHEMA_INCOMPATIBLE';
  END IF;
  FOR r IN SELECT * FROM (VALUES
    ('zalo_bot_events','id','bigint',true,'sequence',true),
    ('zalo_bot_events','chat_id','text',true,NULL,true),
    ('zalo_bot_events','message_id','text',true,NULL,true),
    ('zalo_bot_events','event_name','text',true,NULL,true),
    ('zalo_bot_events','text','text',true,NULL,true),
    ('zalo_bot_events','status','text',true,'''pending''::text',true),
    ('zalo_bot_events','received_at','timestamp with time zone',true,'now()',true),
    ('zalo_bot_events','updated_at','timestamp with time zone',true,'now()',true),
    ('zalo_bot_events','identity_key','text',NULL,NULL,true),
    ('zalo_bot_events','source_event_key','text',NULL,NULL,true),
    ('zalo_bot_events','assistant_outcome','jsonb',false,NULL,true),
    ('zalo_bot_events','outbound_message_id','text',false,NULL,true),
    ('zalo_bot_events','completed_at','timestamp with time zone',false,NULL,true),
    ('zalo_bot_events','ingress_key','text',NULL,NULL,false),
    ('zalo_bot_events','attempt_count','integer',true,'0',false),
    ('zalo_bot_events','locked_at','timestamp with time zone',false,NULL,false),
    ('zalo_bot_events','lease_until','timestamp with time zone',false,NULL,false),
    ('zalo_bot_events','claim_token','uuid',false,NULL,false),
    ('zalo_bot_events','next_attempt_at','timestamp with time zone',true,'now()',false),
    ('zalo_bot_events','last_error_code','text',false,NULL,false),
    ('zalo_bot_ingress_keys','ingress_key','text',true,NULL,true),
    ('zalo_bot_ingress_keys','event_id','bigint',true,NULL,true),
    ('zalo_bot_ingress_keys','source_event_key','text',true,NULL,true),
    ('zalo_bot_ingress_keys','accepted_at','timestamp with time zone',true,'now()',true),
    ('zalo_bot_admissions','source_event_key','text',true,NULL,true),
    ('zalo_bot_admissions','identity_key','text',true,NULL,true),
    ('zalo_bot_admissions','decision','text',true,NULL,true),
    ('zalo_bot_admissions','decided_at','timestamp with time zone',true,'now()',true),
    ('zalo_bot_burst_windows','identity_key','text',true,NULL,true),
    ('zalo_bot_burst_windows','window_start','timestamp with time zone',true,NULL,true),
    ('zalo_bot_burst_windows','admitted_count','integer',true,NULL,true),
    ('zalo_bot_actor_leases','identity_key','text',true,NULL,true),
    ('zalo_bot_actor_leases','holder_token','uuid',true,NULL,true),
    ('zalo_bot_actor_leases','lease_until','timestamp with time zone',true,NULL,true),
    ('zalo_sales_bindings','opaque_identity','text',true,NULL,true),
    ('zalo_sales_bindings','sales_id','bigint',true,NULL,true),
    ('zalo_sales_bindings','is_active','boolean',true,NULL,true)
  ) AS x(tbl,col,typ,nn,def,required) LOOP
    obj := to_regclass('public.' || r.tbl);
    IF obj IS NULL THEN CONTINUE; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid=obj AND relkind='r') THEN
      RAISE EXCEPTION 'ZALO_SCHEMA_INCOMPATIBLE';
    END IF;
    SELECT format_type(p.atttypid,p.atttypmod) AS typ,p.attnotnull AS nn,
      pg_get_expr(d.adbin,d.adrelid) AS def INTO col
    FROM pg_attribute p LEFT JOIN pg_attrdef d ON d.adrelid=p.attrelid AND d.adnum=p.attnum
    WHERE p.attrelid=obj AND p.attname=r.col AND p.attnum>0 AND NOT p.attisdropped;
    IF NOT FOUND THEN
      IF r.required THEN RAISE EXCEPTION 'ZALO_SCHEMA_INCOMPATIBLE'; END IF;
    ELSIF col.typ<>r.typ OR (r.nn IS NOT NULL AND col.nn<>r.nn)
      OR (r.def IS DISTINCT FROM 'sequence' AND col.def IS DISTINCT FROM r.def)
      OR (r.def='sequence' AND (col.def IS NULL OR col.def NOT LIKE 'nextval(%'
        OR pg_get_serial_sequence('public.' || r.tbl,r.col) IS NULL)) THEN
      RAISE EXCEPTION 'ZALO_SCHEMA_INCOMPATIBLE';
    END IF;
  END LOOP;
  FOR r IN SELECT * FROM (VALUES
    ('zalo_bot_events','PRIMARY KEY (id)'),
    ('zalo_bot_ingress_keys','PRIMARY KEY (ingress_key)'),
    ('zalo_bot_ingress_keys','UNIQUE (event_id)'),
    ('zalo_bot_ingress_keys','UNIQUE (source_event_key)'),
    ('zalo_bot_admissions','PRIMARY KEY (source_event_key)'),
    ('zalo_bot_admissions','FOREIGN KEY (source_event_key) REFERENCES zalo_bot_ingress_keys(source_event_key) ON DELETE RESTRICT'),
    ('zalo_bot_admissions','CHECK ((decision = ANY (ARRAY[''started''::text, ''braked''::text])))'),
    ('zalo_bot_burst_windows','PRIMARY KEY (identity_key)'),
    ('zalo_bot_burst_windows','CHECK ((admitted_count >= 0))'),
    ('zalo_bot_actor_leases','PRIMARY KEY (identity_key)'),
    ('zalo_sales_bindings','PRIMARY KEY (opaque_identity)'),
    ('zalo_sales_bindings','FOREIGN KEY (sales_id) REFERENCES sales(id) ON DELETE RESTRICT')
  ) AS x(tbl,def) LOOP
    obj := to_regclass('public.' || r.tbl);
    IF obj IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pg_constraint k
      WHERE k.conrelid=obj AND k.convalidated AND NOT k.condeferrable
        AND replace(pg_get_constraintdef(k.oid),'public.','')=r.def) THEN
      RAISE EXCEPTION 'ZALO_SCHEMA_INCOMPATIBLE';
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE
      conrelid=to_regclass('public.zalo_bot_ingress_keys') AND contype='f') THEN
    RAISE EXCEPTION 'ZALO_SCHEMA_INCOMPATIBLE';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid='public.zalo_bot_events'::regclass
    AND i.indisunique AND i.indisvalid AND i.indimmediate AND i.indpred IS NULL
    AND i.indexprs IS NULL AND i.indnkeyatts=2 AND
    (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
      FROM unnest(i.indkey) WITH ORDINALITY k(num,ord)
      JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
      WHERE k.ord<=i.indnkeyatts)=ARRAY['chat_id','message_id']) THEN
    RAISE EXCEPTION 'ZALO_SCHEMA_INCOMPATIBLE';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname IN ('zalo_bot_events','zalo_bot_ingress_keys',
      'zalo_bot_admissions','zalo_bot_burst_windows','zalo_bot_actor_leases','zalo_sales_bindings')
      AND (NOT i.indisvalid OR NOT i.indisready)) THEN
    RAISE EXCEPTION 'ZALO_INDEX_INVALID';
  END IF;
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

ALTER TABLE zalo_bot_events
  ADD COLUMN IF NOT EXISTS ingress_key TEXT,
  ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS claim_token UUID,
  ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS last_error_code TEXT;

CREATE TABLE IF NOT EXISTS zalo_bot_ingress_keys (
  ingress_key TEXT PRIMARY KEY,
  event_id BIGINT NOT NULL UNIQUE,
  source_event_key TEXT NOT NULL UNIQUE,
  accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Deliberately no FK to events: tombstones MUST survive event deletion.
CREATE TABLE IF NOT EXISTS zalo_bot_admissions (
  source_event_key TEXT PRIMARY KEY
    REFERENCES zalo_bot_ingress_keys(source_event_key) ON DELETE RESTRICT,
  identity_key TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('started', 'braked')),
  decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS zalo_bot_burst_windows (
  identity_key TEXT PRIMARY KEY,
  window_start TIMESTAMPTZ NOT NULL,
  admitted_count INTEGER NOT NULL CHECK (admitted_count >= 0)
);
CREATE TABLE IF NOT EXISTS zalo_bot_actor_leases (
  identity_key TEXT PRIMARY KEY,
  holder_token UUID NOT NULL,
  lease_until TIMESTAMPTZ NOT NULL
);
COMMIT;
