"""Shared offline Firebase-auth seams for API tests (underscore: not collected).

Same local-RSA JWKS pattern as tests/test_admin_auth.py: ID tokens are minted
with a locally generated RSA key, the verifier's key resolution is
monkeypatched, and the PG sales mapping is a fake — nothing touches the
network or a real database. Test modules that exercise staff-gated or
training-mode routes import the fixtures/helpers from here so the auth
matrix lives in exactly one place.
"""

from __future__ import annotations

import time

import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from api.infrastructure import dependencies as dependency_injection
from api.infrastructure.adapters import firebase_auth_jwks
from api.infrastructure.config.config import get_settings
from api.interfaces.api import deps as admin_deps

PROJECT_ID = "sale-chat-bot-11e49"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"

# uid -> sales_id rows the fake mapping "returns from PG".
MAPPED_SALES_ID_BY_UID = {"uid-sales-mapped": 777, "uid-sales": 778}


@pytest.fixture(scope="module")
def local_rsa_jwk() -> dict:
    """One module-scoped RSA key pair exported as a JWK entry (kid pinned)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_entry = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_entry["kid"] = "local-test-key"
    jwk_entry["_private_key"] = private_key
    return jwk_entry


@pytest.fixture(scope="module")
def foreign_rsa_private_key():
    """A second key pair: tokens signed by it must fail verification."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def build_offline_verifier(local_rsa_jwk: dict) -> firebase_auth_jwks.FirebaseAuthJwksVerifier:
    """Verifier whose JWKS key resolution is the local key (never HTTP)."""

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


def mint_id_token(
    local_rsa_jwk: dict,
    claims: dict,
    *,
    signing_key=None,
    kid: str | None = None,
) -> str:
    """Sign claims RS256 with the local (or an explicitly foreign) key."""
    return jwt.encode(
        claims,
        key=signing_key or local_rsa_jwk["_private_key"],
        algorithm="RS256",
        headers={"kid": kid or local_rsa_jwk["kid"]},
    )


def base_claims(firebase_uid: str, role: str | None, **overrides) -> dict:
    """Valid Firebase ID-token claims for this project's issuer/audience."""
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": PROJECT_ID,
        "sub": firebase_uid,
        "user_id": firebase_uid,
        "email": f"{firebase_uid}@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
        "firebase": {"sign_in_provider": "password"},
    }
    if role is not None:
        claims["role"] = role
    claims.update(overrides)
    return claims


@pytest.fixture(autouse=False)
def offline_auth_seams(monkeypatch: pytest.MonkeyPatch, local_rsa_jwk: dict) -> None:
    """Point the DI verifier provider and the PG sales mapping at local fakes."""
    monkeypatch.setattr(
        dependency_injection,
        "get_firebase_auth_verifier",
        lambda: build_offline_verifier(local_rsa_jwk),
    )
    monkeypatch.setattr(get_settings(), "sales_legacy_key_auth_enabled", False)

    def fake_fetch_active_sales_id_sync(firebase_uid: str) -> int | None:
        return MAPPED_SALES_ID_BY_UID.get(firebase_uid)

    monkeypatch.setattr(
        admin_deps.admin,
        "_fetch_active_sales_id_sync",
        fake_fetch_active_sales_id_sync,
    )


def sales_bearer_headers(local_rsa_jwk: dict, uid: str = "uid-sales-mapped") -> dict[str, str]:
    """Bearer header for a verified sales principal with an active mapping."""
    token = mint_id_token(local_rsa_jwk, base_claims(uid, "sales"))
    return {"Authorization": f"Bearer {token}"}

def apply_offline_auth_seams(monkeypatch: pytest.MonkeyPatch, local_rsa_jwk: dict) -> None:
    """Non-fixture twin of the offline_auth_seams fixture (direct-callable)."""
    offline_auth_seams.__wrapped__(monkeypatch, local_rsa_jwk)
