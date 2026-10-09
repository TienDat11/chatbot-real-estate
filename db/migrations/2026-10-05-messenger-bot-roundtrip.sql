-- Messenger roundtrip state and trusted administrative sales bindings.
-- Forward-only, repeat-safe additive migration; no membership backfill.
-- Apply only after the Messenger event table and existing sales schema.
-- Down: disable Messenger processing first; removing these columns/table destroys
-- cached delivery state and binding ownership. No automatic rollback provided.
--
-- Diverges from Zalo here: chunked numbered-picker replies need ordered
-- delivery state, so Messenger tracks outbound_message_ids (JSONB array, in
-- send order) instead of Zalo's scalar outbound_message_id.

BEGIN;

ALTER TABLE messenger_bot_events
    ADD COLUMN IF NOT EXISTS identity_key TEXT,
    ADD COLUMN IF NOT EXISTS source_event_key TEXT,
    ADD COLUMN IF NOT EXISTS assistant_outcome JSONB,
    ADD COLUMN IF NOT EXISTS outbound_message_ids JSONB,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

-- Existing status TEXT supports pending/rejected/done without staged states.
CREATE TABLE IF NOT EXISTS messenger_sales_bindings (
    opaque_identity TEXT PRIMARY KEY,
    sales_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE RESTRICT,
    is_active BOOLEAN NOT NULL
);

COMMIT;
