BEGIN;

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    device_id TEXT NULL,
    identity_key TEXT NULL,
    project_key TEXT NOT NULL,
    title TEXT NULL,
    message_count INT NOT NULL DEFAULT 0,
    handed_off BOOL NOT NULL DEFAULT FALSE,
    lead_id INT NULL REFERENCES leads(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    project_key TEXT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id_id ON chat_messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_device_last_active ON chat_sessions(device_id, last_active_at DESC);

COMMIT;
