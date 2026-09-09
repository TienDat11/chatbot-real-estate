"""Signed anonymous identity (secure wave Issue 1A): token crypto + mint route.

Secrets are runtime-generated in fixtures only — no credential literals,
matching spec §6 ("NO literals in source/tests").
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.application.services import anon_identity
from api.application.services.anon_identity import (
    ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_SECONDS,
    CLOCK_SKEW_TOLERANCE_SECONDS,
    AnonymousIdentityClaims,
    AnonymousIdentityService,
    InMemoryRateLimitStore,
    mint_anonymous_identity_token,
    set_rate_limit_port,
    verify_anonymous_identity_token,
)
from api.infrastructure.config.config import Settings
from api.interfaces.api import anon_routes


@pytest.fixture
def signing_secret() -> str:
    return secrets.token_urlsafe(48)


def encode_base64url_segment(raw: bytes) -> str:
    # Mirrors the production encoder so hand-built claim sets stay compatible.
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_signed_token_with_claims(signing_secret_value: str, claims: dict[str, object]) -> str:
    """Craft a correctly-signed token carrying arbitrary claims (attack tests)."""
    payload_bytes = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature_bytes = hmac.new(
        signing_secret_value.encode("utf-8"), payload_bytes, hashlib.sha256
    ).digest()
    return f"{encode_base64url_segment(payload_bytes)}.{encode_base64url_segment(signature_bytes)}"


# --- Token crypto -----------------------------------------------------------


def test_mint_verify_roundtrip_preserves_subject(signing_secret: str) -> None:
    token = mint_anonymous_identity_token(signing_secret)
    claims = verify_anonymous_identity_token(token, signing_secret)
    assert isinstance(claims, AnonymousIdentityClaims)
    # Roundtrip subject must be the minted opaque uuid4, PII-free by design.
    assert uuid.UUID(claims.subject).version == 4
    assert claims.token_version == 1


def test_each_mint_yields_a_distinct_identity(signing_secret: str) -> None:
    first = verify_anonymous_identity_token(
        mint_anonymous_identity_token(signing_secret), signing_secret
    )
    second = verify_anonymous_identity_token(
        mint_anonymous_identity_token(signing_secret), signing_secret
    )
    assert first is not None and second is not None
    assert first.subject != second.subject


def test_tampered_payload_is_rejected(signing_secret: str) -> None:
    token = mint_anonymous_identity_token(signing_secret)
    payload_segment, signature_segment = token.split(".")
    flipped_payload_segment = (
        "A" + payload_segment[1:] if payload_segment[0] != "A" else "B" + payload_segment[1:]
    )
    tampered_token = f"{flipped_payload_segment}.{signature_segment}"
    assert verify_anonymous_identity_token(tampered_token, signing_secret) is None


def test_tampered_signature_is_rejected(signing_secret: str) -> None:
    token = mint_anonymous_identity_token(signing_secret)
    payload_segment, signature_segment = token.split(".")
    flipped_signature_segment = (
        "A" + signature_segment[1:] if signature_segment[0] != "A" else "B" + signature_segment[1:]
    )
    tampered_token = f"{payload_segment}.{flipped_signature_segment}"
    assert verify_anonymous_identity_token(tampered_token, signing_secret) is None


def test_verification_fails_under_a_different_secret(signing_secret: str) -> None:
    token = mint_anonymous_identity_token(signing_secret)
    other_secret = secrets.token_urlsafe(48)
    assert other_secret != signing_secret
    assert verify_anonymous_identity_token(token, other_secret) is None


def test_token_expires_after_the_freshness_window(signing_secret: str) -> None:
    issued_at_epoch_seconds = 1_700_000_000
    token = mint_anonymous_identity_token(signing_secret, now_epoch_seconds=issued_at_epoch_seconds)
    boundary_now = issued_at_epoch_seconds + ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_SECONDS
    assert (
        verify_anonymous_identity_token(token, signing_secret, now_epoch_seconds=boundary_now - 1)
        is not None
    )
    # Exactly-at-window stays acceptable; one second past it must be rejected.
    assert (
        verify_anonymous_identity_token(token, signing_secret, now_epoch_seconds=boundary_now)
        is not None
    )
    assert (
        verify_anonymous_identity_token(token, signing_secret, now_epoch_seconds=boundary_now + 1)
        is None
    )


def test_token_issued_in_the_far_future_is_rejected(signing_secret: str) -> None:
    issued_at_epoch_seconds = 1_700_000_000
    token = mint_anonymous_identity_token(signing_secret, now_epoch_seconds=issued_at_epoch_seconds)
    beyond_skew_now = issued_at_epoch_seconds - CLOCK_SKEW_TOLERANCE_SECONDS - 1
    assert (
        verify_anonymous_identity_token(token, signing_secret, now_epoch_seconds=beyond_skew_now)
        is None
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "not-a-uuid", "iat": 1_700_000_000, "ver": 1},
        {"sub": str(uuid.uuid1()), "iat": 1_700_000_000, "ver": 1},
        {"sub": str(uuid.uuid4()), "iat": 1_700_000_000, "ver": 2},
        {"sub": str(uuid.uuid4()), "iat": "1700000000", "ver": 1},
        {"sub": str(uuid.uuid4()), "ver": 1},
        {"iat": 1_700_000_000, "ver": 1},
    ],
)
def test_structurally_invalid_but_correctly_signed_claims_are_rejected(
    signing_secret: str, claims: dict[str, object]
) -> None:
    token = build_signed_token_with_claims(signing_secret, claims)
    assert verify_anonymous_identity_token(token, signing_secret) is None


@pytest.mark.parametrize(
    "malformed_token",
    ["", "no-separator-here", "one.two.three", "@@@@.@@@@", "aGVsbG8.aGVsbG8"],
)
def test_malformed_tokens_are_rejected_without_raising(
    signing_secret: str, malformed_token: str
) -> None:
    assert verify_anonymous_identity_token(malformed_token, signing_secret) is None


def test_service_refuses_to_construct_with_an_empty_secret() -> None:
    with pytest.raises(ValueError):
        AnonymousIdentityService("")


def test_service_roundtrips_through_instance_methods(signing_secret: str) -> None:
    service = AnonymousIdentityService(signing_secret)
    assert service.verify_token(service.mint_token()) is not None
    assert service.verify_token("forged.value") is None


# --- Configuration fail-fast ------------------------------------------------


def _generated_production_env_overrides() -> dict[str, str]:
    return {
        "postgres_password": secrets.token_urlsafe(24),
        "llm_api_key": secrets.token_urlsafe(24),
        # Independent runtime-only material lets each test reach the
        # ANON_IDENTITY_SECRET validator without embedding a usable secret.
        "lead_mirror_hmac_secret": secrets.token_urlsafe(48),
        # Production now requires an explicit non-wildcard CORS allowlist.
        "cors_origins": ["https://app.example.com"],
    }


def test_production_settings_fail_fast_without_anon_identity_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANON_IDENTITY_SECRET", raising=False)
    with pytest.raises(ValidationError, match="ANON_IDENTITY_SECRET"):
        Settings(app_env="production", _env_file=None, **_generated_production_env_overrides())


def test_production_settings_accept_generated_anon_identity_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_settings = Settings(
        app_env="production",
        anon_identity_secret=secrets.token_urlsafe(48),
        _env_file=None,
        **_generated_production_env_overrides(),
    )
    assert production_settings.anon_identity_secret != ""
    assert production_settings.anonymous_bonus_turns_after_lead == 5
    assert production_settings.ip_rate_limit_mint_max_requests > 0


def test_default_settings_expose_ip_limit_thresholds() -> None:
    settings = Settings(_env_file=None)
    assert settings.ip_rate_limit_window_seconds == 3600
    assert settings.ip_rate_limit_query_max_requests > settings.ip_rate_limit_mint_max_requests
    assert settings.ip_rate_limit_lead_max_requests > 0


@pytest.mark.parametrize("placeholder_secret", ["__GENERATE_ME__", "short", "secret", "password"])
def test_production_settings_fail_fast_on_placeholder_anon_secret(
    monkeypatch: pytest.MonkeyPatch, placeholder_secret: str
) -> None:
    """B2: prod refuses the __GENERATE_ME__ placeholder, guessable values, and
    anything under the 32-char floor; non-prod keeps its ephemeral fallback."""
    monkeypatch.delenv("ANON_IDENTITY_SECRET", raising=False)
    with pytest.raises(ValidationError, match="ANON_IDENTITY_SECRET"):
        Settings(
            app_env="production",
            anon_identity_secret=placeholder_secret,
            _env_file=None,
            **_generated_production_env_overrides(),
        )


def test_production_settings_rejects_short_but_random_anon_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A random-looking secret below the 32-char floor is still refused."""
    monkeypatch.delenv("ANON_IDENTITY_SECRET", raising=False)
    with pytest.raises(ValidationError, match="ANON_IDENTITY_SECRET"):
        Settings(
            app_env="production",
            anon_identity_secret="abc123def456",
            _env_file=None,
            **_generated_production_env_overrides(),
        )


