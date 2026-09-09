from pathlib import Path

import asyncpg
import pytest

from api.infrastructure.adapters import postgres_chat_history


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BrokenConnection:
    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        raise asyncpg.UndefinedColumnError('column "chat_sessions.identity_key" does not exist')

    async def fetch(self, query, *args):
        raise asyncpg.UndefinedColumnError('column "chat_sessions.identity_key" does not exist')

    async def fetchrow(self, query, *args):
        raise asyncpg.UndefinedColumnError('column "chat_sessions.identity_key" does not exist')


class _Acquire:
    async def __aenter__(self):
        return _BrokenConnection()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BrokenPool:
    def acquire(self):
        return _Acquire()


@pytest.fixture
def degraded_pool(monkeypatch):
    async def get_pool():
        return _BrokenPool()

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", get_pool)


@pytest.mark.asyncio
async def test_legacy_identity_column_degrades_reads_and_writes(degraded_pool):
    repository = postgres_chat_history.PostgresChatHistoryRepository()

    assert not await repository.append_turn(
        session_id="s1",
        device_id="d1",
        project_key="camellia",
        user_content="hello",
        assistant_content="world",
        assistant_meta={},
        identity_key="identity-1",
    )
    assert await repository.list_sessions(
        device_id="d1", project_key="camellia", identity_key="identity-1"
    ) == []
    assert await repository.get_session(
        session_id="s1",
        device_id="d1",
        project_key="camellia",
        identity_key="identity-1",
    ) is None


def test_non_chat_undefined_column_is_not_degraded():
    exc = asyncpg.UndefinedColumnError('column "leads.secret" does not exist')
    assert not postgres_chat_history._is_history_degraded(exc)


def test_upgrade_migration_is_forward_only_and_idempotent():
    migration = Path("db/migrations/2026-08-26-chat-sessions-upgrade.sql").read_text()
    assert "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS identity_key TEXT" in migration
    assert "CREATE INDEX IF NOT EXISTS idx_chat_sessions_device_last_active" in migration
    assert "UPDATE chat_sessions" not in migration
    assert "DROP TABLE" not in migration
