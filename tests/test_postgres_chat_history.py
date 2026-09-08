from datetime import datetime, timezone

import pytest

from api.infrastructure.adapters import postgres_chat_history


class FakeRow(dict):
    """Minimal asyncpg.Record-shaped row for adapter deserialization tests."""


class FakeConnection:
    def __init__(self):
        self.cta_query = None
        self.cta_args = None

    async def fetchrow(self, query: str, *args):
        self.cta_query = query
        self.cta_args = args
        return FakeRow(message_count=6, handed_off=False, phone_given=False)

    async def fetch(self, query: str, session_id: str):
        return [
            FakeRow(
                role="assistant",
                content="42 tỷ",
                meta='{"images": [], "confidence": 0.9}',
                created_at=datetime.now(timezone.utc),
            )
        ]


class FakeAcquire:
    async def __aenter__(self):
        return FakeConnection()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self):
        self.connection = FakeConnection()

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return pool.connection

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Acquire()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_cta_state_uses_full_session_scope(monkeypatch):
    pool = FakePool()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    state = await postgres_chat_history.PostgresChatHistoryRepository().get_cta_state(
        session_id="session-1",
        device_id="device-1",
        project_key="camellia",
        identity_key="identity-1",
    )

    assert state == (3, False, False)
    assert pool.connection.cta_args == ("session-1", "device-1", "identity-1", "camellia")
    assert "cs.session_id=$1" in pool.connection.cta_query
    assert "cs.device_id=$2" in pool.connection.cta_query
    assert "cs.identity_key=$3" in pool.connection.cta_query
    assert "cs.project_key=$4" in pool.connection.cta_query


@pytest.mark.asyncio
async def test_cta_state_unknown_scope_returns_zero_without_leak(monkeypatch):
    pool = FakePool()
    pool.connection.fetchrow = _missing_fetchrow

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)
    assert await postgres_chat_history.PostgresChatHistoryRepository().get_cta_state(
        session_id="guessed", device_id="other", project_key="other", identity_key="other"
    ) == (0, False, False)


async def _missing_fetchrow(query: str, *args):
    return None


@pytest.mark.asyncio
async def test_list_messages_normalizes_raw_jsonb_string(monkeypatch):
    async def fake_get_pool():
        return FakePool()

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    messages = await postgres_chat_history.PostgresChatHistoryRepository().list_messages(
        session_id="session-1"
    )

    assert messages[0].meta == {"images": [], "confidence": 0.9}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ({"ok": True}, {"ok": True}),
        ('{"ok": true}', {"ok": True}),
        ("not-json", None),
        ("[]", None),
        (42, None),
    ],
)
def test_normalize_meta_handles_all_supported_asyncpg_values(value, expected):
    assert postgres_chat_history._normalize_meta(value) == expected


# --- lead handoff + lead transcript resolution (QA leads 177/179 regression) --


class RecordingConnection:
    """Captures the last fetchrow statement and returns a canned row."""

    def __init__(self, row):
        self.row = row
        self.fetchrow_query = None
        self.fetchrow_args = None

    async def fetchrow(self, query: str, *args):
        self.fetchrow_query = query
        self.fetchrow_args = args
        return self.row

    async def fetch(self, query: str, *args):
        return [
            FakeRow(
                role="user",
                content="Cho xem mat bang",
                meta="{}",
                created_at=datetime.now(timezone.utc),
            )
        ]


class RecordingPool:
    def __init__(self, row):
        self.connection = RecordingConnection(row)

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return pool.connection

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Acquire()


class ExplodingPool:
    def acquire(self):
        raise AssertionError("pool must not be touched when no ownership claim is presented")


@pytest.mark.asyncio
async def test_handoff_without_any_claim_never_touches_database(monkeypatch):
    """A bare session id is not a credential: fail closed before any query."""

    async def fake_get_pool():
        return ExplodingPool()

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)
    result = await postgres_chat_history.PostgresChatHistoryRepository().get_session_for_handoff(
        session_id="session-177", project_key="camellia", device_id=None, identity_key=None
    )
    assert result is None


@pytest.mark.asyncio
async def test_handoff_matches_session_on_either_claim_with_project_scope(monkeypatch):
    pool = RecordingPool(
        FakeRow(
            session_id="session-177",
            device_id="device-177",
            identity_key=None,
            project_key="camellia",
            title="Cho xem mat bang",
            message_count=4,
            handed_off=False,
            last_active_at=datetime.now(timezone.utc),
        )
    )

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)
    summary = await postgres_chat_history.PostgresChatHistoryRepository().get_session_for_handoff(
        session_id="session-177",
        project_key="camellia",
        device_id="device-177",
        identity_key=None,
    )
    assert summary is not None
    assert summary.session_id == "session-177"
    query = pool.connection.fetchrow_query
    assert "answer_mode IS NULL" in query
    assert "identity_key = $3" in query
    assert "device_id = $4" in query
    assert pool.connection.fetchrow_args == (
        "session-177",
        "camellia",
        None,
        "device-177",
    )


@pytest.mark.asyncio
async def test_lead_transcript_resolves_via_handoff_pointer_or_committed_claims(monkeypatch):
    """Read path: explicit pointer arm OR the lead's own committed claims."""
    pool = RecordingPool(FakeRow(session_id="session-177", project_key="camellia"))

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)
    result = await postgres_chat_history.PostgresChatHistoryRepository().get_session_for_lead(
        lead_id=177
    )
    assert result is not None
    session_id, project_key, messages = result
    assert session_id == "session-177"
    assert project_key == "camellia"
    assert messages[0].content == "Cho xem mat bang"
    query = pool.connection.fetchrow_query
    assert "JOIN leads l ON l.id = $1" in query
    assert "cs.lead_id = $1" in query
    assert "cs.session_id = l.session_id" in query
    assert "cs.project_key = l.project_key" in query
    assert "cs.device_id = l.device_id" in query
    assert "cs.answer_mode IS NULL" in query
    assert pool.connection.fetchrow_args == (177,)


@pytest.mark.asyncio
async def test_lead_transcript_returns_none_for_truly_unlinked_lead(monkeypatch):
    pool = RecordingPool(None)
    pool.connection.row = None

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)
    result = await postgres_chat_history.PostgresChatHistoryRepository().get_session_for_lead(
        lead_id=999
    )
    assert result is None
