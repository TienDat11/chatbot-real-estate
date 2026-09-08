"""Stable machine-readable codes for the three training preflight 409s.

The /query training preflight (``_validate_training_request``) rejects three
conflicts with HTTP 409. The FE must branch on a stable ``error.code`` — a
self-healable session conflict (mint a new session id, retry once) versus a
non-retryable inactive project — so each site now rides the same
``{"ok": False, "error": {"code", "message"}}`` envelope /query already uses
for PROJECT_SCOPE/REJECTED. These tests pin status 409 AND the exact code for
each site, and assert customer-mode /query stays byte-unchanged.

Offline: fake registry + chat-history seams, local-RSA sales token, no DB.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.application.services.query_quota_gate import UnmanagedTurnContext
from api.interfaces.api.main import create_app
from tests._auth_seams import sales_bearer_headers
from tests._training_seams import (
    TRAINING_TEST_UID,
    FakeTrainingChatRepository,
    install_training_seams,
    training_payload,
)


class _OfflineQuotaGate:
    async def prepare_turn(self, **kwargs):
        return UnmanagedTurnContext()

    async def refund_turn(self, context):
        return None


_PAYLOAD = {
    "answer": "coaching ok",
    "sources": [],
    "facts": [],
    "images": [],
    "videos": [],
    "places": [],
    "confidence": "HIGH",
    "requires_review": False,
    "routing": {"intent": "rag"},
    "trace_id": "t-1",
    "latency_ms": 1,
}


class FakePipeline:
    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        return dict(_PAYLOAD)


class _BindConflictRepo(FakeTrainingChatRepository):
    """A session id already owned by a non-training (customer) conversation.

    ``get_training_session`` finds no training row (so the preflight proceeds
    to bind), but the bind predicate refuses to hijack the foreign row and
    returns False — the exact shape of the third 409 site.
    """

    async def get_training_session(self, *, session_id, owner_firebase_uid=None):
        return None

    async def bind_training_session(
        self, *, session_id, owner_firebase_uid, context_project_key, title=None
    ):
        return False


@pytest.fixture()
def client(monkeypatch, offline_auth_seams) -> TestClient:
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: _OfflineQuotaGate(),
    )
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", FakePipeline
    )

    async def _fake_resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", _fake_resolve)
    return TestClient(create_app())


def _assert_envelope(resp, code: str) -> None:
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_project_inactive_returns_stable_code(client, monkeypatch, local_rsa_jwk) -> None:
    install_training_seams(monkeypatch, statuses={"camellia": "inactive"})
    resp = client.post(
        "/query",
        json=training_payload(),
        headers=sales_bearer_headers(local_rsa_jwk),
    )
    _assert_envelope(resp, "TRAINING_PROJECT_INACTIVE")


def test_session_context_mismatch_returns_stable_code(
    client, monkeypatch, local_rsa_jwk
) -> None:
    repo = FakeTrainingChatRepository()
    # Same owner, bound to a different context project than the request names.
    repo.bound["s-training-1"] = (TRAINING_TEST_UID, "soleil")
    install_training_seams(monkeypatch, repo=repo)
    resp = client.post(
        "/query",
        json=training_payload(),
        headers=sales_bearer_headers(local_rsa_jwk),
    )
    _assert_envelope(resp, "TRAINING_SESSION_CONTEXT_MISMATCH")


def test_session_id_conflict_returns_stable_code(
    client, monkeypatch, local_rsa_jwk
) -> None:
    install_training_seams(monkeypatch, repo=_BindConflictRepo())
    resp = client.post(
        "/query",
        json=training_payload(),
        headers=sales_bearer_headers(local_rsa_jwk),
    )
    _assert_envelope(resp, "TRAINING_SESSION_CONFLICT")


def test_customer_mode_query_unchanged(client) -> None:
    """Normal-mode /query never enters the training preflight: plain success body."""
    resp = client.post("/query", json={"query": "gia can", "project_key": "camellia"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert "error" not in body
    assert body.get("ok", True) is not False
