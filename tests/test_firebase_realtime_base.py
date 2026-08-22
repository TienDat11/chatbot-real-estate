"""Firebase realtime base — JWKS verifier, REST mirror, DI dispatch (no live network).

Covers the four seams of the BE realtime layer:
1. FirebaseAuthJwksVerifier against a locally-generated RSA key pair (valid /
   expired / audience-mismatch / garbage token) with the JWKS fetch monkeypatched.
2. FirestoreRestLeadMirror REST Value mapping, document URL, delete path, and
   health_check=False on token failure — httpx monkeypatched, never real calls.
3. Port purity: importing the ports module alone must not pull the adapters in.
4. DI dispatch: off -> NoopRealtimeLeadMirror; firestore without service-account
   config -> RealtimeMirrorNotConfiguredError at wiring time.
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from api.infrastructure import dependencies as dependency_injection
from api.infrastructure.adapters import firebase_auth_jwks, firestore_rest_mirror
from api.infrastructure.adapters.firestore_rest_mirror import FirestoreRestLeadMirror
from api.infrastructure.adapters.noop_realtime_mirror import NoopRealtimeLeadMirror
from api.infrastructure.ports.firebase_auth import (
    FirebaseAuthTokenAudienceMismatch,
    FirebaseAuthTokenExpired,
    FirebaseAuthTokenInvalid,
)
from api.infrastructure.ports.realtime_mirror import (
    LeadMirrorDocument,
    RealtimeMirrorNotConfiguredError,
)

PROJECT_ID = "sale-chat-bot-11e49"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"


@pytest.fixture(scope="module")
def local_rsa_jwk() -> dict:
    """One module-scoped RSA key pair exported as a JWK entry (kid pinned)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_entry = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_entry["kid"] = "local-test-key"
    jwk_entry["_private_key"] = private_key
    return jwk_entry


@pytest.fixture
def verifier(local_rsa_jwk: dict) -> firebase_auth_jwks.FirebaseAuthJwksVerifier:
    """Verifier with the JWKS key resolution replaced by the local key (module cache reset)."""
    firebase_auth_jwks._jwks_by_kid = {}
    firebase_auth_jwks._jwks_cached_at = 0.0

    async def fake_key_for_kid(kid: str):
        if kid != local_rsa_jwk["kid"]:
            return None
        jwk_entry = {k: v for k, v in local_rsa_jwk.items() if not k.startswith("_")}
        return RSAAlgorithm.from_jwk(jwk_entry)

    verifier_instance = firebase_auth_jwks.FirebaseAuthJwksVerifier(
        project_id=PROJECT_ID,
        jwks_url="https://example.invalid/jwks",
        issuer=ISSUER,
        audience=PROJECT_ID,
    )
    verifier_instance._key_for_kid = fake_key_for_kid  # type: ignore[method-assign]
    return verifier_instance


def _mint_id_token(local_rsa_jwk: dict, claims: dict) -> str:
    return jwt.encode(
        claims,
        key=local_rsa_jwk["_private_key"],
        algorithm="RS256",
        headers={"kid": local_rsa_jwk["kid"]},
    )


@pytest.mark.asyncio
async def test_verifier_accepts_valid_id_token(verifier, local_rsa_jwk) -> None:
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "uid-123",
            "user_id": "uid-123",
            "email": "sales@example.com",
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
            "firebase": {"sign_in_provider": "password"},
        },
    )
    user = await verifier.verify_id_token(token)
    assert user.firebase_uid == "uid-123"
    assert user.email == "sales@example.com"
    assert user.email_verified is True
    assert user.role is None
    assert user.auth_provider == "password"


@pytest.mark.asyncio
async def test_verifier_rejects_expired_token(verifier, local_rsa_jwk) -> None:
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        {"iss": ISSUER, "aud": PROJECT_ID, "sub": "uid-123", "iat": now - 7200, "exp": now - 3600},
    )
    with pytest.raises(FirebaseAuthTokenExpired):
        await verifier.verify_id_token(token)


@pytest.mark.asyncio
async def test_verifier_rejects_audience_mismatch(verifier, local_rsa_jwk) -> None:
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        {"iss": ISSUER, "aud": "other-project", "sub": "uid-123", "iat": now, "exp": now + 3600},
    )
    with pytest.raises(FirebaseAuthTokenAudienceMismatch):
        await verifier.verify_id_token(token)


@pytest.mark.asyncio
async def test_verifier_rejects_garbage_token(verifier) -> None:
    with pytest.raises(FirebaseAuthTokenInvalid):
        await verifier.verify_id_token("not-a-jwt")


