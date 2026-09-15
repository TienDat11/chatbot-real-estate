"""GA-03 — Global assistant conversation persistence tests.

These tests exercise the global session persistence path via the repository
directly, using a stateful fake pool/connection that simulates real Postgres
row-level effects for the INSERT/CONFLICT/UPSERT and message inserts. The test
sequence proves the NULL -> camellia -> soleil -> camellia round-trip by
persisting four turns and reading them back through the retrieval primitives.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.application.services.chat_history_service import (
    ChatHistoryService,
    ChatMessage,
    GlobalSessionSummary,
)
from api.infrastructure.adapters import postgres_chat_history

GLOBAL_SENTINEL_KEY = postgres_chat_history.repository.GLOBAL_SENTINEL_KEY
GLOBAL_SESSION_MODE = postgres_chat_history.repository.GLOBAL_SESSION_MODE

_SEED_TS = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)


class _FakeRow(dict):
    """Minimal asyncpg.Record-shaped row."""


class _FakeConn:
    """Stateful fake that simulates the chat_sessions + chat_messages rows.

    It records every executed statement and produces observable side-effects
    that the read primitives (get_global_session, list_global_messages) can
    read back — so assertions are on persisted state, not SQL argument order.
    """

    def __init__(self):
        self.session = None  # dict or None for the chat_sessions row
        self.messages = []   # list of chat_messages dicts
        self._msg_seq = 0
        self.executed = []  # all (query, args) for forensic assertions

    def reset(self):
        self.session = None
        self.messages = []
        self._msg_seq = 0
        self.executed = []

    async def execute(self, query: str, *args):
        self.executed.append(("execute", query, args))
        q = query.strip().upper()
        if q.startswith("INSERT INTO CHAT_SESSIONS"):
            # Simulate ON CONFLICT: if no session exists, INSERT 0 1;
            # if it exists but ownership mismatches, INSERT 0 0
            # (Postgres never returns UPDATE 0 for INSERT...ON CONFLICT DO UPDATE WHERE).
            if self.session is None:
                self.session = {
                    "session_id": args[0],
                    "device_id": args[1],
                    "identity_key": args[2],
                    "project_key": args[3],
                    "title": args[4],
                    "message_count": 0,
                    "session_mode": args[5],
                    "active_project_key": None,
                    "handed_off": False,
                    "last_active_at": _SEED_TS,
                }
            # ON CONFLICT DO UPDATE WHERE ... ownership check
            # Simulate: matches only if device/identity match AND session_mode='global'
            match = (
                (self.session["device_id"] is not None and self.session["device_id"] == args[1] or
                 self.session["identity_key"] is not None and self.session["identity_key"] == args[2])
                and self.session["session_mode"] == "global"
            )
            if match:
                self.session["last_active_at"] = _SEED_TS
                return "INSERT 0 1"
            return "INSERT 0 0"

        if q.startswith("UPDATE CHAT_SESSIONS"):
            if self.session is None:
                return "UPDATE 0"
            self.session["last_active_at"] = _SEED_TS
            if "ACTIVE_PROJECT_KEY" in q:
                self.session["active_project_key"] = args[1]
            if "MESSAGE_COUNT" in q:
                self.session["message_count"] = (self.session.get("message_count") or 0) + 2
            return "UPDATE 1"

        return "OK"

    async def executemany(self, query: str, args_seq):
        self.executed.append(("executemany", query, args_seq))
        q = query.strip().upper()
        if q.startswith("INSERT INTO CHAT_MESSAGES"):
            for row_args in args_seq:
                # (session_id, project_key, role, content, meta)
                self.messages.append({
                    "id": self._msg_seq,
                    "session_id": row_args[0],
                    "project_key": row_args[1],
                    "role": row_args[2],
                    "content": row_args[3],
                    "meta": row_args[4],
                    "created_at": datetime(2026, 9, 14, 10, 0, self._msg_seq, tzinfo=timezone.utc),
                })

    def _owns_session(self, session_id, device_id, identity_key):
        """Replicate the adapter's ownership predicate for read-back assertions."""
        if self.session is None:
            return False
        if self.session["session_id"] != session_id:
            return False
        if self.session["session_mode"] != GLOBAL_SESSION_MODE:
            return False
        if device_id is None and identity_key is None:
            return False
        dev_match = device_id is not None and self.session["device_id"] == device_id
        ident_match = identity_key is not None and self.session["identity_key"] == identity_key
        return dev_match or ident_match

    async def fetchrow(self, query: str, *args):
        self.executed.append(("fetchrow", query, args))
        q = query.strip().upper()
        if q.startswith("INSERT INTO CHAT_SESSIONS") and "RETURNING" in q:
            # Simulate INSERT ... ON CONFLICT DO UPDATE WHERE ... RETURNING.
            # Returns the session_id row when the upsert touches a row (fresh
            # insert or ownership-matched conflict update); returns None when the
            # conflict clause WHERE is false (ownership mismatch — Postgres yields
            # INSERT 0 0 and no row for RETURNING).
            if self.session is None:
                self.session = {
                    "session_id": args[0],
                    "device_id": args[1],
                    "identity_key": args[2],
                    "project_key": args[3],
                    "title": args[4],
                    "message_count": 0,
                    "session_mode": args[5],
                    "active_project_key": None,
                    "handed_off": False,
                    "last_active_at": _SEED_TS,
                }
                return _FakeRow(session_id=args[0])
            # ON CONFLICT DO UPDATE WHERE ... ownership check
            match = (
                (self.session["device_id"] is not None and self.session["device_id"] == args[1] or
                 self.session["identity_key"] is not None and self.session["identity_key"] == args[2])
                and self.session["session_mode"] == GLOBAL_SESSION_MODE
            )
            if match:
                self.session["last_active_at"] = _SEED_TS
                return _FakeRow(session_id=args[0])
            return None

        if q.startswith("SELECT SESSION_ID") and "CHAT_SESSIONS" in q:
            session_id = args[0]
            identity_key = args[1]
            device_id = args[2]
            if not self._owns_session(session_id, device_id, identity_key):
                return None
            return _FakeRow(**{k: v for k, v in self.session.items()
                               if k in (
                                   "session_id", "device_id", "identity_key",
                                   "project_key", "session_mode",
                                   "active_project_key", "title",
                                   "message_count", "handed_off",
                                   "last_active_at",
                               )})
        return None

    async def fetch(self, query: str, *args):
        self.executed.append(("fetch", query, args))
        q = query.strip().upper()
        if q.startswith("SELECT ROLE") and "CHAT_MESSAGES" in q:
            session_id = args[0]
            identity_key = args[1]
            device_id = args[2]
            if not self._owns_session(session_id, device_id, identity_key):
                return []
            return [
                _FakeRow(
                    role=m["role"],
                    content=m["content"],
                    meta=m["meta"],
                    created_at=m["created_at"],
                    project_key=m["project_key"],
                )
                for m in self.messages if m["session_id"] == session_id
            ]
        return []


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnWithTx(_FakeConn):
    def transaction(self):
        return _Transaction()


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return pool._conn

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Acquire()


