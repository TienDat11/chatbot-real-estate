"""Customer authentication-side identity linking endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from api.application.ports.staff_audit import STAFF_AUDIT_ACTION_IDENTITY_LINKED, StaffAuditStore
from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.quota_service import link_identity
from api.application.services.staff_audit_service import record_staff_action
from api.infrastructure.config.config import get_settings
from api.infrastructure.dependencies import get_firebase_auth_verifier, get_staff_audit_store
from api.infrastructure.ports.firebase_auth import FirebaseAuthTokenError
from api.interfaces.api.deps import AuthenticatedPrincipal

router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class LinkAnonymousRequest(BaseModel):
    anon_token: str | None = Field(default=None, max_length=512)
    identity_key: str | None = Field(default=None, max_length=256)


@router.post("/link-anon")
async def link_anonymous_identity(
    payload: LinkAnonymousRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),  # noqa: B008
) -> dict[str, object]:
    """Move an anonymous quota identity to the verified Firebase customer."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer credentials required")
    try:
        user = await get_firebase_auth_verifier().verify_id_token(credentials.credentials)
    except FirebaseAuthTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase ID token") from exc
    if (user.role or "customer") in {"sales", "admin"}:
        raise HTTPException(status_code=403, detail="Staff identities cannot be linked")

    identity_key = None
    if payload.anon_token:
        try:
            identity_service = AnonymousIdentityService(get_settings().anon_identity_secret)
            claims = identity_service.verify_token(payload.anon_token)
        except ValueError:
            claims = None
        if claims is None:
            raise HTTPException(status_code=422, detail="Invalid anonymous identity token")
        identity_key = claims.subject
    if payload.identity_key and not payload.anon_token:
        raise HTTPException(status_code=422, detail="anon_token is required")
    if not identity_key:
        raise HTTPException(status_code=422, detail="anon_token is required")
    try:
        created = await link_identity(identity_key, user.firebase_uid)
    except Exception as exc:
        # Do not expose database details or credentials.
        raise HTTPException(status_code=503, detail="Identity linking unavailable") from exc
    if not created:
        raise HTTPException(status_code=409, detail="Anonymous identity already linked")
    await record_staff_action(
        audit_store,
        principal=AuthenticatedPrincipal(
            firebase_uid=user.firebase_uid,
            email=user.email,
            role=user.role or "customer",
            sales_id=None,
        ),
        action=STAFF_AUDIT_ACTION_IDENTITY_LINKED,
        detail={"identity_key_prefix": identity_key[:8]},
    )
    return {"ok": True, "firebase_uid": user.firebase_uid, "identity_key": identity_key}
