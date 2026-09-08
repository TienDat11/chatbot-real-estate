"""FCM device registration (staff + verified anonymous customers) and calls.

Customers of this product are anonymous chat users without Firebase Auth, so
device registration accepts the SAME two transports the lead flow already
trusts: a Firebase bearer token (staff) or the signed anon token minted by
/api/anon/token (header X-Anon-Token or body field anon_token). Recipient
resolution for call-started is server-side from the lead row — callers can
never name the recipient.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from api.application.ports.fcm_notifications import (
    IDENTITY_TYPE_ANON_CUSTOMER,
    IDENTITY_TYPE_FIREBASE,
)
from api.application.ports.staff_audit import STAFF_AUDIT_ACTION_CALL_STARTED
from api.application.services.anon_identity import get_rate_limit_port
from api.application.services.call_started_notification import dispatch_call_started_once
from api.application.services.fcm_notification_service import FcmNotificationService
from api.application.services.lead_mirror_service import compute_customer_id
from api.application.services.staff_audit_service import record_staff_action
from api.infrastructure.config.config import get_settings
from api.infrastructure.dependencies import (
    get_fcm_notification_service,
    get_fcm_tokens,
    get_staff_audit_store,
)
from api.infrastructure.ports.leads import LeadRepository, get_lead_repository
from api.interfaces.api.anon_routes import (
    get_anonymous_identity_service,
    get_client_ip_address,
)
from api.interfaces.api.deps import (
    AuthenticatedPrincipal,
    require_authenticated,
    require_sales_or_admin,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

logger = logging.getLogger("api.notifications")

# Rate-limit vocabulary for this surface; the shared RateLimitPort keeps the
# same fixed-window semantics as the anon mint/lead brakes.
RATE_LIMIT_KIND_FCM_REGISTER = "fcm-register"
FCM_REGISTER_RATE_LIMIT_DEFAULT = 30


class FcmTokenRequest(BaseModel):
    token: str = Field(..., min_length=20, max_length=4096)
    platform: str = Field(default="web", pattern="^web$")
    # Same convention as /api/lead: the signed anon token may travel in the
    # body (anon customers have no Firebase session to mint a bearer from).
    anon_token: str | None = Field(default=None, max_length=512)


def get_fcm_register_request_limit() -> int:
    # Env knob mirrors the lead cooldown pattern so ops can tighten the
    # registration brake per deployment without a code change.
    raw_limit = os.getenv("IP_RATE_LIMIT_FCM_REGISTER_MAX_REQUESTS")
    if raw_limit:
        return int(raw_limit)
    return FCM_REGISTER_RATE_LIMIT_DEFAULT


def _rate_limit_client_ip(request: Request) -> str | None:
    # Non-IP peers (hostname proxies, "unknown") cannot key the INET-backed
    # store; degrade to cooldown-only protection like the lead route does.
    try:
        return str(ipaddress.ip_address(get_client_ip_address(request)))
    except (TypeError, ValueError):
        return None


async def _enforce_registration_rate_limit(request: Request) -> None:
    client_ip = _rate_limit_client_ip(request)
    if client_ip is None:
        return
    settings = get_settings()
    allowed = await get_rate_limit_port().check_rate_limit_allowed(
        client_ip,
        RATE_LIMIT_KIND_FCM_REGISTER,
        get_fcm_register_request_limit(),
        settings.ip_rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=429, detail="Too many device registration requests from this address"
        )


async def _resolve_registration_identity(
    request: Request, anon_token: str | None
) -> tuple[str, str]:
    """Return (identity_type, identity_key) from exactly one verified mode.

    A present Authorization header is authoritative: an invalid bearer is a
    401, never a silent downgrade to the anon path.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=auth_header.partition(" ")[2].strip()
        )
        principal = await require_authenticated(bearer_credentials=credentials)
        return IDENTITY_TYPE_FIREBASE, principal.firebase_uid
    claimed_token = anon_token or request.headers.get("x-anon-token")
    if claimed_token:
        claims = get_anonymous_identity_service().verify_token(claimed_token)
        if claims is not None:
            return IDENTITY_TYPE_ANON_CUSTOMER, claims.subject
    raise HTTPException(
        status_code=401,
        detail="A verified Firebase bearer token or anonymous token is required",
    )


class FcmTestResponse(BaseModel):
    # Mirrors CallStartedResponse so the FE can reuse one polling/result shape:
    # "did the caller's own devices get a push attempt, and are any registered?"
    dispatched_tokens: int
    customer_has_device: bool