@pytest.fixture
def repo():
    return postgres_chat_history.PostgresChatHistoryRepository()


@pytest.fixture
def patch_pool(monkeypatch):
    """Return a helper that installs a stateful fake pool on the postgres adapter."""
    installed = {}

    def _install():
        conn = _FakeConnWithTx()
        pool = _FakePool(conn)
        installed["conn"] = conn
        installed["pool"] = pool

        async def fake_get_pool():
            return pool

        monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)
        return conn

    _install.persistent = installed
    return _install


# ---------------------------------------------------------------------------
# test_global_session_can_store_multiple_project_scopes
#
# Proves the NULL -> camellia -> soleil -> camellia round-trip: persist four
# turns (neutral, camellia, soleil, camellia) and read them back to verify
# each message's project_key and the session's active_project_key evolve
# correctly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_session_can_store_multiple_project_scopes(repo, monkeypatch):
    conn = _FakeConnWithTx()

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session_id = "g-sess-roundtrip"

    # Turn 1: neutral clarification — resolved_project_key = NULL
    assert await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key="ident-1",
        resolved_project_key=None,
        user_content="Bạn có dự án nào không?",
        assistant_content="Hiện có Camellia và Soleil.",
        assistant_meta={"images": []},
    ) is True

    # Turn 2: resolved Camellia
    assert await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key="ident-1",
        resolved_project_key="camellia",
        user_content="Camellia giá bao nhiêu?",
        assistant_content="42 tỷ.",
        assistant_meta={"images": []},
    ) is True

    # Turn 3: resolved Soleil
    assert await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key="ident-1",
        resolved_project_key="soleil",
        user_content="Soleil giá bao nhiêu?",
        assistant_content="36 tỷ.",
        assistant_meta={"images": []},
    ) is True

    # Turn 4: resolved Camellia again — round-trip back to camellia
    assert await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key="ident-1",
        resolved_project_key="camellia",
        user_content="Quay lại Camellia.",
        assistant_content="42 tỷ.",
        assistant_meta={"images": []},
    ) is True

    # --- Read back the session: active_project_key must be 'camellia' (last resolved) ---
    session = await repo.get_global_session(
        session_id=session_id, device_id="dev-1", identity_key="ident-1"
    )
    assert session is not None
    assert session.project_key == GLOBAL_SENTINEL_KEY
    assert session.session_mode == GLOBAL_SESSION_MODE
    assert session.active_project_key == "camellia"

    # --- Read back messages: each carries its own per-turn project_key ---
    messages = await repo.list_global_messages(
        session_id=session_id, device_id="dev-1", identity_key="ident-1"
    )
    assert len(messages) == 8  # 4 turns × 2 messages

    # NULL -> camellia -> soleil -> camellia (per user message)
    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) == 4
    assert user_msgs[0].project_key is None       # neutral
    assert user_msgs[1].project_key == "camellia"
    assert user_msgs[2].project_key == "soleil"
    assert user_msgs[3].project_key == "camellia"  # round-trip back

    # Assistant messages carry the same resolution as their user counterpart
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert [m.project_key for m in assistant_msgs] == [None, "camellia", "soleil", "camellia"]


