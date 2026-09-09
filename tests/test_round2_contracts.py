from __future__ import annotations

import json

import httpx
import pytest
from fastapi.security import HTTPAuthorizationCredentials

from api.infrastructure.adapters.firebase_sales_provisioner import FirebaseSalesProvisioner
from api.infrastructure.ports.firebase_auth import VerifiedFirebaseUser
from api.interfaces.api.deps.admin import resolve_authenticated_principal


class _Verifier:
    async def verify_id_token(self, _: str) -> VerifiedFirebaseUser:
        return VerifiedFirebaseUser(
            firebase_uid="customer-1",
            email="c@example.test",
            email_verified=True,
            role=None,
            auth_provider="password",
            token_issued_at=None,
            token_expires_at=None,
        )


@pytest.mark.asyncio
async def test_missing_role_claim_defaults_to_customer(monkeypatch):
    async def no_sales(_: str) -> int | None:
        return None

    monkeypatch.setattr("api.interfaces.api.deps.admin.resolve_assigned_sales_id", no_sales)
    principal = await resolve_authenticated_principal(
        frozenset({"customer"}),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
        _Verifier(),
    )
    assert principal.role == "customer"


@pytest.mark.asyncio
async def test_sales_provisioner_is_idempotent_for_retries(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "api.infrastructure.adapters.firebase_sales_provisioner.jwt.encode",
        lambda *args, **kwargs: "assertion",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "token"})
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provisioner = FirebaseSalesProvisioner(
        project_id="project",
        client_email="sa@example.test",
        private_key="unused",
        http_client=client,
    )
    # The two retries intentionally perform the same upserts; the adapter never creates
    # a second document or account because both operations address stable Firebase IDs.
    await provisioner.provision(firebase_uid="uid-1", full_name="Sales")
    await provisioner.provision(firebase_uid="uid-1", full_name="Sales")
    assert [method for method, _ in calls] == ["GET", "POST", "PATCH", "GET", "POST", "PATCH"]
    assert all("uid-1" in url or "accounts:update" in url for _, url in calls)


@pytest.mark.asyncio
async def test_sales_profile_contract_has_active_flag_and_revoke_fails_closed(monkeypatch):
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "api.infrastructure.adapters.firebase_sales_provisioner.jwt.encode",
        lambda *args, **kwargs: "assertion",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provisioner = FirebaseSalesProvisioner(
        project_id="project",
        client_email="sa@example.test",
        private_key="unused",
        http_client=client,
    )
    await provisioner.provision(firebase_uid="uid-1", full_name="Sales")
    assert json.loads(requests[3].content)["fields"]["is_active"] == {"booleanValue": True}
    await provisioner.revoke(firebase_uid="uid-1")
    assert requests[6].method == "PATCH"
    assert json.loads(requests[6].content)["fields"]["is_active"] == {"booleanValue": False}
    await client.aclose()
    await client.aclose()