def test_non_production_env_keeps_ephemeral_anon_secret_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: dev/test with no secret still boots with a process-local secret."""
    monkeypatch.delenv("ANON_IDENTITY_SECRET", raising=False)
    settings = Settings(app_env="dev", _env_file=None)
    assert settings.anon_identity_secret != ""
    assert len(settings.anon_identity_secret) >= 32


# --- Mint endpoint (standalone router; main.py registration is Issue 2) -----


class FixedEpochRateLimitStore(InMemoryRateLimitStore):
    """Rate-limit store pinned to one window start so counts never roll over mid-test."""

    def __init__(self, fixed_window_start_epoch: int = 1_700_000_000) -> None:
        super().__init__()
        self._fixed_window_start_epoch = fixed_window_start_epoch

    async def check_rate_limit_allowed(
        self, ip_address: str, kind: str, limit: int, window_seconds: int
    ) -> bool:
        counter_key = (ip_address, kind, self._fixed_window_start_epoch)
        current_count = self._counts_by_ip_kind_window.get(counter_key, 0)
        if current_count >= max(limit, 1):
            return False
        self._counts_by_ip_kind_window[counter_key] = current_count + 1
        return True


def make_mint_client(
    signing_secret_value: str,
    *,
    mint_request_limit: int = 1000,
    trusted_proxy_ips: set[str] | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(anon_routes.router)

    def provide_test_identity_service() -> AnonymousIdentityService:
        return AnonymousIdentityService(signing_secret_value)

    isolated_rate_limit_store = FixedEpochRateLimitStore()
    app.dependency_overrides[anon_routes.get_anonymous_identity_service] = (
        provide_test_identity_service
    )
    app.dependency_overrides[anon_routes.get_mint_request_limit] = lambda: mint_request_limit
    app.dependency_overrides[anon_routes.get_rate_limit_port] = lambda: isolated_rate_limit_store
    # Starlette TestClient presents a "testclient" peer; trust it so the tests'
    # X-Forwarded-For headers are honored the way a deployed app trusts its
    # configured reverse-proxy peer.
    app.dependency_overrides[anon_routes.get_trusted_proxy_ips] = lambda: (
        trusted_proxy_ips if trusted_proxy_ips is not None else {"testclient"}
    )
    return TestClient(app)


def test_get_anon_token_returns_verifiable_signed_token(signing_secret: str) -> None:
    client = make_mint_client(signing_secret)
    response = client.get("/api/anon/token", headers={"X-Forwarded-For": "198.51.100.10"})
    assert response.status_code == 200
    body = response.json()
    assert body["expires_hint_hours"] == ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_SECONDS // 3600
    verified_claims = verify_anonymous_identity_token(body["anon_token"], signing_secret)
    assert verified_claims is not None
    assert uuid.UUID(verified_claims.subject).version == 4


def test_mint_endpoint_trips_per_ip_limit_then_allows_other_addresses(
    signing_secret: str,
) -> None:
    client = make_mint_client(signing_secret, mint_request_limit=2)
    first_response = client.get("/api/anon/token", headers={"X-Forwarded-For": "198.51.100.20"})
    second_response = client.get("/api/anon/token", headers={"X-Forwarded-For": "198.51.100.20"})
    third_response = client.get("/api/anon/token", headers={"X-Forwarded-For": "198.51.100.20"})
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert third_response.status_code == 429
    # A different address carries its own allowance (per-IP keying).
    other_address_response = client.get(
        "/api/anon/token", headers={"X-Forwarded-For": "198.51.100.21"}
    )
    assert other_address_response.status_code == 200


def test_in_memory_store_recovers_in_the_next_window(signing_secret: str) -> None:
    clock = [10.0]
    store = InMemoryRateLimitStore(now_provider=lambda: clock[0])

    async def consume_until_blocked() -> bool:
        for _ in range(3):
            allowed = await store.check_rate_limit_allowed("203.0.113.9", "mint", 2, 10)
            if not allowed:
                return False
        return await store.check_rate_limit_allowed("203.0.113.9", "mint", 2, 10)

    assert asyncio.run(consume_until_blocked()) is False

    # Advance the injected clock explicitly; no wall-clock sleep or boundary race.
    clock[0] = 20.0
    assert asyncio.run(store.check_rate_limit_allowed("203.0.113.9", "mint", 2, 10)) is True

    # A rollback cannot move the key into an older bucket and reset the brake.
    clock[0] = 10.0
    assert asyncio.run(store.check_rate_limit_allowed("203.0.113.9", "mint", 2, 10)) is True
    assert asyncio.run(store.check_rate_limit_allowed("203.0.113.9", "mint", 2, 10)) is False


def test_module_level_port_seam_accepts_a_replacement() -> None:
    replacement_store = InMemoryRateLimitStore()
    original_port = anon_identity.get_rate_limit_port()
    try:
        set_rate_limit_port(replacement_store)
        assert anon_identity.get_rate_limit_port() is replacement_store
    finally:
        set_rate_limit_port(original_port)


# --- Requirement 3: X-Forwarded-For only trusted from a configured proxy peer ---


def _make_http_request(peer_host: str | None, headers: dict[str, str]):
    from starlette.requests import Request

    scope: dict = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in headers.items()
        ],
        "client": (peer_host, 12345) if peer_host else None,
        "server": ("test", 80),
    }
    return Request(scope)


def test_xff_ignored_when_peer_is_not_a_trusted_proxy() -> None:
    """A direct client's forged X-Forwarded-For must never win the keying."""
    request = _make_http_request(
        peer_host="203.0.113.5", headers={"X-Forwarded-For": "198.51.100.7"}
    )
    canonical = anon_routes.get_client_ip_address(request, trusted_proxy_ips=set())
    assert canonical == "203.0.113.5"