@pytest.mark.asyncio
async def test_global_neutral_turn_does_not_replace_active_project(repo, monkeypatch):
    """A neutral turn (resolved_project_key=None) must not update active_project_key."""
    conn = _FakeConnWithTx()

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session_id = "g-sess-neutral"

    # First: a resolved turn sets active_project_key to camellia
    await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key=None,
        resolved_project_key="camellia",
        user_content="Camellia?",
        assistant_content="42tỷ.",
        assistant_meta={},
    )

    # Second: a neutral clarification turn — must NOT update active_project_key
    await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key=None,
        resolved_project_key=None,
        user_content="Và ở đâu nữa?",
        assistant_content="Chúng ta có thêm dự án Soleil.",
        assistant_meta={},
    )

    session = await repo.get_global_session(
        session_id=session_id, device_id="dev-1"
    )
    assert session is not None
    assert session.active_project_key == "camellia"


@pytest.mark.asyncio
async def test_global_resolved_turn_updates_active_project(repo, monkeypatch):
    """A resolved turn must update active_project_key to the resolved project."""
    conn = _FakeConnWithTx()

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session_id = "g-sess-resolved"

    # Neutral first — active_project_key should remain NULL
    await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key=None,
        resolved_project_key=None,
        user_content="Tư vấn?",
        assistant_content="Chọn dự án.",
        assistant_meta={},
    )

    session = await repo.get_global_session(
        session_id=session_id, device_id="dev-1"
    )
    assert session is not None
    assert session.active_project_key is None

    # Resolved to soleil — active_project_key must become soleil
    await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key=None,
        resolved_project_key="soleil",
        user_content="Soleil?",
        assistant_content="36tỷ.",
        assistant_meta={},
    )

    session = await repo.get_global_session(
        session_id=session_id, device_id="dev-1"
    )
    assert session is not None
    assert session.active_project_key == "soleil"


