-- FCM registration tokens are notification transport state, not Firestore data.
BEGIN;
CREATE TABLE IF NOT EXISTS fcm_device_tokens (
  id BIGSERIAL PRIMARY KEY,
  firebase_uid TEXT NOT NULL,
  token TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT 'web',
  enabled BOOLEAN NOT NULL DEFAULT true,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (firebase_uid, token)
);
CREATE INDEX IF NOT EXISTS idx_fcm_device_tokens_uid ON fcm_device_tokens (firebase_uid);
CREATE INDEX IF NOT EXISTS idx_fcm_device_tokens_enabled ON fcm_device_tokens (enabled);
COMMIT;