@router.post("/test", status_code=202, response_model=FcmTestResponse)
async def test_fcm_delivery(
    request: Request,
    background_tasks: BackgroundTasks,
    service: FcmNotificationService = Depends(get_fcm_notification_service),  # noqa: B008
) -> FcmTestResponse:
    """Send a data-only test push to the CALLER'S OWN registered devices.

    Why 202 + dispatched_tokens=0 (not 404) when nothing is registered: the
    call-started endpoint already established that precedent, and a test ping
    for an unregistered device is an expected "nothing to verify" state rather
    than an error — the FE shows "register a device first" instead of failing.
    Auth reuses the registration identity resolver so a user can only ever
    reach their own tokens (the anon bearer is scoped to their subject).
    """
    await _enforce_registration_rate_limit(request)
    identity_type, identity_key = await _resolve_registration_identity(request, None)
    # Synchronous resolve is ONLY to report device presence in the response; the
    # actual send runs through the tested send_test seam in the background task.
    tokens = await service.resolve_identity_tokens(
        identity_type=identity_type, identity_key=identity_key
    )
    if tokens:
        # Reuse the tested send_test seam (data-only message, title/body None) so
        # the shipped path is exactly what the unit tests exercise; this is the
        # message that deterministically hits the SW onBackgroundMessage path.
        background_tasks.add_task(
            service.send_test,
            identity_type=identity_type,
            identity_key=identity_key,
            data={
                "type": "fcm_test",
                "sent_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    return FcmTestResponse(
        dispatched_tokens=len(tokens),
        customer_has_device=len(tokens) > 0,
    )


@router.post("/device-token", status_code=204)
async def register_device_token(
    payload: FcmTokenRequest,
    request: Request,
) -> Response:
    await _enforce_registration_rate_limit(request)
    identity_type, identity_key = await _resolve_registration_identity(request, payload.anon_token)
    await get_fcm_tokens().register_fcm_token(
        identity_type=identity_type,
        identity_key=identity_key,
        token=payload.token,
        platform=payload.platform,
    )
    return Response(status_code=204)


@router.delete("/device-token", status_code=204)
async def delete_device_token(
    payload: FcmTokenRequest,
    request: Request,
) -> Response:
    await _enforce_registration_rate_limit(request)
    identity_type, identity_key = await _resolve_registration_identity(request, payload.anon_token)
    # Ownership-scoped removal: only the caller's own registration is disabled.
    await get_fcm_tokens().remove_fcm_token(
        identity_type=identity_type, identity_key=identity_key, token=payload.token
    )
    return Response(status_code=204)


class CallStartedRequest(BaseModel):
    # extra="forbid": a caller-supplied recipient is rejected outright — the
    # whole point of server-side resolution is that clients cannot name it.
    model_config = ConfigDict(extra="forbid")

    lead_id: int = Field(..., ge=1)


class CallStartedResponse(BaseModel):
    accepted: bool
    # dispatched_tokens counts the customer's REGISTERED devices resolved from
    # the DB synchronously; the actual FCM sends run after the response is
    # returned (see call_started), so this is "devices found", not "sends
    # confirmed". The staff UI only needs to know whether the customer can be
    # reached at all — customer_has_device is the actionable bit.
    dispatched_tokens: int
    customer_has_device: bool


def _customer_landing_url(project_key: str | None) -> str:
    # The service worker consumes data.url; the customer's own conversation
    # surface is the project chat page, falling back to the app root. Kept in
    # sync with the replicated rule in call_started_notification.py.
    return f"/project/{project_key}" if project_key else "/"


async def dispatch_call_started_push(
    service: FcmNotificationService,
    tokens: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str],
) -> None:
    """Post-response FCM fan-out; failures are only logged.

    Runs as a FastAPI background task so the service's 5s dispatch budget can
    never ride on the staff request that triggered the call.
    """
    try:
        await service.dispatch_to_tokens(tokens=tokens, title=title, body=body, data=data)
    except Exception:  # noqa: BLE001 — delivery must never fail the call event
        logger.warning(
            "call-started FCM dispatch failed for %s tokens", len(tokens), exc_info=True
        )


@router.post("/call-started", status_code=202, response_model=CallStartedResponse)
async def call_started(
    payload: CallStartedRequest,
    background_tasks: BackgroundTasks,
    principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    service: FcmNotificationService = Depends(get_fcm_notification_service),  # noqa: B008
) -> CallStartedResponse:
    """Notify the lead's customer that their assigned sales is calling.

    FR-33: the actual send converges on the same DB CAS stamp as the CRM
    PATCH and sales-action paths, so whichever event fires first wins and the
    others become no-ops. Device presence is still resolved synchronously for
    the response fields BEFORE any stamp claim (a pure SELECT — the stamp is
    only claimed when the background dispatch runs, keeping the
    "customer_has_device" answer truthful without burning the once-guard on a
    presence probe)."""
    lead = await repo.get_lead_by_id(payload.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if principal.role == "sales" and lead.assigned_sales_id != principal.sales_id:
        raise HTTPException(status_code=403, detail="Lead is not assigned to this sales account")
    sales = (
        await repo.get_sales_by_id(lead.assigned_sales_id)
        if lead.assigned_sales_id is not None
        else None
    )
    await record_staff_action(
        get_staff_audit_store(),
        principal=principal,
        action=STAFF_AUDIT_ACTION_CALL_STARTED,
        lead_id=lead.id,
        customer_id=compute_customer_id(lead.phone),
        detail={"event": "fcm_call_started"},
    )
    if lead.customer_identity is None:
        # Legacy row written before the identity column existed: nothing to
        # resolve, and the UI still needs the "no device" answer.
        return CallStartedResponse(accepted=True, dispatched_tokens=0, customer_has_device=False)
    # Device resolution stays synchronous — one indexed SELECT, no network
    # fan-out — so the response can truthfully report whether the customer is
    # reachable; only the FCM sends move off the request path.
    tokens = await service.resolve_identity_tokens(
        identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
        identity_key=lead.customer_identity,
    )
    if tokens:
        # Stamp-gated once-dispatch: if another path (CRM PATCH / sales
        # action) already claimed the lead's call_notified_at stamp, this
        # task becomes a silent no-op instead of a duplicate push.
        background_tasks.add_task(
            dispatch_call_started_once, repo, service, lead_id=lead.id
        )
    return CallStartedResponse(
        accepted=True,
        dispatched_tokens=len(tokens),
        customer_has_device=len(tokens) > 0,
    )