@pytest.mark.asyncio
async def test_global_session_rejects_foreign_identity(repo, monkeypatch):
    """A turn from a different device/identity than the session owner must fail
    closed: append_global_turn returns False, no messages are written, and the
    existing session data is untouched."""
    conn = _FakeConnWithTx()

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session_id = "g-sess-foreign"

    # Owner creates the session
    assert await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key="ident-1",
        resolved_project_key=None,
        user_content="hello",
        assistant_content="hi",
        assistant_meta={},
    ) is True

    messages_before_foreign = len(conn.messages)
    active_before = conn.session["active_project_key"]

    # Foreign caller tries with different device_id and identity_key
    result = await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-FOREIGN",
        identity_key="ident-FOREIGN",
        resolved_project_key="camellia",
        user_content="foreign turn",
        assistant_content="should not be written",
        assistant_meta={},
    )
    assert result is False  # ownership mismatch → false

    # No new messages written
    assert len(conn.messages) == messages_before_foreign

    # The session row was NOT created/mutated by the foreign attempt
    assert conn.session["active_project_key"] == active_before
    assert conn.session["device_id"] == "dev-1"
    assert conn.session["identity_key"] == "ident-1"

    # Bare append with no claims must also fail closed (no session, no messages)
    no_claim_result = await repo.append_global_turn(
        session_id="g-sess-no-claims",
        device_id=None,
        identity_key=None,
        resolved_project_key="camellia",
        user_content="no claims",
        assistant_content="should not persist",
        assistant_meta={},
    )
    assert no_claim_result is False
    # The no-claims attempt must not have created any session
    assert conn.session["session_id"] != "g-sess-no-claims"


@pytest.mark.asyncio
async def test_global_messages_preserve_project_key(repo, monkeypatch):
    """list_global_messages returns each message with its stored project_key,
    preserving NULL for neutral turns and the real key for resolved turns."""
    conn = _FakeConnWithTx()

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session_id = "g-sess-msgs"

    # Neutral turn
    await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key=None,
        resolved_project_key=None,
        user_content="?",
        assistant_content="Chọn dự án.",
        assistant_meta={},
    )
    # Resolved Camellia turn
    await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key=None,
        resolved_project_key="camellia",
        user_content="Camellia?",
        assistant_content="42tỷ.",
        assistant_meta={},
    )

    messages = await repo.list_global_messages(
        session_id=session_id, device_id="dev-1"
    )

    assert len(messages) == 4
    # user assistant order within each message insert batch
    assert messages[0].role == "user"
    assert messages[0].project_key is None
    assert messages[1].role == "assistant"
    assert messages[1].project_key is None
    assert messages[2].role == "user"
    assert messages[2].project_key == "camellia"
    assert messages[3].role == "assistant"
    assert messages[3].project_key == "camellia"


@pytest.mark.asyncio
async def test_global_parent_uses_reserved_global_container_key(repo, monkeypatch):
    """The global parent session must use project_key = '_global' and
    session_mode = 'global', with active_project_key starting NULL."""
    conn = _FakeConnWithTx()

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session_id = "g-sess-parent"

    await repo.append_global_turn(
        session_id=session_id,
        device_id="dev-1",
        identity_key="ident-1",
        resolved_project_key=None,
        user_content="hello",
        assistant_content="hi",
        assistant_meta={},
    )

    # Read back via get_global_session
    session = await repo.get_global_session(
        session_id=session_id, device_id="dev-1", identity_key="ident-1"
    )
    assert session is not None
    assert session.project_key == GLOBAL_SENTINEL_KEY  # '_global'
    assert session.session_mode == GLOBAL_SESSION_MODE  # 'global'
    assert session.active_project_key is None  # NULL on creation

    # Also verify the session table row directly
    assert conn.session is not None
    assert conn.session["project_key"] == GLOBAL_SENTINEL_KEY
    assert conn.session["session_mode"] == GLOBAL_SESSION_MODE
    assert conn.session["active_project_key"] is None

    # Bare lookup — no claims → must return None (fail closed)
    bare = await repo.get_global_session(
        session_id=session_id, device_id=None, identity_key=None
    )
    assert bare is None

    # Both device and identity foreign → must return None
    foreign_pair = await repo.get_global_session(
        session_id=session_id, device_id="dev-foreign", identity_key="ident-foreign"
    )
    assert foreign_pair is None


    # Foreign identity with valid device — under OR-of-claims, the valid
    # device_id authorizes the read (same as the legacy handoff precedent).
    # Verify the authorization succeeds and is the _global sentinel session.
    foreign_ident = await repo.get_global_session(
        session_id=session_id, device_id="dev-1", identity_key="ident-foreign"
    )
    assert foreign_ident is not None
    assert foreign_ident.project_key == GLOBAL_SENTINEL_KEY
