BEGIN;

CREATE TABLE IF NOT EXISTS identity_links (
    anon_identity_key TEXT PRIMARY KEY,
    firebase_uid TEXT NOT NULL UNIQUE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_link_audit (
    id BIGSERIAL PRIMARY KEY,
    anon_identity_key TEXT NOT NULL,
    firebase_uid TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS identity_key TEXT NULL;
CREATE INDEX IF NOT EXISTS idx_chat_sessions_identity_project_last_active
    ON chat_sessions(identity_key, project_key, last_active_at DESC);

COMMIT;
