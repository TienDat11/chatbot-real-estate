"""Regression: JWKS verifier tolerates small clock skew on freshly minted tokens.

Google signs ID tokens on its own clock; a token can arrive with ``iat`` a few
seconds ahead of local time and was rejected as "not yet valid". The verifier
now decodes with a fixed leeway, so slightly-future iat is accepted while a
genuinely premature or expired token still fails.
"""

from __future__ import annotations

import time

import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from api.infrastructure.adapters.firebase_auth_jwks import (
    _TOKEN_CLOCK_LEEWAY_SECONDS,
    FirebaseAuthJwksVerifier,
)
from api.infrastructure.ports.firebase_auth import (
    FirebaseAuthTokenExpired,
    FirebaseAuthTokenInvalid,
)

PROJECT_ID = "sale-chat-bot-11e49"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"
# Skew window under test must stay strictly inside the verifier's leeway bound.
_LEEWAY_SECONDS = _TOKEN_CLOCK_LEEWAY_SECONDS
# Live-E2E regression anchor: the Windows host ran 12.6 s behind Google's
# signer clock, so a freshly minted token carried iat ~13 s in the future and
# 401ed every staff route. Any skew up to the leeway bound must verify.
_OBSERVED_LIVE_SKEW_SECONDS = 15


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
def verifier(local_rsa_jwk: dict) -> FirebaseAuthJwksVerifier:
    """Verifier whose JWKS resolution is replaced by the local key."""

    async def fake_key_for_kid(kid: str):
        if kid != local_rsa_jwk["kid"]:
            return None
        jwk_entry = {k: v for k, v in local_rsa_jwk.items() if not k.startswith("_")}
        return RSAAlgorithm.from_jwk(jwk_entry)

    verifier_instance = FirebaseAuthJwksVerifier(
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
async def test_verifier_accepts_token_issued_a_few_seconds_in_future(
    verifier, local_rsa_jwk
) -> None:
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "uid-skew",
            "user_id": "uid-skew",
            "iat": now + (_LEEWAY_SECONDS - 3),
            "exp": now + 3600,
        },
    )
    user = await verifier.verify_id_token(token)
    assert user.firebase_uid == "uid-skew"


@pytest.mark.asyncio
async def test_verifier_accepts_token_at_observed_live_clock_skew(
    verifier, local_rsa_jwk
) -> None:
    """Live-E2E regression: 12.6 s host drift made fresh iat look future and
    every staff route 401ed; the leeway must absorb this skew."""
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "uid-live-skew",
            "user_id": "uid-live-skew",
            "iat": now + _OBSERVED_LIVE_SKEW_SECONDS,
            "exp": now + 3600,
        },
    )
    user = await verifier.verify_id_token(token)
    assert user.firebase_uid == "uid-live-skew"


@pytest.mark.asyncio
async def test_verifier_still_rejects_token_far_beyond_leeway(verifier, local_rsa_jwk) -> None:
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "uid-premature",
            "iat": now + 600,
            "exp": now + 7200,
        },
    )
    with pytest.raises(FirebaseAuthTokenInvalid):
        await verifier.verify_id_token(token)


@pytest.mark.asyncio
async def test_verifier_still_rejects_expired_token(verifier, local_rsa_jwk) -> None:
    """Leeway shifts expiry by seconds, not minutes — old tokens stay dead."""
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        {"iss": ISSUER, "aud": PROJECT_ID, "sub": "uid-old", "iat": now - 7200, "exp": now - 600},
    )
    with pytest.raises(FirebaseAuthTokenExpired):
        await verifier.verify_id_token(token)
