"""Staff session endpoints on Firebase ID-token auth (story 8.3 / ISSUE-06).

GET /api/admin/session — principal echo behind ``require_admin`` (admin screen
bootstrap) and GET /api/sales/session — principal echo behind ``require_sales``
(broker-board bootstrap). The FE calls these right after sign-in to learn its
effective identity (verified uid, role, and the PG sales mapping) before
rendering role-gated screens; both endpoints are pure reads of the
dependency-resolved principal, no business logic.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.interfaces.api.deps import AuthenticatedPrincipal, require_admin, require_sales


class AuthenticatedSessionResponse(BaseModel):
    """Wire shape of a session bootstrap: the principal fields verbatim."""

    firebase_uid: str
    email: str | None
    role: str
    sales_id: int | None


admin_session_router = APIRouter(prefix="/api/admin", tags=["admin-session"])
sales_session_router = APIRouter(prefix="/api/sales", tags=["sales-session"])


@admin_session_router.get("/session", response_model=AuthenticatedSessionResponse)
async def read_admin_session(
    authenticated_principal: AuthenticatedPrincipal = Depends(require_admin),
) -> AuthenticatedSessionResponse:
    """Echo the admin principal so the FE can bootstrap the admin screen."""
    return AuthenticatedSessionResponse(
        firebase_uid=authenticated_principal.firebase_uid,
        email=authenticated_principal.email,
        role=authenticated_principal.role,
        sales_id=authenticated_principal.sales_id,
    )


@sales_session_router.get("/session", response_model=AuthenticatedSessionResponse)
async def read_sales_session(
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales),
) -> AuthenticatedSessionResponse:
    """Echo the sales principal so the FE can bootstrap the broker board."""
    return AuthenticatedSessionResponse(
        firebase_uid=authenticated_principal.firebase_uid,
        email=authenticated_principal.email,
        role=authenticated_principal.role,
        sales_id=authenticated_principal.sales_id,
    )
