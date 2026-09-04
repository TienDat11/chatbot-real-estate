"""Staff-principal hydration of customer chat sessions.

Sessions created by an authenticated sales principal via POST /query persist
with identity_key NULL (the principal's turn_context carries no anon subject).
Before the staff path existed, GET /api/sessions/{id}/messages and
GET /api/sessions required the anon identity triple, so those NULL rows could
never hydrate (404 / empty drawer). These tests pin the dual-surface contract:
staff bearer + X-Device-Id uses the device+project+answer_mode-IS-NULL scope,
while the anonymous path keeps its signed-identity requirements unchanged.

Offline: fake service/repository + FakePool SQL capture, no DB, no network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from api.application.services.anon_identity import (
    AnonymousIdentityService,
    mint_anonymous_identity_token,
)
from api.application.services.chat_history_service import (
    ChatHistoryService,
    ChatMessage,
    ChatSessionSummary,
)
from api.infrastructure.adapters import postgres_chat_history
from api.interfaces.api import sessions
from api.interfaces.api.deps import AuthenticatedPrincipal

_SECRET = "test-staff-hydration-secret"
_TOKEN = mint_anonymous_identity_token(
    _SECRET,
    now_epoch_seconds=1_700_000_000,
    subject="00000000-0000-4000-8000-000000000001",
)
_IDENTITY_SERVICE = AnonymousIdentityService(_SECRET, now_provider=lambda: 1_700_000_000)
_STAFF = AuthenticatedPrincipal(
    firebase_uid="uid-sales-mapped", email="s@example.com", role="sales", sales_id=777
)
_NOW = datetime.now(timezone.utc)


def _summary(session_id: str, device_id: str, identity_key: str | None) -> ChatSessionSummary:
    return ChatSessionSummary(
        session_id=session_id,
        device_id=device_id,
        project_key="camellia",
        identity_key=identity_key,
        title="Hello",
        message_count=2,
        handed_off=False,
        last_active_at=_NOW,
    )


class FakeHistoryService:
    """Mirrors the repository predicate semantics for both surfaces."""

    def __init__(self) -> None:
        # Staff-created row: identity_key NULL. Anon-owned row for device-1.
        self.session_value = _summary("session-staff", "device-1", None)
        self.message_value = ChatMessage(
            role="user", content="Hello", meta={}, created_at=_NOW
        )

    async def session(
        self, *, session_id, device_id=None, project_key=None, identity_key=None, staff_scoped=False
    ):
        if session_id != self.session_value.session_id:
            return None
        if device_id != self.session_value.device_id or project_key != self.session_value.project_key:
            return None
        if staff_scoped:
            # Staff branch ignores identity_key (rows may be NULL-owned).
            return self.session_value
        if identity_key != self.session_value.identity_key:
            return None
        return self.session_value

    async def sessions(self, *, device_id, project_key, identity_key=None):
        if device_id != "device-1" or project_key != "camellia":
            return []
        if identity_key is None:
            return [self.session_value]
        return []

    async def messages(self, *, session_id: str):
        return [self.message_value]


@pytest.mark.asyncio
async def test_staff_bearer_hydrates_null_identity_session(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    response = await sessions.list_messages(
        "session-staff",
        project_key="camellia",
        x_device_id="device-1",
        x_anon_token=None,
        identity_service=_IDENTITY_SERVICE,
        staff_principal=_STAFF,
    )

    assert response["session_id"] == "session-staff"
    assert response["messages"][0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_staff_bearer_mismatched_device_is_404(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    with pytest.raises(HTTPException) as exc_info:
        await sessions.list_messages(
            "session-staff",
            project_key="camellia",
            x_device_id="device-2",
            x_anon_token=None,
            identity_service=_IDENTITY_SERVICE,
            staff_principal=_STAFF,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_no_bearer_no_anon_headers_stays_401(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    with pytest.raises(HTTPException) as exc_info:
        await sessions.list_messages(
            "session-staff",
            project_key="camellia",
            x_device_id=None,
            x_anon_token=None,
            identity_service=_IDENTITY_SERVICE,
            staff_principal=None,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Signed anonymous identity is required"


@pytest.mark.asyncio
async def test_anon_path_with_valid_identity_unchanged(monkeypatch):
    class AnonOwnedService(FakeHistoryService):
        def __init__(self) -> None:
            super().__init__()
            self.session_value = _summary(
                "session-staff", "device-1", "00000000-0000-4000-8000-000000000001"
            )

    monkeypatch.setattr(sessions, "_service", lambda: AnonOwnedService())

    response = await sessions.list_messages(
        "session-staff",
        project_key="camellia",
        x_device_id="device-1",
        x_anon_token=_TOKEN,
        identity_service=_IDENTITY_SERVICE,
        staff_principal=None,
    )

    assert response["messages"][0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_staff_list_sessions_is_device_scoped(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    response = await sessions.list_sessions(
        project_key="camellia",
        x_device_id="device-1",
        x_anon_token=None,
        identity_service=_IDENTITY_SERVICE,
        staff_principal=_STAFF,
    )

    assert [item["session_id"] for item in response["sessions"]] == ["session-staff"]


@pytest.mark.asyncio
async def test_anon_list_sessions_requires_identity(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    with pytest.raises(HTTPException) as exc_info:
        await sessions.list_sessions(
            project_key="camellia",
            x_device_id="device-1",
            x_anon_token=None,
            identity_service=_IDENTITY_SERVICE,
            staff_principal=None,
        )

    assert exc_info.value.status_code == 401


# ----- repository-level staff branch (SQL predicate capture) -----------------


class FakeRow(dict):
    pass


class CapturingConnection:
    def __init__(self, row=None):
        self.queries: list[tuple[str, tuple]] = []
        self.row = row

    async def fetchrow(self, query: str, *args):
        self.queries.append((query, args))
        return self.row

    async def fetch(self, query: str, *args):
        self.queries.append((query, args))
        return []


class FakePool:
    def __init__(self, connection: CapturingConnection):
        self.connection = connection

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return pool.connection

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Acquire()


def _staff_row():
    return FakeRow(
        session_id="session-staff",
        device_id="device-1",
        identity_key=None,
        project_key="camellia",
        title="Hello",
        message_count=2,
        handed_off=False,
        last_active_at=_NOW,
    )


@pytest.mark.asyncio
async def test_repository_staff_branch_scopes_by_device_project_and_null_answer_mode(monkeypatch):
    conn = CapturingConnection(row=_staff_row())

    async def fake_get_pool():
        return FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session = await postgres_chat_history.PostgresChatHistoryRepository().get_session(
        session_id="session-staff",
        device_id="device-1",
        project_key="camellia",
        identity_key=None,
        staff_scoped=True,
    )

    assert session is not None
    query, args = conn.queries[0]
    assert args == ("session-staff", "device-1", "camellia")
    assert "answer_mode IS NULL" in query
    assert "identity_key" not in query.split("WHERE")[1]


@pytest.mark.asyncio
async def test_repository_anon_triple_branch_unchanged(monkeypatch):
    conn = CapturingConnection(row=None)

    async def fake_get_pool():
        return FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)

    session = await postgres_chat_history.PostgresChatHistoryRepository().get_session(
        session_id="session-1",
        device_id="device-1",
        project_key="camellia",
        identity_key="identity-1",
    )

    assert session is None
    query, args = conn.queries[0]
    assert args == ("session-1", "device-1", "identity-1", "camellia")
    assert "identity_key=$3" in query


@pytest.mark.asyncio
async def test_service_forwards_staff_scoped_flag(monkeypatch):
    conn = CapturingConnection(row=_staff_row())

    async def fake_get_pool():
        return FakePool(conn)

    monkeypatch.setattr(postgres_chat_history, "get_lead_pool", fake_get_pool)
    service = ChatHistoryService(postgres_chat_history.PostgresChatHistoryRepository())

    session = await service.session(
        session_id="session-staff",
        device_id="device-1",
        project_key="camellia",
        identity_key=None,
        staff_scoped=True,
    )

    assert session is not None
    assert session.identity_key is None
