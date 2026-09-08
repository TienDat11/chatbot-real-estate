"""Route-level CTA state isolation matrix for JSON and SSE query paths."""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from api.application.services.anon_identity import (
    AnonymousIdentityService,
    mint_anonymous_identity_token,
)
from api.interfaces.api import main

_SECRET = secrets.token_urlsafe(48)
_OWNER_IDENTITY = "00000000-0000-4000-8000-000000000001"
_OTHER_IDENTITY = "00000000-0000-4000-8000-000000000002"
_OWNER_TOKEN = mint_anonymous_identity_token(_SECRET, subject=_OWNER_IDENTITY)
_OTHER_TOKEN = mint_anonymous_identity_token(_SECRET, subject=_OTHER_IDENTITY)
_CTA = "Anh/chị để lại số điện thoại nhé"


class _TurnContext:
    def __init__(
        self, identity_key: str, *, authenticated: bool = False, minted: str | None = None
    ):
        self.identity_key = identity_key
        self.project_key = "camellia"
        self.anon_token = minted
        self.anon_token_is_newly_minted = minted is not None
        self._authenticated = authenticated

    def quota_payload(self) -> dict:
        return {
            "used_turns": None,
            "remaining_turns": None,
            "cap": None,
            "is_authenticated": self._authenticated,
            "bonus_granted": None,
        }


class _Gate:
    def __init__(self, identity: str, *, authenticated: bool = False, minted: str | None = None):
        self.identity = identity
        self.authenticated = authenticated
        self.minted = minted

    async def prepare_turn(self, **kwargs):
        return _TurnContext(self.identity, authenticated=self.authenticated, minted=self.minted)

    async def refund_turn(self, context):
        return None


class _Pipeline:
    crash = False
    persisted = None

    async def run(self, query, session_id, as_of, history, **kwargs):
        if self.crash:
            raise RuntimeError("must not reach client")
        return {
            "answer": "answer",
            "sources": [],
            "facts": [],
            "places": [],
            "confidence": "HIGH",
            "requires_review": False,
            "routing": {"intent": "rag"},
            "trace_id": "trace",
            "latency_ms": 1,
        }


@pytest.fixture()
def route_harness(monkeypatch):
    calls: list[dict] = []
    pipeline = _Pipeline()
    persisted = {"value": None}

    async def resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    async def get_cta_state(**scope):
        calls.append(scope)
        if scope == {
            "session_id": "session-owner",
            "device_id": "device-owner",
            "project_key": "camellia",
            "identity_key": _OWNER_IDENTITY,
        }:
            return 3, False, False
        return 0, False, False

    async def persist_turn(self, **kwargs):
        return persisted["value"]

    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", resolve)
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", lambda: pipeline
    )
    monkeypatch.setattr(
        "api.infrastructure.adapters.postgres_chat_history.repository.get_cta_state", get_cta_state
    )
    monkeypatch.setattr(
        "api.application.services.chat_history_service.ChatHistoryService.persist_turn",
        persist_turn,
    )
    monkeypatch.setattr(
        main, "get_cfg", lambda key, default=None: 3 if key == "lead_cta_after_turns" else default
    )

    def client_for(
        token: str | None, device="device-owner", project="camellia", *, auth=False, invalid=False
    ):
        identity = _OWNER_IDENTITY if token == _OWNER_TOKEN and not invalid else _OTHER_IDENTITY
        minted = None
        if token is None or invalid or token not in {_OWNER_TOKEN, _OTHER_TOKEN}:
            minted = mint_anonymous_identity_token(_SECRET, subject=_OTHER_IDENTITY)
        monkeypatch.setattr(
            "api.application.services.query_quota_gate.build_query_quota_gate",
            lambda: _Gate(identity, authenticated=auth, minted=minted),
        )
        monkeypatch.setattr(
            "api.interfaces.api.anon_routes.get_anonymous_identity_service",
            lambda: AnonymousIdentityService(_SECRET),
        )
        client = TestClient(main.create_app())
        client._cta_authenticated = auth
        return client

    client_for.persisted = persisted
    return client_for, calls, pipeline


def _request(
    client: TestClient,
    *,
    sse: bool = False,
    device: str = "device-owner",
    project: str = "camellia",
):
    headers = {"Accept": "text/event-stream"} if sse else {}
    if getattr(client, "_cta_authenticated", False):
        headers["Authorization"] = "Bearer test-authenticated-caller"
    return client.post(
        "/query",
        headers=headers,
        json={
            "query": "hello",
            "session_id": "session-owner",
            "device_id": device,
            "project_key": project,
        },
    )


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
def test_owner_gets_cta_and_repository_receives_complete_scope(route_harness, sse):
    client_for, calls, _ = route_harness
    response = _request(client_for(_OWNER_TOKEN), sse=sse)
    assert response.status_code == 200
    assert _CTA in response.text
    assert calls[-1] == {
        "session_id": "session-owner",
        "device_id": "device-owner",
        "project_key": "camellia",
        "identity_key": _OWNER_IDENTITY,
    }


