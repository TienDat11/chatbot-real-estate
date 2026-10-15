-- ZB-02 roundtrip state and trusted administrative sales bindings.
-- Forward-only, repeat-safe additive migration; no membership backfill.
-- Apply only after the ZB-01 event table and existing sales schema.
-- Down: disable Zalo processing first; removing these columns/table destroys
-- cached delivery state and binding ownership. No automatic rollback provided.

BEGIN;

ALTER TABLE zalo_bot_events
    ADD COLUMN IF NOT EXISTS identity_key TEXT,
    ADD COLUMN IF NOT EXISTS source_event_key TEXT,
    ADD COLUMN IF NOT EXISTS assistant_outcome JSONB,
    ADD COLUMN IF NOT EXISTS outbound_message_id TEXT,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

-- Existing status TEXT supports pending/rejected/done without staged states.
CREATE TABLE IF NOT EXISTS zalo_sales_bindings (
    opaque_identity TEXT PRIMARY KEY,
    sales_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE RESTRICT,
    is_active BOOLEAN NOT NULL
);

COMMIT;
