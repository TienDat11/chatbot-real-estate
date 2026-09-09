"""Reviewer blocker — bearer handling on the greeting endpoints.

POST /llms-hello and POST /llms-hello/stream previously silently downgraded
any caller presenting an invalid/expired/malformed bearer token to the
anonymous (customer) audience. The security contract now is:

  absent Authorization        -> anonymous customer greeting (200)
  any presented invalid token -> 401 (garbage, expired, foreign-signed,
                                 non-bearer scheme, empty bearer)
  valid sales/admin token     -> sales greeting, unchanged behavior
  valid customer/roleless     -> customer greeting, unchanged behavior

All offline: local-RSA JWKS verifier + fake media/LLM seams, no network/DB.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from api.interfaces.api import hello as hello_mod
from api.interfaces.api.main import create_app
from tests._auth_seams import base_claims, mint_id_token

# Audience markers from _audience_content (static fallback greeting streams
# when the fake LLM fails, so the marker decides the audience in the body).
_SALES_MARKER = "em hỗ trợ tra cứu nội bộ"
_CUSTOMER_MARKER = "em chào Anh/Chị"


class _FailingLLM:
    """Greeting falls back to the static audience greeting on any call."""

    async def complete(self, messages, **kwargs):
        raise RuntimeError("no LLM in offline tests")

    async def stream(self, messages, **kwargs):
        raise RuntimeError("no LLM in offline tests")
        yield  # pragma: no cover — async generator contract


@pytest.fixture()
def client(monkeypatch, offline_auth_seams) -> TestClient:
    """Real app with offline auth + fake LLM/media so 200s never touch IO."""
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FailingLLM())

    async def _no_images(*args, **kwargs):
        return []

    monkeypatch.setattr(hello_mod, "search_project_images", _no_images)
    monkeypatch.setattr(hello_mod, "fetch_recent_project_images", _no_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", lambda *a, **k: [])
    return TestClient(create_app())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(local_rsa_jwk: dict, uid: str, role: str | None, **overrides) -> str:
    return mint_id_token(local_rsa_jwk, base_claims(uid, role, **overrides))


# ---------------------------------------------------------------------------
# 401 family — a presented credential must fail closed, never downgrade.
# ---------------------------------------------------------------------------


def test_hello_stream_rejects_garbage_token(client) -> None:
    resp = client.post("/llms-hello/stream", headers=_bearer("not-a-jwt"))
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert _CUSTOMER_MARKER not in resp.text
    assert _SALES_MARKER not in resp.text


def test_hello_stream_rejects_empty_bearer(client) -> None:
    resp = client.post("/llms-hello/stream", headers=_bearer(""))
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_hello_stream_rejects_bearer_without_scheme(client) -> None:
    resp = client.post("/llms-hello/stream", headers={"Authorization": "Bearer"})
    assert resp.status_code == 401


def test_hello_stream_rejects_non_bearer_scheme(client) -> None:
    resp = client.post("/llms-hello/stream", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_hello_stream_rejects_expired_token(client, local_rsa_jwk) -> None:
    token = _token(
        local_rsa_jwk,
        "uid-sales-mapped",
        "sales",
        iat=int(time.time()) - 120,
        exp=int(time.time()) - 60,
    )
    resp = client.post("/llms-hello/stream", headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_hello_stream_rejects_foreign_signed_token(
    client, local_rsa_jwk, foreign_rsa_private_key
) -> None:
    token = mint_id_token(
        local_rsa_jwk,
        base_claims("uid-sales-mapped", "sales"),
        signing_key=foreign_rsa_private_key,
    )
    resp = client.post("/llms-hello/stream", headers=_bearer(token))
    assert resp.status_code == 401


def test_hello_rejects_invalid_token(client) -> None:
    resp = client.post("/llms-hello", headers=_bearer("not-a-jwt"))
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# Anonymous mode — only a MISSING Authorization header selects it.
# ---------------------------------------------------------------------------


def test_hello_stream_absent_token_is_anonymous_customer(client) -> None:
    resp = client.post("/llms-hello/stream")
    assert resp.status_code == 200
    assert _CUSTOMER_MARKER in resp.text
    assert _SALES_MARKER not in resp.text


def test_hello_absent_token_is_anonymous_customer(client) -> None:
    resp = client.post("/llms-hello")
    assert resp.status_code == 200
    assert resp.json()["audience"] == "customer"


# ---------------------------------------------------------------------------
# Valid tokens — role behavior unchanged.
# ---------------------------------------------------------------------------


def test_hello_stream_valid_sales_gets_sales_greeting(client, local_rsa_jwk) -> None:
    token = _token(local_rsa_jwk, "uid-sales-mapped", "sales")
    resp = client.post("/llms-hello/stream", headers=_bearer(token))
    assert resp.status_code == 200
    assert _SALES_MARKER in resp.text
    assert _CUSTOMER_MARKER not in resp.text


def test_hello_stream_valid_admin_gets_sales_greeting(client, local_rsa_jwk) -> None:
    token = _token(local_rsa_jwk, "uid-admin", "admin")
    resp = client.post("/llms-hello/stream", headers=_bearer(token))
    assert resp.status_code == 200
    assert _SALES_MARKER in resp.text


def test_hello_stream_valid_customer_keeps_customer_greeting(
    client, local_rsa_jwk
) -> None:
    token = _token(local_rsa_jwk, "uid-customer", "customer")
    resp = client.post("/llms-hello/stream", headers=_bearer(token))
    assert resp.status_code == 200
    assert _CUSTOMER_MARKER in resp.text
    assert _SALES_MARKER not in resp.text


def test_hello_stream_roleless_token_stays_customer(client, local_rsa_jwk) -> None:
    token = _token(local_rsa_jwk, "uid-roleless", None)
    resp = client.post("/llms-hello/stream", headers=_bearer(token))
    assert resp.status_code == 200
    assert _CUSTOMER_MARKER in resp.text
    assert _SALES_MARKER not in resp.text


def test_hello_valid_sales_gets_sales_audience(client, local_rsa_jwk) -> None:
    token = _token(local_rsa_jwk, "uid-sales-mapped", "sales")
    resp = client.post("/llms-hello", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["audience"] == "sales"


def test_hello_valid_admin_gets_sales_audience(client, local_rsa_jwk) -> None:
    token = _token(local_rsa_jwk, "uid-admin", "admin")
    resp = client.post("/llms-hello", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["audience"] == "sales"


def test_hello_valid_customer_keeps_customer_audience(client, local_rsa_jwk) -> None:
    token = _token(local_rsa_jwk, "uid-customer", "customer")
    resp = client.post("/llms-hello", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["audience"] == "customer"
