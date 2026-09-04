"""Durable customer session history (anonymous device-owned transcripts).

The staff CRM lead-conversation view lives ONLY in
``api.interfaces.api.crm_routes`` (GET /api/crm/leads/{lead_id}/conversation)
— its single registration there owns auth, response shape, and meta
sanitization; this router must never shadow it with a second implementation.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.chat_history_service import ChatHistoryService
from api.infrastructure.adapters.postgres_chat_history import repository
from api.interfaces.api.anon_routes import get_anonymous_identity_service
from api.interfaces.api.deps import AuthenticatedPrincipal, optional_sales_or_admin

router = APIRouter(prefix="/api", tags=["sessions"])


class SessionSummaryResponse(BaseModel):
    model_config = {"from_attributes": True}
    session_id: str
    project_key: str
    title: str | None
    message_count: int
    handed_off: bool
    last_active_at: datetime


class MessageResponse(BaseModel):
    model_config = {"from_attributes": True}
    role: str
    content: str
    meta: dict | None
    created_at: datetime


def _service() -> ChatHistoryService:
    return ChatHistoryService(repository)


def _identity_key(anon_token: str | None, identity_service: AnonymousIdentityService) -> str | None:
    if not anon_token:
        return None
    claims = identity_service.verify_token(anon_token)
    return claims.subject if claims else None


def _optional_anon_identity_service() -> AnonymousIdentityService | None:
    """Resolve the anon service defensively: the staff-principal path must not
    500 when ``anon_identity_secret`` is unconfigured, and the anon path turns
    a missing service into its existing 401 answer."""
    try:
        return get_anonymous_identity_service()
    except Exception:  # noqa: BLE001 — unconfigured secret is an auth failure, not a crash
        return None


@router.get("/sessions")
async def list_sessions(
    project_key: str = Query(...),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    x_anon_token: str | None = Header(default=None, alias="X-Anon-Token"),
    identity_service: AnonymousIdentityService | None = Depends(  # noqa: B008
        _optional_anon_identity_service
    ),
    staff_principal: AuthenticatedPrincipal | None = Depends(optional_sales_or_admin),  # noqa: B008
) -> dict:
    if staff_principal is not None:
        # Staff hydrate customer sessions they created via POST /query while
        # authenticated; those rows carry identity_key NULL, so the lookup is
        # device+project scoped (identity_key=None selects the repository's
        # device branch). The device id remains a required capability so a
        # staff principal cannot enumerate arbitrary devices' sessions.
        if not x_device_id:
            raise HTTPException(status_code=401, detail="X-Device-Id is required")
        sessions = await _service().sessions(
            device_id=x_device_id, project_key=project_key, identity_key=None
        )
    else:
        if not x_device_id or not x_anon_token or identity_service is None:
            raise HTTPException(status_code=401, detail="Signed anonymous identity is required")
        identity_key = _identity_key(x_anon_token, identity_service)
        if identity_key is None:
            raise HTTPException(status_code=401, detail="Invalid anonymous identity")
        sessions = await _service().sessions(
            device_id=x_device_id, project_key=project_key, identity_key=identity_key
        )
    return {
        "sessions": [SessionSummaryResponse.model_validate(item).model_dump() for item in sessions]
    }


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    project_key: str = Query(...),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    x_anon_token: str | None = Header(default=None, alias="X-Anon-Token"),
    identity_service: AnonymousIdentityService | None = Depends(  # noqa: B008
        _optional_anon_identity_service
    ),
    staff_principal: AuthenticatedPrincipal | None = Depends(optional_sales_or_admin),  # noqa: B008
) -> dict:
    if staff_principal is not None:
        # See list_sessions: staff-created rows have identity_key NULL, so
        # hydration runs on the repository's device-scoped staff branch.
        if not x_device_id:
            raise HTTPException(status_code=401, detail="X-Device-Id is required")
        # Scope the lookup in SQL so an unknown session never leaks ownership details.
        session = await _service().session(
            session_id=session_id,
            device_id=x_device_id,
            project_key=project_key,
            identity_key=None,
            staff_scoped=True,
        )
    else:
        if not x_device_id or not x_anon_token or identity_service is None:
            raise HTTPException(status_code=401, detail="Signed anonymous identity is required")
        identity_key = _identity_key(x_anon_token, identity_service)
        if identity_key is None:
            raise HTTPException(status_code=401, detail="Invalid anonymous identity")
        # Scope the lookup in SQL so an unknown session never leaks ownership details.
        session = await _service().session(
            session_id=session_id,
            device_id=x_device_id,
            project_key=project_key,
            identity_key=identity_key,
        )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = await _service().messages(session_id=session_id)
    return {
        "session_id": session_id,
        "messages": [MessageResponse.model_validate(item).model_dump() for item in messages],
    }
