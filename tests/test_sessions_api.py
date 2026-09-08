from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.chat_history_service import ChatMessage, ChatSessionSummary
from api.interfaces.api import sessions
from api.interfaces.api.main import create_app

_SECRET = "test-session-secret"
_TOKEN = __import__(
    "api.application.services.anon_identity", fromlist=["mint_anonymous_identity_token"]
).mint_anonymous_identity_token(
    _SECRET,
    now_epoch_seconds=1_700_000_000,
    subject="00000000-0000-4000-8000-000000000001",
)
_IDENTITY_SERVICE = AnonymousIdentityService(_SECRET, now_provider=lambda: 1_700_000_000)


class FakeHistoryService:
    def __init__(self) -> None:
        self.session_value = ChatSessionSummary(
            session_id="session-1",
            device_id="device-1",
            project_key="camellia",
            identity_key="00000000-0000-4000-8000-000000000001",
            title="Hello",
            message_count=2,
            handed_off=False,
            last_active_at=datetime.now(timezone.utc),
        )
        self.message_value = ChatMessage(
            role="user",
            content="Hello",
            meta={},
            created_at=datetime.now(timezone.utc),
        )

    async def session(
        self, *, session_id: str, device_id=None, project_key=None, identity_key=None, staff_scoped=False
    ):
        if session_id != "session-1":
            return None
        if (
            device_id != self.session_value.device_id
            or project_key != self.session_value.project_key
            or identity_key != self.session_value.identity_key
        ):
            return None
        return self.session_value

    async def messages(self, *, session_id: str):
        return [self.message_value]


@pytest.mark.asyncio
async def test_messages_require_device_header(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    with pytest.raises(HTTPException) as exc_info:
        await sessions.list_messages(
            "session-1",
            project_key="camellia",
            x_device_id=None,
            x_anon_token=None,
            identity_service=_IDENTITY_SERVICE,
            staff_principal=None,
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_messages_hide_sessions_owned_by_another_device(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    with pytest.raises(HTTPException) as exc_info:
        await sessions.list_messages(
            "session-1",
            project_key="camellia",
            x_device_id="device-2",
            x_anon_token=_TOKEN,
            identity_service=_IDENTITY_SERVICE,
            staff_principal=None,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_messages_return_transcript_for_owner(monkeypatch):
    monkeypatch.setattr(sessions, "_service", lambda: FakeHistoryService())

    response = await sessions.list_messages(
        "session-1",
        project_key="camellia",
        x_device_id="device-1",
        x_anon_token=_TOKEN,
        identity_service=_IDENTITY_SERVICE,
        staff_principal=None,
    )

    assert response["session_id"] == "session-1"
    assert response["messages"][0]["content"] == "Hello"


def test_crm_lead_conversation_route_not_registered_on_sessions_router() -> None:
    """Duplicate-route consolidation (reviewer blocker): GET
    /api/crm/leads/{lead_id}/conversation has exactly one authoritative
    registration (api.interfaces.api.crm_routes), owning auth, response shape,
    and meta sanitization; the sessions router must not shadow it with a
    second implementation whose behavior can drift."""
    shadowed = [
        route
        for route in sessions.router.routes
        if getattr(route, "path", "") == "/crm/leads/{lead_id}/conversation"
    ]
    assert shadowed == []


def _flattened_app_api_routes(app) -> list:
    """FastAPI >= 0.141 keeps included routers lazy (_IncludedRouter); flatten
    them so per-path registration counts can be asserted."""
    flattened: list = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            flattened.extend(original_router.routes)
        else:
            flattened.append(route)
    return flattened


def test_crm_lead_conversation_route_is_registered_exactly_once_on_the_app() -> None:
    """App-level guard for the same consolidation: no double registration of
    the CRM transcript route survives in the composed application."""
    app = create_app()
    registrations = [
        route
        for route in _flattened_app_api_routes(app)
        if getattr(route, "path", "") == "/api/crm/leads/{lead_id}/conversation"
        and "GET" in getattr(route, "methods", set())
    ]
    assert len(registrations) == 1
