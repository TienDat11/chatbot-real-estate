-- FCM tokens become (identity_type, identity_key) addressed so anonymous
-- chat customers (no Firebase Auth) can register devices with the same signed
-- anon token /api/lead verifies. Expand-and-contract: the legacy firebase_uid
-- column and its unique constraint stay until a later release drops them.
BEGIN;
ALTER TABLE fcm_device_tokens ADD COLUMN IF NOT EXISTS identity_type TEXT
  NOT NULL DEFAULT 'firebase_uid';
ALTER TABLE fcm_device_tokens ADD COLUMN IF NOT EXISTS identity_key TEXT;

DO $$ BEGIN
  ALTER TABLE fcm_device_tokens
    ADD CONSTRAINT chk_fcm_device_tokens_identity_type
    CHECK (identity_type IN ('firebase_uid', 'anon_customer'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Backfill: existing rows were all Firebase-auth registrations.
UPDATE fcm_device_tokens SET identity_key = firebase_uid
  WHERE identity_key IS NULL;
ALTER TABLE fcm_device_tokens ALTER COLUMN identity_key SET NOT NULL;
ALTER TABLE fcm_device_tokens ALTER COLUMN firebase_uid DROP NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fcm_device_tokens_identity_token
  ON fcm_device_tokens (identity_type, identity_key, token);
-- Cap-eviction ordering index (oldest last_seen_at evicted first).
CREATE INDEX IF NOT EXISTS idx_fcm_device_tokens_identity_recency
  ON fcm_device_tokens (identity_type, identity_key, last_seen_at DESC)
  WHERE enabled;
COMMIT;
