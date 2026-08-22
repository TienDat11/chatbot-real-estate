"""Firebase ID-token verification via PyJWT + Google's public JWKS.

firebase-admin is banned by the stack lock, so the backend verifies Firebase
ID tokens itself: fetch Google's JWKS over HTTPS once, cache keys per kid, and
verify RS256 signatures with PyJWT. A token kid that is not yet cached triggers
a one-shot refresh because Google rotates signing keys periodically. The JWKS
cache lives on the verifier instance (each instance targets one jwks_url /
audience), so a second instance for another project can never read or poison
the first instance's keys.

Every failure path is translated to the port-level exception hierarchy so
application callers never import PyJWT.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from api.infrastructure.ports.firebase_auth import (
    FirebaseAuthTokenAudienceMismatch,
    FirebaseAuthTokenExpired,
    FirebaseAuthTokenInvalid,
    VerifiedFirebaseUser,
)

logger = logging.getLogger("api.adapters.firebase_auth_jwks")

# JWKS cache freshness window; Google rotates keys, so re-fetch at least this often.
_JWKS_CACHE_TTL_SECONDS = 3600

# Shared HTTP client only; the JWKS key cache is per-verifier-instance state.
_jwks_http_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use."""
    global _jwks_http_client
    if _jwks_http_client is None or _jwks_http_client.is_closed:
        _jwks_http_client = httpx.AsyncClient(timeout=10.0)
    return _jwks_http_client


async def close_client() -> None:
    """Close the shared AsyncClient and drop the reference."""
    global _jwks_http_client
    if _jwks_http_client is not None:
        await _jwks_http_client.aclose()
        _jwks_http_client = None


async def _fetch_jwks(jwks_url: str) -> dict[str, Any]:
    """GET the JWKS document; raises on transport/HTTP failure."""
    client = await get_client()
    response = await client.get(jwks_url)
    response.raise_for_status()
    return response.json()


def _rsa_public_key_from_jwk(jwk_entry: dict[str, Any]) -> Any:
    """Build a cryptography RSA public key from a raw JWK entry (n/e base64url)."""
    return RSAAlgorithm.from_jwk(jwk_entry)


class FirebaseAuthJwksVerifier:
    """Verifies Firebase ID tokens with PyJWT against Google's public JWKS."""

    def __init__(self, project_id: str, jwks_url: str, issuer: str, audience: str) -> None:
        self.project_id = project_id
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience
        # Per-instance JWKS cache: instance-scoped so a verifier built for a
        # different project/audience never shares rotation state with this one.
        self._jwks_by_kid: dict[str, Any] = {}
        self._jwks_cached_at: float = 0.0

    def _jwks_cache_is_fresh(self) -> bool:
        return time.monotonic() - self._jwks_cached_at < _JWKS_CACHE_TTL_SECONDS

    async def _key_for_kid(self, kid: str) -> Any:
        """Return the cached RSA key for a kid, refreshing the JWKS once if unknown/stale."""
        cached_key = self._jwks_by_kid.get(kid)
        if cached_key is not None and self._jwks_cache_is_fresh():
            return cached_key
        # Unknown kid or stale cache — Google may have rotated keys, so refresh once.
        jwks_document = await _fetch_jwks(self.jwks_url)
        self._jwks_by_kid = {
            entry["kid"]: _rsa_public_key_from_jwk(entry)
            for entry in jwks_document.get("keys", [])
            if "kid" in entry
        }
        self._jwks_cached_at = time.monotonic()
        return self._jwks_by_kid.get(kid)

    async def verify_id_token(self, id_token: str) -> VerifiedFirebaseUser:
        try:
            unverified_header = jwt.get_unverified_header(id_token)
            kid = unverified_header.get("kid")
            if not kid:
                raise FirebaseAuthTokenInvalid("token header has no kid")
        except jwt.PyJWTError as exc:
            raise FirebaseAuthTokenInvalid(f"malformed token header: {exc}") from exc

        try:
            rsa_public_key = await self._key_for_kid(kid)
            if rsa_public_key is None:
                raise FirebaseAuthTokenInvalid(f"no JWKS key found for kid {kid}")
            payload = jwt.decode(
                id_token,
                rsa_public_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
            )
        except FirebaseAuthTokenInvalid:
            raise
        except jwt.ExpiredSignatureError as exc:
            raise FirebaseAuthTokenExpired(str(exc)) from exc
        except jwt.InvalidAudienceError as exc:
            raise FirebaseAuthTokenAudienceMismatch(str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise FirebaseAuthTokenInvalid(str(exc)) from exc

        firebase_claims = payload.get("firebase") or {}
        return VerifiedFirebaseUser(
            firebase_uid=str(payload.get("user_id") or payload.get("sub") or ""),
            email=payload.get("email"),
            email_verified=bool(payload.get("email_verified", False)),
            role=None,  # resolved later from the sales document, never from the token
            auth_provider=firebase_claims.get("sign_in_provider"),
            token_issued_at=payload.get("iat"),
            token_expires_at=payload.get("exp"),
        )
