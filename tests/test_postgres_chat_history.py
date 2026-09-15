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

# --- GA-03 real-DB regression: RETURNING-based ownership guard (no string tag) ---
#
# This test proves the fix for the INSERT...ON CONFLICT DO UPDATE WHERE guard.
# Postgres returns 'INSERT 0 0' (NOT 'UPDATE 0') when the conflict clause WHERE
# is false, so checking the command tag string == "UPDATE 0" is dead code that
# never fires. The correct fix adds RETURNING session_id and checks row is None.
#
# To prove the fix is real (not just a fake that says so), this test runs
# against a live PostgreSQL with the actual asyncpg adapter path.

import asyncpg


def _pg_admin_params():
    """Probe parameters for a local PostgreSQL connection (admin-level)."""
    try:
        from api.infrastructure.config.config import get_settings
        s = get_settings()
        return {
            "host": s.postgres_host,
            "port": s.postgres_port,
            "user": s.postgres_user,
            "password": s.postgres_password,
            "database": "postgres",
            "connect_timeout": 5,
        }
    except Exception:
        return None


def _real_db_available():
    """Probe whether a PostgreSQL is reachable for integration tests."""
    psycopg2 = pytest.importorskip("psycopg2")
    params = _pg_admin_params()
    if params is None:
        return None
    try:
        conn = psycopg2.connect(**params)
        conn.close()
        return params
    except Exception:
        return None



# Core schema for the global session ownership regression.
# Deliberately minimal: only the columns the adapter touches in append_global_turn.
_GA03_TEST_SCHEMA = """
CREATE TEMP TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    device_id TEXT NULL,
    identity_key TEXT NULL,
    project_key TEXT NOT NULL,
    title TEXT NULL,
    message_count INT NOT NULL DEFAULT 0,
    handed_off BOOL NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_mode TEXT NOT NULL DEFAULT 'project',
    active_project_key TEXT NULL,
    answer_mode TEXT NULL
);

CREATE TEMP TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    project_key TEXT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@pytest.mark.asyncio
async def test_global_turn_ownership_guard_real_db(monkeypatch):
    """Integration proof on real PostgreSQL: a foreign device/identity cannot
    mutate an existing global session created by a different owner.

    The guard uses RETURNING session_id + row-is-None; a stale '== "UPDATE 0"'
    guard would silently allow the write. This test FAILS if the guard is
    reverted to the string-tag comparison.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    params = _real_db_available()
    if params is None:
        pytest.skip("local PostgreSQL not reachable for ownership-guard regression")

    # Use the existing database with TEMP tables — no database-level DDL required,
    # so this works against managed poolers (Supabase) that restrict CREATE DATABASE.
    import urllib.parse
    pw = urllib.parse.quote(params["password"], safe="")
    dsn = (
        f"postgresql://{params['user']}:{pw}"
        f"@{params['host']}:{params['port']}/{params['database']}"
    )

    async with asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=1,
    ) as pool:
        async with pool.acquire() as conn:
            await conn.execute(_GA03_TEST_SCHEMA)

        # Monkeypatch the adapter to use our real test pool.
        async def _fake_get_pool():
            return pool

        monkeypatch.setattr(postgres_chat_history, "get_lead_pool", _fake_get_pool)

        repo = postgres_chat_history.PostgresChatHistoryRepository()

        # Owner creates the global session.
        ok = await repo.append_global_turn(
            session_id="g-real-sess",
            device_id="dev-owner",
            identity_key="ident-owner",
            resolved_project_key=None,
            user_content="hello",
            assistant_content="hi",
            assistant_meta={},
        )
        assert ok is True

        # Foreign caller (different device AND different identity) attempts
        # to append a turn into the same session id.
        result = await repo.append_global_turn(
            session_id="g-real-sess",
            device_id="dev-FOREIGN",
            identity_key="ident-FOREIGN",
            resolved_project_key="camellia",
            user_content="foreign?",
            assistant_content="should not persist",
            assistant_meta={},
        )
        assert result is False, "foreign caller must be rejected (RETURNING guard)"

        # Verify no foreign messages landed in the table.
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id = $1", "g-real-sess"
            )
            assert count == 2, f"foreign messages leaked: {count} rows"

        # Verify the session row is still owned by the original owner.
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT device_id, identity_key, project_key, session_mode "
                "FROM chat_sessions WHERE session_id = $1", "g-real-sess"
            )
            assert row["device_id"] == "dev-owner"
            assert row["identity_key"] == "ident-owner"
            assert row["project_key"] == "_global"
            assert row["session_mode"] == "global"
