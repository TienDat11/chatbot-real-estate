"""Role-gated FastAPI dependencies on verified Firebase ID tokens (story 8.3).

Flow per dependency: Bearer credentials -> JWKS verifier port (any port-level
failure is a 401) -> the signed ``role`` custom claim is checked against the
dependency's allowed role set (403 when missing or outside the set) -> a
best-effort PG sales mapping attaches ``sales_id`` when the verified firebase
uid matches an active sales row (``sales.access_key``). The output is an
immutable :class:`AuthenticatedPrincipal` so route handlers never re-derive
identity or authorization.

Clean-architecture note: this layer only orchestrates the verifier port and
a sync-psycopg2 read seam (same best-effort pattern as media_config); the
token transport itself stays behind the port, so no PyJWT import leaks here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from api.infrastructure.ports.firebase_auth import (
    FirebaseAuthTokenError,
    FirebaseAuthTokenVerifier,
)

logger = logging.getLogger("api.interfaces.api.deps.admin")

# Bearer extraction only. auto_error is off because HTTPBearer's built-in
# failure answer is 403, while a missing/malformed Authorization header must
# surface as 401 — the dependency owns that translation below.
_bearer_credentials_extractor = HTTPBearer(auto_error=False)

# One declared role set per dependency: coarse, single-purpose gates so the
# claim matrix stays explicit (adding a composite rule later means adding a
# new set here, not branching inside the resolver).
ADMIN_ALLOWED_ROLES = frozenset({"admin"})
SALES_ALLOWED_ROLES = frozenset({"sales"})
VIEWER_ALLOWED_ROLES = frozenset({"viewer"})


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Verified caller identity attached to staff-facing requests.

    ``firebase_uid`` is the verified token subject (it doubles as
    ``sales.access_key``); ``role`` is the signed role claim used for gating;
    ``sales_id`` is the PG mapping when an active sales row matches, else None
    (admins and viewers legitimately carry no sales row).
    """

    firebase_uid: str
    email: str | None
    role: str
    sales_id: int | None


def _fetch_active_sales_id_sync(firebase_uid: str) -> int | None:
    """Map a firebase uid to its active PG sales row id; None when absent.

    Best-effort by design (mirrors media_config's legacy sync seam): a short
    connect timeout and a swallow-all return keep a PG blip from failing an
    already-verified request — the role claim gates access, the mapping only
    enriches the principal. Callers must run this off the event loop (see
    :func:`resolve_assigned_sales_id`).
    """
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; principal carries no sales_id")
        return None
    from api.infrastructure.config.config import settings

    try:
        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM sales WHERE access_key = %s AND is_active",
                    (firebase_uid,),
                )
                row = cur.fetchone()
        return int(row[0]) if row is not None else None
    except Exception as exc:  # noqa: BLE001 — mapping is best-effort enrichment
        logger.warning("sales mapping read failed for uid %s: %s", firebase_uid, exc)
        return None


async def resolve_assigned_sales_id(firebase_uid: str) -> int | None:
    """Threadpool wrapper so the sync PG read never blocks the event loop."""
    return await run_in_threadpool(_fetch_active_sales_id_sync, firebase_uid)


async def resolve_authenticated_principal(
    allowed_roles_for_dependency: frozenset[str],
    bearer_credentials: HTTPAuthorizationCredentials | None,
    firebase_token_verifier: FirebaseAuthTokenVerifier,
) -> AuthenticatedPrincipal:
    """Verify the Bearer token, gate the role claim, attach the sales mapping.

    401 for anything failing verification — the port's exception hierarchy
    keeps PyJWT internals out of this layer; 403 for a verified caller whose
    role claim is missing or outside the dependency's allowed set.
    """
    if (
        bearer_credentials is None
        or (bearer_credentials.scheme or "").lower() != "bearer"
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization bearer credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        verified_firebase_user = await firebase_token_verifier.verify_id_token(
            bearer_credentials.credentials
        )
    except FirebaseAuthTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Firebase ID token: {exc.reason}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    token_role_claim = verified_firebase_user.role
    if token_role_claim not in allowed_roles_for_dependency:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated caller lacks the required role",
        )

    assigned_sales_id = await resolve_assigned_sales_id(
        verified_firebase_user.firebase_uid
    )
    return AuthenticatedPrincipal(
        firebase_uid=verified_firebase_user.firebase_uid,
        email=verified_firebase_user.email,
        role=token_role_claim,
        sales_id=assigned_sales_id,
    )


def _verifier_from_dependency_injection() -> FirebaseAuthTokenVerifier:
    """Resolve the shared verifier through the DI provider (lazy import)."""
    from api.infrastructure.dependencies import get_firebase_auth_verifier  # noqa: PLC0415

    return get_firebase_auth_verifier()


async def require_admin(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal:
    """Admin-only routes: verified Firebase token carrying role claim 'admin'."""
    return await resolve_authenticated_principal(
        allowed_roles_for_dependency=ADMIN_ALLOWED_ROLES,
        bearer_credentials=bearer_credentials,
        firebase_token_verifier=_verifier_from_dependency_injection(),
    )


async def require_sales(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal:
    """Sales-only routes: verified Firebase token carrying role claim 'sales'."""
    return await resolve_authenticated_principal(
        allowed_roles_for_dependency=SALES_ALLOWED_ROLES,
        bearer_credentials=bearer_credentials,
        firebase_token_verifier=_verifier_from_dependency_injection(),
    )


async def require_viewer(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal:
    """Viewer-only routes: verified Firebase token carrying role claim 'viewer'."""
    return await resolve_authenticated_principal(
        allowed_roles_for_dependency=VIEWER_ALLOWED_ROLES,
        bearer_credentials=bearer_credentials,
        firebase_token_verifier=_verifier_from_dependency_injection(),
    )
