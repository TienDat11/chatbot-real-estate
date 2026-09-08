"""Reviewer blocker 1 — server-side verified sales auth for training mode.

The FE /train route is client-side role-gated, which is NOT a security
boundary: a caller can POST /query with answer_mode="training" directly.
These tests pin the server-side gate (api/interfaces/api/deps/admin.py
require_training_sales) on the real main.py route:

  anonymous/missing token              -> 401
  invalid (garbage/foreign-sign) token -> 401
  customer / admin / roleless token    -> 403 (sales-training contract is
                                          sales-only; no admin allowance)
  valid, active, mapped sales token    -> 200
  verified sales with no active PG row -> 403 (inactive/unmapped)

Normal-mode anonymous and customer queries stay untouched (no auth required),
and training responses keep their _training isolation / CTA suppression.

All offline: local-RSA JWKS verifier + fake PG sales mapping, no network/DB.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.application.services.query_quota_gate import UnmanagedTurnContext
from api.interfaces.api.main import create_app
from tests._auth_seams import MAPPED_SALES_ID_BY_UID, base_claims, mint_id_token
from tests._training_seams import install_training_seams


class _OfflineQuotaGate:
    """Test-only quota seam; production keeps the fail-closed gate unchanged."""

    async def prepare_turn(self, **kwargs):
        return UnmanagedTurnContext()

    async def refund_turn(self, context):
        return None


_PAYLOAD = {
    "answer": "lookup ok",
    "sources": [{"doc_id": "price-a", "kind": "price", "title": "T"}],
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
    """Answers any training query so the gate alone decides the status code."""

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        return dict(_PAYLOAD)


@pytest.fixture()
def client(monkeypatch, offline_auth_seams) -> TestClient:
    # Training preflight + persist read the registry/chat seams lazily at call
    # time; without these offline fakes the tests hit the real PG (pass on a
    # dev machine with DB access, fail in CI and offline runs).
    install_training_seams(monkeypatch)
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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sales_token(local_rsa_jwk: dict, uid: str = "uid-sales-mapped") -> str:
    return mint_id_token(local_rsa_jwk, base_claims(uid, "sales"))


# ---------------------------------------------------------------------------
# 401 family: training mode never runs without a valid bearer token.
# ---------------------------------------------------------------------------


def test_training_requires_bearer_token(client) -> None:
    resp = client.post("/query", json={"query": "coaching", "answer_mode": "training"})
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    assert "answer" not in resp.text


def test_training_rejects_garbage_token(client) -> None:
    resp = client.post(
        "/query",
        json={"query": "coaching", "answer_mode": "training"},
        headers=_bearer("not-a-jwt"),
    )
    assert resp.status_code == 401


def test_training_rejects_foreign_signed_token(
    client, local_rsa_jwk, foreign_rsa_private_key
) -> None:
    token = mint_id_token(
        local_rsa_jwk,
        base_claims("uid-sales-mapped", "sales"),
        signing_key=foreign_rsa_private_key,
    )
    resp = client.post(
        "/query",
        json={"query": "coaching", "answer_mode": "training"},
        headers=_bearer(token),
    )
    assert resp.status_code == 401


def test_training_rejects_expired_token(client, local_rsa_jwk) -> None:
    import time

    now = int(time.time())
    token = mint_id_token(
        local_rsa_jwk,
        base_claims("uid-sales-mapped", "sales", iat=now - 7200, exp=now - 3600),
    )
    resp = client.post(
        "/query",
        json={"query": "coaching", "answer_mode": "training"},
        headers=_bearer(token),
    )
    assert resp.status_code == 401


def test_training_sse_missing_token_is_unauthorized(client) -> None:
    resp = client.post(
        "/query",
        headers={"accept": "text/event-stream"},
        json={"query": "coaching", "answer_mode": "training"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 403 family: verified tokens carrying a non-sales role claim.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["customer", "admin", "viewer"])
def test_training_rejects_non_sales_role(client, local_rsa_jwk, role) -> None:
    token = mint_id_token(local_rsa_jwk, base_claims(f"uid-{role}", role))
    resp = client.post(
        "/query",
        json={"query": "coaching", "answer_mode": "training"},
        headers=_bearer(token),
    )
    assert resp.status_code == 403, resp.text


def test_training_rejects_roleless_token(client, local_rsa_jwk) -> None:
    """Authenticated but no role claim: 403, not 401 — identity is valid."""
    token = mint_id_token(local_rsa_jwk, base_claims("uid-roleless", None))
    resp = client.post(
        "/query",
        json={"query": "coaching", "answer_mode": "training"},
        headers=_bearer(token),
    )
    assert resp.status_code == 403


def test_training_rejects_sales_without_active_mapping(client, local_rsa_jwk) -> None:
    """A verified sales claim is denied without an active PG sales row."""
    token = _sales_token(local_rsa_jwk, uid="uid-sales-unmapped")
    resp = client.post(
        "/query",
        json={"query": "coaching", "answer_mode": "training"},
        headers=_bearer(token),
    )
    assert resp.status_code == 403
    assert "no active sales mapping" in resp.text


# ---------------------------------------------------------------------------
# 200 family: valid active mapped sales passes; normal-mode flow is preserved.
# ---------------------------------------------------------------------------


def test_training_allows_valid_mapped_sales(client, local_rsa_jwk) -> None:
    resp = client.post(
        "/query",
        json={
            "query": "coaching",
            "answer_mode": "training",
            "context": {"project_key": "camellia"},
            "session_id": "training-auth-valid",
        },
        headers=_bearer(_sales_token(local_rsa_jwk)),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["sources"][0]["kind"] == "price"
    # training isolation (CTA/persona suppression), NOT a separate corpus.
    assert body["lead_cta_hint"] is None
    assert body["project_redirect"] is None


def test_training_allows_each_mapped_sales_uid(client, local_rsa_jwk) -> None:
    for uid in MAPPED_SALES_ID_BY_UID:
        resp = client.post(
            "/query",
            json={
                "query": "coaching",
                "answer_mode": "training",
                "context": {"project_key": "camellia"},
                "session_id": f"training-auth-{uid}",
            },
            headers=_bearer(_sales_token(local_rsa_jwk, uid=uid)),
        )
        assert resp.status_code == 200, (uid, resp.text)


def test_normal_mode_anonymous_still_allowed(client) -> None:
    resp = client.post("/query", json={"query": "gia can", "project_key": "camellia"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"


def test_normal_mode_customer_token_still_allowed(client, local_rsa_jwk) -> None:
    token = mint_id_token(local_rsa_jwk, base_claims("uid-customer", "customer"))
    resp = client.post(
        "/query",
        json={"query": "gia can", "project_key": "camellia"},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
