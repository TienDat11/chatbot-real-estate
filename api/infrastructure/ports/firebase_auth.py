"""Firebase ID-token verification port — the BE-side auth boundary.

firebase-admin is banned by the stack lock, so the backend verifies Firebase
ID tokens itself over HTTPS: a JWKS-fetching PyJWT adapter (RS256) behind this
Protocol. Application code depends only on this port; swapping the transport
later means replacing one adapter, never touching callers.

The port maps PyJWT/transport failure classes into a small, transport-neutral
exception hierarchy so callers can translate to 401/403 without importing PyJWT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VerifiedFirebaseUser:
    """Claims extracted from a verified Firebase ID token.

    `role` is deliberately None here: the sales role is resolved later from the
    sales document (PG is the source of truth for roles), never trusted from a
    client-mintable token claim.
    """

    firebase_uid: str
    email: str | None
    email_verified: bool
    role: str | None
    auth_provider: str | None
    token_issued_at: int | None
    token_expires_at: int | None


class FirebaseAuthTokenError(Exception):
    """Base failure for Firebase ID-token verification (port-level, transport-neutral).

    Subclasses carry the exact failure class so callers map to 401 vs 403
    without knowing PyJWT internals.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FirebaseAuthTokenExpired(FirebaseAuthTokenError):
    """Token signature is valid but the token is past its exp claim."""


class FirebaseAuthTokenAudienceMismatch(FirebaseAuthTokenError):
    """Token is not issued for this Firebase project (aud != firebase_project_id)."""


class FirebaseAuthTokenInvalid(FirebaseAuthTokenError):
    """Token failed structural/signature verification or JWKS fetching."""


class FirebaseAuthTokenVerifier(Protocol):
    """Verifies a Firebase ID token and returns the authenticated user claims."""

    async def verify_id_token(self, id_token: str) -> VerifiedFirebaseUser: ...


async def get_firebase_auth_verifier() -> FirebaseAuthTokenVerifier:
    from api.infrastructure.dependencies import get_firebase_auth_verifier as _di_factory

    # The DI factory owns the Settings reads (project id, JWKS URL, issuer,
    # audience); the verifier stays live regardless of the mirror binding
    # because auth and mirroring are separate concerns.
    return _di_factory()