def test_xff_honored_only_for_trusted_proxy_peer() -> None:
    """From a configured proxy peer, XFF is walked right-to-left and stops at
    the first hop that is NOT a trusted proxy."""
    request = _make_http_request(
        peer_host="127.0.0.1",
        headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.9"},
    )
    canonical = anon_routes.get_client_ip_address(request, trusted_proxy_ips={"127.0.0.1"})
    # Only 127.0.0.1 is trusted, so 10.0.0.9 (the nearest hop to the peer) is
    # the first non-trusted hop from the right — the original client claim
    # (198.51.100.7) cannot be verified through an untrusted hop.
    assert canonical == "10.0.0.9"


def test_xff_chain_stops_at_first_untrusted_hop_from_the_right() -> None:
    """A chain of trusted proxies is walked from the right; the first hop we
    cannot vouch for is the canonical client."""
    request = _make_http_request(
        peer_host="10.0.0.1",
        headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.9, 10.0.0.1"},
    )
    canonical = anon_routes.get_client_ip_address(
        request, trusted_proxy_ips={"10.0.0.1", "10.0.0.9"}
    )
    assert canonical == "198.51.100.7"


def test_xff_right_to_left_keeps_single_hop_behavior() -> None:
    """A single XFF hop from a trusted peer is still the client."""
    request = _make_http_request(
        peer_host="127.0.0.1",
        headers={"X-Forwarded-For": "198.51.100.7"},
    )
    canonical = anon_routes.get_client_ip_address(request, trusted_proxy_ips={"127.0.0.1"})
    assert canonical == "198.51.100.7"


def test_client_ip_falls_back_to_peer_when_no_xff() -> None:
    request = _make_http_request(peer_host="203.0.113.9", headers={})
    assert (
        anon_routes.get_client_ip_address(request, trusted_proxy_ips={"127.0.0.1"}) == "203.0.113.9"
    )


def test_spoofed_xff_cannot_claim_another_ips_bucket() -> None:
    """Route-level: with no trusted proxy, all clients share the peer bucket,
    so a forged XFF header cannot mint from a fresh per-IP allowance."""
    client = make_mint_client(
        secrets.token_urlsafe(48), mint_request_limit=1, trusted_proxy_ips=set()
    )
    first = client.get("/api/anon/token", headers={"X-Forwarded-For": "198.51.100.10"})
    assert first.status_code == 200
    # Same "testclient" peer, different forged XFF -> still the same bucket, now spent.
    spoofed = client.get("/api/anon/token", headers={"X-Forwarded-For": "198.51.100.11"})
    assert spoofed.status_code == 429