@pytest.mark.parametrize(
    "label,kwargs",
    [
        ("cross-device", {"device": "device-other"}),
        ("cross-project", {"project": "soleil"}),
        ("cross-identity", {"token": _OTHER_TOKEN}),
        ("missing-token", {"token": None}),
        ("invalid-token", {"token": "invalid"}),
    ],
)
def test_non_owner_scope_never_receives_cta_state(route_harness, label, kwargs):
    client_for, calls, _ = route_harness
    token = kwargs.pop("token", _OWNER_TOKEN)
    response = _request(
        client_for(token, **kwargs),
        device=kwargs.get("device", "device-owner"),
        project=kwargs.get("project", "camellia"),
    )
    assert response.status_code == 200, label
    assert _CTA not in response.text
    if label in {"missing-token", "invalid-token"}:
        assert "anon_token" in response.text
    assert "handed_off" not in response.text and "phone_given" not in response.text
    assert calls[-1]["session_id"] == "session-owner"
    assert set(calls[-1]) == {"session_id", "device_id", "project_key", "identity_key"}


@pytest.mark.parametrize("persisted, expected", [(None, False), (False, False), (True, True)])
@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
def test_history_persisted_is_explicit_boolean_in_both_transports(
    route_harness, persisted, expected, sse
):
    client_for, _, _ = route_harness
    client_for.persisted["value"] = persisted
    response = _request(client_for(_OWNER_TOKEN), sse=sse)
    assert response.status_code == 200
    if sse:
        done = next(
            __import__("json").loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ") and '"history_persisted"' in line
        )
        assert done["history_persisted"] is expected
    else:
        assert response.json()["history_persisted"] is expected


def test_authenticated_caller_suppresses_anonymous_cta(route_harness):
    client_for, calls, _ = route_harness
    response = _request(client_for(_OWNER_TOKEN, auth=True))
    assert response.status_code == 200
    assert _CTA not in response.text
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turns, expected",
    [
        (2, None),
        (
            3,
            (
                "Anh/chị để lại số điện thoại nhé, chuyên viên gọi lại trong ~5 phút "
                "để tư vấn căn phù hợp."
            ),
        ),
    ],
    ids=["below-configured-turn-count", "at-configured-turn-count"],
)
async def test_durable_cta_hint_is_emitted_only_at_configured_completed_turn_count(
    monkeypatch, turns, expected
):
    """The authorized durable lookup is the post-stream completion contract."""
    calls = []

    async def get_cta_state(**scope):
        calls.append(scope)
        return turns, False, False

    monkeypatch.setattr(
        "api.infrastructure.adapters.postgres_chat_history.repository.get_cta_state",
        get_cta_state,
    )
    monkeypatch.setattr(
        main, "get_cfg", lambda key, default=None: 3 if key == "lead_cta_after_turns" else default
    )

    result = await main._durable_lead_cta_hint(
        "session-owner",
        device_id="device-owner",
        project_key="camellia",
        identity_key=_OWNER_IDENTITY,
        authenticated=False,
    )

    assert result == expected
    assert calls == [
        {
            "session_id": "session-owner",
            "device_id": "device-owner",
            "project_key": "camellia",
            "identity_key": _OWNER_IDENTITY,
        }
    ]


@pytest.mark.asyncio
async def test_durable_cta_hint_never_overrides_handoff_or_phone_policy(monkeypatch):
    async def get_cta_state(**scope):
        return 3, scope["session_id"] == "handoff", scope["session_id"] == "phone"

    monkeypatch.setattr(
        "api.infrastructure.adapters.postgres_chat_history.repository.get_cta_state",
        get_cta_state,
    )
    monkeypatch.setattr(main, "get_cfg", lambda key, default=None: 3)

    for session_id in ("handoff", "phone"):
        assert (
            await main._durable_lead_cta_hint(
                session_id,
                device_id="device-owner",
                project_key="camellia",
                identity_key=_OWNER_IDENTITY,
                authenticated=False,
            )
            is None
        )


@pytest.mark.parametrize("sse", [True, False], ids=["json", "sse"])
def test_error_frames_contain_no_cta_state(route_harness, sse):
    client_for, _, pipeline = route_harness
    pipeline.crash = True
    response = _request(client_for(_OWNER_TOKEN), sse=sse)
    assert response.status_code == (200 if sse else 500)
    if sse:
        assert "event: error" in response.text and "event: done" in response.text
    assert _CTA not in response.text
    assert "session-owner" not in response.text
    assert "phone_given" not in response.text
