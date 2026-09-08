"""Role-gated FastAPI dependencies on verified Firebase ID tokens (story 8.3).

Flow per dependency: Bearer credentials -> JWKS verifier port (any port-level
failure is a 401) -> the signed ``role`` custom claim is checked against the
dependency's allowed role set (403 when missing or outside the set) -> a
best-effort PG sales mapping attaches ``sales_id`` when the verified firebase
uid matches an active sales row (``sales.firebase_uid``, falling back to
``sales.access_key`` for pre-provisioning rows). The output is an immutable
:class:`AuthenticatedPrincipal` so route handlers never re-derive identity or
authorization.

Clean-architecture note: this layer only orchestrates the verifier port and
a sync-psycopg2 read seam (same best-effort pattern as media_config); the
token transport itself stays behind the port, so no PyJWT import leaks here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
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
# Composite gate for CRM endpoints (story 9.3): both staff roles may call, the
# route layer then applies per-lead ownership scoping on top of the role claim.
SALES_OR_ADMIN_ALLOWED_ROLES = frozenset({"sales", "admin"})


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Verified caller identity attached to staff-facing requests.

    ``firebase_uid`` is the verified token subject; ``role`` is the signed
    role claim used for gating; ``sales_id`` is the PG mapping when an active
    sales row matches (by ``firebase_uid``, else legacy ``access_key``), else
    None (admins and viewers legitimately carry no sales row).
    """

    firebase_uid: str
    email: str | None
    role: str
    sales_id: int | None


class SalesMappingUnavailable(RuntimeError):
    """Raised when the authoritative sales mapping cannot be read."""


class SalesMappingMissing(LookupError):
    """Raised when a verified sales token has no active sales mapping."""


def _fetch_active_sales_id_sync(firebase_uid: str) -> int | None:
    """Return an active sales mapping, using legacy keys only when enabled."""
    try:
        import psycopg2
    except ImportError as exc:
        raise SalesMappingUnavailable from exc
    from api.infrastructure.config.config import get_settings

    settings = get_settings()
    legacy_mode = bool(settings.sales_legacy_key_auth_enabled) and (
        settings.app_env.strip().lower()
        in {
            "dev",
            "development",
            "test",
        }
    )
    try:
        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                if not firebase_uid:
                    row = None
                else:
                    cur.execute(
                        "SELECT id FROM sales WHERE firebase_uid = %s AND is_active LIMIT 1",
                        (firebase_uid,),
                    )
                    row = cur.fetchone()
                if row is None and legacy_mode:
                    cur.execute(
                        "SELECT id FROM sales WHERE access_key = %s AND is_active",
                        (firebase_uid,),
                    )
                    row = cur.fetchone()
                    logger.info(
                        "legacy sales mapping fallback",
                        extra={
                            "auth_method": "legacy_sales_key",
                            "outcome": "success" if row else "failure",
                        },
                    )
        return int(row[0]) if row is not None else None
    except Exception as exc:  # noqa: BLE001 — caller maps to existing HTTP contract
        logger.warning("sales mapping read failed", exc_info=True)
        raise SalesMappingUnavailable from exc


async def resolve_assigned_sales_id(firebase_uid: str) -> int:
    """Threadpool wrapper so the sync PG read never blocks the event loop."""
    sales_id = await run_in_threadpool(_fetch_active_sales_id_sync, firebase_uid)
    if sales_id is None:
        raise SalesMappingMissing
    return sales_id


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
    if bearer_credentials is None or (bearer_credentials.scheme or "").lower() != "bearer":
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

    # Missing custom claims are ordinary customers. Only signed server claims
    # can elevate a principal to sales/admin; clients cannot self-assign roles.
    token_role_claim = verified_firebase_user.role or "customer"
    if token_role_claim not in allowed_roles_for_dependency:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated caller lacks the required role",
        )

    assigned_sales_id: int | None = None
    if token_role_claim == "sales":
        try:
            assigned_sales_id = await resolve_assigned_sales_id(
                verified_firebase_user.firebase_uid
            )
            if assigned_sales_id is None:
                raise SalesMappingMissing
        except SalesMappingMissing as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated sales caller has no active sales mapping",
            ) from exc
        except SalesMappingUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sales authorization is temporarily unavailable",
            ) from exc
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
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
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
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
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
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal:
    """Viewer-only routes: verified Firebase token carrying role claim 'viewer'."""
    return await resolve_authenticated_principal(
        allowed_roles_for_dependency=VIEWER_ALLOWED_ROLES,
        bearer_credentials=bearer_credentials,
        firebase_token_verifier=_verifier_from_dependency_injection(),
    )


async def require_sales_or_admin(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal:
    """CRM routes (story 9.3): sales or admin may authenticate; per-lead
    ownership scoping (admin unrestricted) is enforced downstream in the
    application service, not by the role claim alone."""
    return await resolve_authenticated_principal(
        allowed_roles_for_dependency=SALES_OR_ADMIN_ALLOWED_ROLES,
        bearer_credentials=bearer_credentials,
        firebase_token_verifier=_verifier_from_dependency_injection(),
    )


async def optional_sales_or_admin(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal | None:
    """Dual-surface routes (customer + staff): returns the sales/admin
    principal when a valid staff bearer is present, else ``None`` so the
    handler can fall back to the anonymous flow.

    Only the no/invalid-credential 401 is swallowed — a verified caller whose
    role claim is wrong (403) must keep failing loudly rather than be
    silently downgraded to the anonymous path.
    """
    if bearer_credentials is None or (bearer_credentials.scheme or "").lower() != "bearer":
        return None
    try:
        return await resolve_authenticated_principal(
            allowed_roles_for_dependency=SALES_OR_ADMIN_ALLOWED_ROLES,
            bearer_credentials=bearer_credentials,
            firebase_token_verifier=_verifier_from_dependency_injection(),
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise


async def require_authenticated(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal:
    """Authenticate any Firebase identity without granting staff privileges."""
    return await resolve_authenticated_principal(
        allowed_roles_for_dependency=frozenset({"customer", "sales", "admin", "viewer"}),
        bearer_credentials=bearer_credentials,
        firebase_token_verifier=_verifier_from_dependency_injection(),
    )


async def require_training_sales(
    request: Request,
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal | None:
    """Server-side authorization gate for training-mode queries (reviewer blocker).

    The FE /train route is client-side role-gated, which is not a security
    boundary — a caller can hit POST /query with ``answer_mode="training"``
    directly. Training is an internal sales-coaching surface (FR-5/FR-25): the
    signed Firebase ``role`` claim must be ``sales`` with an active PG mapping
    (401 missing/invalid token, 403 customer/admin/roleless or unmapped sales),
    mirroring the §7.1 identity chain. Normal-mode queries (``answer_mode``
    absent or ``normal``) pass through untouched, preserving the anonymous and
    authenticated customer flow byte-for-byte.

    The body is read only to decide which gate applies; Starlette caches it so
    the route's own QueryRequest parsing is unaffected. The role decision comes
    exclusively from the verified token — never from a client-supplied field.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Malformed body is the route's own 422 to raise; never leak an auth
        # decision (or a 500) on an unparseable payload.
        return None
    if not isinstance(body, dict) or body.get("answer_mode") != "training":
        return None
    return await resolve_authenticated_principal(
        allowed_roles_for_dependency=SALES_ALLOWED_ROLES,
        bearer_credentials=bearer_credentials,
        firebase_token_verifier=_verifier_from_dependency_injection(),
    )