@pytest.mark.asyncio
async def test_verifier_rejects_unknown_kid(verifier, local_rsa_jwk) -> None:
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": PROJECT_ID, "sub": "uid-123", "iat": now, "exp": now + 3600},
        local_rsa_jwk["_private_key"],
        algorithm="RS256",
        headers={"kid": "rotated-away-key"},
    )
    with pytest.raises(FirebaseAuthTokenInvalid):
        await verifier.verify_id_token(token)


# ---------------------------------------------------------------------------
# FirestoreRestLeadMirror — REST Value mapping + URL shapes, httpx monkeypatched
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return {"access_token": "fake-access-token", "expires_in": 3600}


class _FakeAsyncClient:
    """Records requests without touching the network."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.token_response = _FakeResponse()
        self.document_response = _FakeResponse()

    async def post(self, url: str, data: dict | None = None, **kwargs) -> _FakeResponse:
        self.requests.append(("POST", url, data))
        return self.token_response

    async def patch(self, url: str, headers: dict | None = None, json: dict | None = None, **kwargs) -> _FakeResponse:
        self.requests.append(("PATCH", url, json))
        return self.document_response

    async def delete(self, url: str, headers: dict | None = None, **kwargs) -> _FakeResponse:
        self.requests.append(("DELETE", url, None))
        return self.document_response


@pytest.fixture
def mirror_with_fake_client(monkeypatch: pytest.MonkeyPatch) -> tuple[FirestoreRestLeadMirror, _FakeAsyncClient]:
    mirror = FirestoreRestLeadMirror(
        project_id=PROJECT_ID,
        service_account_client_email="mirror@developer.gserviceaccount.com",
        service_account_private_key="-----BEGIN PRIVATE KEY-----\\nfake\\n-----END PRIVATE KEY-----",
        rest_base_url="https://firestore.googleapis.com/v1",
    )
    fake_client = _FakeAsyncClient()

    async def fake_get_client() -> _FakeAsyncClient:
        return fake_client

    monkeypatch.setattr(firestore_rest_mirror, "get_client", fake_get_client)
    # Bypass RS256 signing of the fake PEM: hand the adapter a cached token.
    monkeypatch.setattr(
        firestore_rest_mirror,
        "_cached_access_token",
        "pre-cached-token",
    )
    monkeypatch.setattr(
        firestore_rest_mirror,
        "_cached_access_token_expires_at",
        time.monotonic() + 3600.0,
    )
    return mirror, fake_client


@pytest.mark.asyncio
async def test_mirror_upsert_maps_fields_and_stamps_updated_at(mirror_with_fake_client) -> None:
    mirror, fake_client = mirror_with_fake_client
    document = LeadMirrorDocument(
        customer_id="hmac-digest-1",
        project_key="camellia",
        lead_status="new",
        display_name="Nguyen Van A",
        masked_phone="090****789",
        assigned_sales_firebase_uid="sales-uid-1",
        consent_service=True,
        consent_marketing=False,
        consent_recorded_at="2026-08-22T10:00:00+00:00",
        last_customer_message_at=None,
        updated_at="caller-timestamp",
    )
    await mirror.upsert_lead_mirror(customer_id="hmac-digest-1", document=document)

    method, url, body = fake_client.requests[-1]
    assert method == "PATCH"
    assert url == (
        f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
        "/databases/(default)/documents/leads/hmac-digest-1"
    )
    fields = body["fields"]
    assert fields["customer_id"] == {"stringValue": "hmac-digest-1"}
    assert fields["consent_service"] == {"booleanValue": True}
    assert fields["consent_marketing"] == {"booleanValue": False}
    # None must be omitted, not written as a null value.
    assert "last_customer_message_at" not in fields
    # The adapter stamps updated_at itself, ignoring the caller's clock.
    assert fields["updated_at"]["timestampValue"] != "caller-timestamp"


@pytest.mark.asyncio
async def test_mirror_remove_uses_delete(mirror_with_fake_client) -> None:
    mirror, fake_client = mirror_with_fake_client
    await mirror.remove_lead_mirror("hmac-digest-1")
    method, url, _ = fake_client.requests[-1]
    assert method == "DELETE"
    assert url.endswith("/documents/leads/hmac-digest-1")


@pytest.mark.asyncio
async def test_mirror_health_check_false_on_token_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = FirestoreRestLeadMirror(
        project_id=PROJECT_ID,
        service_account_client_email="mirror@developer.gserviceaccount.com",
        service_account_private_key="-----BEGIN PRIVATE KEY-----\\nfake\\n-----END PRIVATE KEY-----",
        rest_base_url="https://firestore.googleapis.com/v1",
    )

    async def failing_token() -> str:
        raise RuntimeError("token endpoint down")

    monkeypatch.setattr(mirror, "_access_token", failing_token)
    assert await mirror.health_check() is False


def test_mirror_constructor_requires_service_account_config() -> None:
    with pytest.raises(RealtimeMirrorNotConfiguredError):
        FirestoreRestLeadMirror(
            project_id=PROJECT_ID,
            service_account_client_email="",
            service_account_private_key="",
            rest_base_url="https://firestore.googleapis.com/v1",
        )


# ---------------------------------------------------------------------------
# Port purity + DI dispatch
# ---------------------------------------------------------------------------


def test_ports_import_without_adapters() -> None:
    """No port file may import an adapter at module level (lazy factory imports are fine).

    Checked via AST on top-level statements: package __init__ chains legitimately
    load adapters elsewhere, so sys.modules in a subprocess would over-assert.
    """
    ports_directory = Path(__file__).resolve().parents[1] / "api" / "infrastructure" / "ports"
    port_files = sorted(ports_directory.glob("*.py"))
    assert port_files, "ports directory unexpectedly empty"
    for port_file in port_files:
        module_tree = ast.parse(port_file.read_text(encoding="utf-8"))
        for node in module_tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "api.infrastructure.adapters"
            ):
                pytest.fail(f"{port_file.name} imports {node.module} at module level")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("api.infrastructure.adapters"):
                        pytest.fail(f"{port_file.name} imports {alias.name} at module level")


@pytest.mark.asyncio
async def test_dependency_dispatch_off_yields_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependency_injection, "_realtime_lead_mirror", None)

    class _OffSettings:
        firebase_binding = "off"
        firebase_project_id = PROJECT_ID

    monkeypatch.setattr(dependency_injection, "get_settings", lambda: _OffSettings())
    resolved = await dependency_injection.get_realtime_lead_mirror()
    assert isinstance(resolved, NoopRealtimeLeadMirror)


@pytest.mark.asyncio
async def test_dependency_dispatch_firestore_without_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependency_injection, "_realtime_lead_mirror", None)

    class _FirestoreSettings:
        firebase_binding = "firestore"
        firebase_project_id = PROJECT_ID
        firebase_service_account_client_email = ""
        firebase_service_account_private_key = ""
        firebase_firestore_rest_base_url = "https://firestore.googleapis.com/v1"

    monkeypatch.setattr(dependency_injection, "get_settings", lambda: _FirestoreSettings())
    with pytest.raises(RealtimeMirrorNotConfiguredError):
        await dependency_injection.get_realtime_lead_mirror()
    # Leave the singleton slot empty for other tests.
    dependency_injection._realtime_lead_mirror = None


def test_dependency_verifier_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependency_injection, "_firebase_auth_verifier", None)

    class _VerifierSettings:
        firebase_project_id = PROJECT_ID
        firebase_jwks_url = "https://example.invalid/jwks"
        firebase_auth_issuer = ISSUER

    monkeypatch.setattr(dependency_injection, "get_settings", lambda: _VerifierSettings())
    verifier_instance = dependency_injection.get_firebase_auth_verifier()
    assert isinstance(verifier_instance, firebase_auth_jwks.FirebaseAuthJwksVerifier)
    assert verifier_instance.project_id == PROJECT_ID
    assert verifier_instance.audience == PROJECT_ID
    dependency_injection._firebase_auth_verifier = None


def test_noop_mirror_is_transport_neutral() -> None:
    """The Noop must satisfy the port contract: writes swallow, health stays True."""
    noop = NoopRealtimeLeadMirror()
    document = LeadMirrorDocument(
        customer_id="cid",
        project_key="camellia",
        lead_status="new",
        display_name=None,
        masked_phone=None,
        assigned_sales_firebase_uid=None,
        consent_service=False,
        consent_marketing=False,
        consent_recorded_at=None,
        last_customer_message_at=None,
        updated_at="2026-08-22T00:00:00+00:00",
    )
    assert asyncio.run(noop.upsert_lead_mirror(customer_id="cid", document=document)) is None
    assert asyncio.run(noop.remove_lead_mirror("cid")) is None
    assert asyncio.run(noop.health_check()) is True
