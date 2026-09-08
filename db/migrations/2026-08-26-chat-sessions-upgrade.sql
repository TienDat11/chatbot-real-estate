BEGIN;

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS identity_key TEXT;

CREATE INDEX IF NOT EXISTS idx_chat_sessions_device_last_active
  ON chat_sessions(device_id, last_active_at DESC);

COMMIT;
