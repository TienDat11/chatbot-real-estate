"""Sales API — broker board endpoints (Epic 5/6 Story 6.4) plus the G3-r6
staff notification read model and private training history routes.

The legacy broker-board endpoints keep their existing ``verify_sales_key``
contract untouched; the new /notifications and /training surfaces ride the
same Firebase bearer path as the CRM routes (``require_sales_or_admin`` with
per-principal assignment scoping in the application services).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from api import get_cfg
from api.application.ports.staff_audit import STAFF_AUDIT_ACTION_TRAINING_SESSION_READ
from api.application.services.call_started_notification import queue_dispatch_call_started_once
from api.application.services.chat_history_service import (
    TRANSCRIPT_MAX_BODY_BYTES,
    ChatHistoryService,
    transcript_size_bytes,
)
from api.application.services.lead_service import get_sales_dashboard, handle_lead_action
from api.application.services.sales_notification_service import (
    NotificationBatchCapError,
    NotificationInvalidIdsError,
    NotificationLimitOutOfRangeError,
    NotificationMalformedCursorError,
    NotificationPageSizeCapError,
    SalesNotificationService,
)
from api.application.services.staff_audit_service import record_staff_action
from api.application.training_history import (
    TRAINING_MESSAGE_HARD_CAP,
    encode_training_cursor,
    filter_training_messages_for_response,
    parse_training_cursor,
)
from api.infrastructure.adapters.postgres_chat_history import repository as chat_repository
from api.infrastructure.dependencies import (
    get_sales_notifications_store,
    get_staff_audit_store,
)
from api.infrastructure.ports.leads import (
    LeadRepository,
    LeadRow,
    SalesRow,
    get_lead_repository,
)
from api.infrastructure.ports.realtime_mirror import (
    RealtimeLeadMirror,
    get_realtime_lead_mirror,
)
from api.interfaces.api.deps import AuthenticatedPrincipal, require_sales_or_admin
from api.interfaces.api.http_contract import (
    error_headers_with_correlation,
    resolve_correlation_id,
)

router = APIRouter(prefix="/api/sales", tags=["sales"])
logger = logging.getLogger("api.interfaces.api.sales")


def _legacy_sales_key_auth_enabled() -> bool:
    """Return true only for the explicitly enabled development migration mode."""
    environment = str(get_cfg("app_env", "") or "").strip().lower()
    return bool(get_cfg("sales_legacy_key_auth_enabled", False)) and environment in {
        "dev",
        "development",
        "test",
    }


# ----- Auth dependency -----

_bearer = HTTPBearer(auto_error=False)


async def verify_sales_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
    x_sales_key: str | None = Header(default=None, alias="X-Sales-Key"),
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
) -> SalesRow:
    """Authenticate sales with verified Firebase auth, or explicit dev legacy key."""
    if credentials is not None:
        from api.infrastructure.dependencies import get_firebase_auth_verifier
        from api.infrastructure.ports.firebase_auth import FirebaseAuthTokenError

        try:
            user = await get_firebase_auth_verifier().verify_id_token(credentials.credentials)
        except FirebaseAuthTokenError as exc:
            raise HTTPException(status_code=401, detail="Invalid Firebase credentials") from exc
        if user.role != "sales":
            raise HTTPException(status_code=403, detail="Sales role required")
        # The active-only lookup preserves the unmapped 404 contract. Repositories
        # that expose the full mapping lookup let us distinguish inactive rows.
        get_sales_for_admin = getattr(repo, "get_sales_for_admin", None)
        sales = (
            await get_sales_for_admin(user.firebase_uid)
            if get_sales_for_admin is not None
            else await repo.get_sales_by_firebase_uid(user.firebase_uid)
        )
        if sales is None:
            raise HTTPException(status_code=404, detail="Sales account not found")
        if not sales.is_active:
            raise HTTPException(status_code=403, detail="Inactive sales account")
    elif _legacy_sales_key_auth_enabled() and x_sales_key:
        sales = await repo.get_sales_by_key(x_sales_key)
        logger.info(
            "legacy sales authentication attempt",
            extra={"auth_method": "legacy_sales_key", "outcome": "success" if sales else "failure"},
        )
        if not sales:
            raise HTTPException(status_code=401, detail="Invalid or inactive sales key")
    else:
        raise HTTPException(status_code=401, detail="Firebase authentication required")
    await repo.update_sales_last_seen(sales.id)
    return sales


# ----- Models -----


class LeadActionRequest(BaseModel):
    action: str = Field(..., pattern="^(called|no_answer|callback|booked|lost)$")
    note: str | None = None


class LeadActionResponse(BaseModel):
    ok: bool
    lead_status: str | None = None


class LeadStatusPatchRequest(BaseModel):
    status: str = Field(..., pattern="^(called|callback|booked|lost)$")
    note: str | None = None


class LeadStatusPatchResponse(BaseModel):
    ok: bool
    lead_id: int
    lead_status: str


# ----- Endpoints -----


@router.get("/leads")
async def get_leads(
    sales: SalesRow = Depends(verify_sales_key),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
) -> dict[str, Any]:
    """Active leads assigned to this sales, sorted by lock_expires_at ASC."""
    return await get_sales_dashboard(repo, sales)


async def dispatch_call_started_sales_push(sales_id: int, lead: LeadRow) -> None:
    """Post-response sales push for a started call; failures are only logged.

    Runs as a FastAPI background task so the service's bounded FCM dispatch
    budget can never ride on the staff action that triggered the call.
    """
    try:
        from api.infrastructure.dependencies import get_fcm_notification_service  # noqa: PLC0415

        await get_fcm_notification_service().notify_sales(
            sales_id=sales_id,
            title="Cuộc gọi bắt đầu",
            body=lead.name or "Cuộc gọi với lead đã bắt đầu.",
            data={"type": "sales_call_started", "lead_id": str(lead.id)},
        )
    except Exception:  # noqa: BLE001 — push must never fail the committed action
        logger.warning("call-started sales push failed for lead_id=%s", lead.id, exc_info=True)


@router.post("/leads/{lead_id}/action", response_model=LeadActionResponse)
async def lead_action(
    lead_id: int,
    payload: LeadActionRequest,
    background_tasks: BackgroundTasks,
    sales: SalesRow = Depends(verify_sales_key),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    mirror: RealtimeLeadMirror = Depends(get_realtime_lead_mirror),  # noqa: B008
) -> LeadActionResponse:
    """Process sales action on their lead."""
    lead = await handle_lead_action(
        repo, sales, lead_id, payload.action, payload.note, mirror=mirror
    )
    if not lead:
        raise HTTPException(
            status_code=409,
            detail="Lead not found, not assigned to you, or not in actionable state",
        )
    if payload.action == "called":
        # Dispatch after the response is sent: FCM latency must never hold the
        # staff action, and a delivery failure (swallowed in the helper) cannot
        # fail the committed status transition.
        background_tasks.add_task(dispatch_call_started_sales_push, sales.id, lead)
        # FR-33: the customer-facing push converges on the same DB CAS stamp
        # as the CRM PATCH and tel-link paths — at most one send per lead.
        queue_dispatch_call_started_once(background_tasks, repo, lead_id=lead.id)
    return LeadActionResponse(ok=True, lead_status=lead.status)


@router.patch("/leads/{lead_id}/status", response_model=LeadStatusPatchResponse)
async def patch_lead_status(
    lead_id: int,
    payload: LeadStatusPatchRequest,
    background_tasks: BackgroundTasks,
    sales: SalesRow = Depends(verify_sales_key),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    mirror: RealtimeLeadMirror = Depends(get_realtime_lead_mirror),  # noqa: B008
) -> LeadStatusPatchResponse:
    """Broker marks lead as called/callback/booked/lost via PATCH (literal prompt contract)."""
    lead = await repo.get_lead_by_id(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.status == "new" and payload.status == "called":
        result = await handle_lead_action(
            repo, sales, lead_id, "called", payload.note, mirror=mirror
        )
        if not result:
            raise HTTPException(status_code=409, detail="Lead not assigned to you")
        # Same exactly-once client push guard as the action endpoint above.
        queue_dispatch_call_started_once(background_tasks, repo, lead_id=lead_id)
        return LeadStatusPatchResponse(ok=True, lead_id=lead_id, lead_status="called")
    if payload.status in ("callback", "booked", "lost"):
        action = "callback" if payload.status == "callback" else payload.status
        result = await handle_lead_action(repo, sales, lead_id, action, payload.note, mirror=mirror)
        if not result:
            raise HTTPException(status_code=409, detail="Lead not in actionable state")
        return LeadStatusPatchResponse(ok=True, lead_id=lead_id, lead_status=result.status)
    raise HTTPException(status_code=422, detail="Invalid status transition from current state")


@router.get("/stats")
async def get_stats(
    sales: SalesRow = Depends(verify_sales_key),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
) -> dict[str, Any]:
    """Today's stats for this sales."""
    return await get_sales_dashboard(repo, sales)


# ----- G3-r6 staff surfaces: notifications + private training history -----
#
# Both ride the SAME Firebase bearer path as the CRM routes
# (require_sales_or_admin): sales_id/owner scoping happens in the
# application services, never from a client-supplied identifier.

TRAINING_MAX_PAGE_SIZE = 50
TRAINING_DEFAULT_PAGE_SIZE = 20

# Same shape the query/lead surfaces accept for project keys; used for the
# optional /training/sessions filter so an invalid shape is a 422, not a
# silent empty list.
_PROJECT_KEY_QUERY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

_notification_service: SalesNotificationService | None = None


def get_notification_service() -> SalesNotificationService:
    """Process-wide notification service over the PG read model."""
    global _notification_service
    if _notification_service is None:
        _notification_service = SalesNotificationService(get_sales_notifications_store())
    return _notification_service


def get_chat_history_service() -> ChatHistoryService:
    """Training-history service over the durable chat repository."""
    return ChatHistoryService(chat_repository)


class FcmTokenRequest(BaseModel):
    token: str = Field(..., min_length=20, max_length=4096)
    platform: str = Field(default="web", pattern="^web$")


@router.post("/notifications/fcm-token", status_code=204)
async def register_fcm_token(
    payload: FcmTokenRequest,
    principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
) -> Response:
    """Register the caller's browser token in the backend storage."""
    if principal.sales_id is None:
        raise HTTPException(status_code=403, detail="Sales role required")
    from api.application.ports.fcm_notifications import IDENTITY_TYPE_FIREBASE
    from api.infrastructure.dependencies import get_fcm_tokens
    await get_fcm_tokens().register_fcm_token(
        identity_type=IDENTITY_TYPE_FIREBASE,
        identity_key=principal.firebase_uid,
        token=payload.token,
        platform=payload.platform,
    )
    return Response(status_code=204)


class NotificationReadRequest(BaseModel):
    """Mark-read batch: ids are decimal lead-id strings, read must be true."""

    notification_ids: list[str] = Field(..., min_length=1, max_length=200)
    read: bool

    @field_validator("read")
    @classmethod
    def _read_must_be_true(cls, value: bool) -> bool:
        # The wire contract only marks READ; an un-read verb would be a
        # different (unsupported) operation, so reject it as 422 here.
        if value is not True:
            raise ValueError("read must be true")
        return value


class NotificationReadAllRequest(BaseModel):
    """Read-all through an RFC3339 instant (pydantic parses/validates)."""

    through: datetime


def _rfc3339(moment: datetime) -> str:
    return moment.isoformat()


@router.get("/notifications")
async def list_notifications(
    request: Request,
    response: Response,
    status: str = Query(default="unread", pattern="^(unread|all)$"),
    limit: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    service: SalesNotificationService = Depends(get_notification_service),  # noqa: B008
) -> dict[str, Any]:
    """Assigned-lead notification read model (newest first, keyset cursor).

    Unread is the default view; the unread count covers ALL assigned leads
    regardless of the page. Admin principals (no sales mapping) converge to
    the empty read model. Responses are private/no-store: the items carry
    masked contact data scoped to the authenticated recipient.
    """
    correlation_id = resolve_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Authorization"
    try:
        outcome = await service.list_notifications(
            sales_id=principal.sales_id, status=status, limit=limit, cursor=cursor
        )
    except NotificationPageSizeCapError as exc:
        raise HTTPException(
            status_code=413, detail=str(exc), headers=error_headers_with_correlation(correlation_id)
        ) from exc
    except (
        NotificationLimitOutOfRangeError,
        NotificationInvalidIdsError,
    ) as exc:
        raise HTTPException(
            status_code=422, detail=str(exc), headers=error_headers_with_correlation(correlation_id)
        ) from exc
    except NotificationMalformedCursorError as exc:
        raise HTTPException(
            status_code=400, detail=str(exc), headers=error_headers_with_correlation(correlation_id)
        ) from exc
    return {
        "items": [
            {
                "id": item.id,
                "lead_id": item.lead_id,
                "project_key": item.project_key,
                "masked_phone": item.masked_phone,
                "display_name": item.display_name,
                "created_at": _rfc3339(item.created_at),
                "read_at": _rfc3339(item.read_at) if item.read_at is not None else None,
            }
            for item in outcome.items
        ],
        "unread_count": outcome.unread_count,
        "next_cursor": outcome.next_cursor,
    }


@router.patch("/notifications/read")
async def mark_notifications_read(
    payload: NotificationReadRequest,
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    service: SalesNotificationService = Depends(get_notification_service),  # noqa: B008
) -> dict[str, Any]:
    """Idempotent mark-read for an assigned batch; returns converged count."""
    correlation_id = resolve_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Authorization"
    try:
        outcome = await service.mark_read(
            sales_id=principal.sales_id, notification_ids=payload.notification_ids
        )
    except NotificationBatchCapError as exc:
        raise HTTPException(
            status_code=413, detail=str(exc), headers=error_headers_with_correlation(correlation_id)
        ) from exc
    except NotificationInvalidIdsError as exc:
        raise HTTPException(
            status_code=422, detail=str(exc), headers=error_headers_with_correlation(correlation_id)
        ) from exc
    return {"updated_ids": outcome.updated_ids, "unread_count": outcome.unread_count}


@router.post("/notifications/read-all")
async def mark_notifications_read_all(
    payload: NotificationReadAllRequest,
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    service: SalesNotificationService = Depends(get_notification_service),  # noqa: B008
) -> dict[str, Any]:
    """Mark every assigned lead created at or before ``through`` as read."""
    correlation_id = resolve_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Authorization"
    through = payload.through
    if through.tzinfo is None:
        # A naive RFC3339 instant is interpreted as UTC so the cutoff is
        # unambiguous server-side; aware datetimes pass through untouched.
        through = through.replace(tzinfo=timezone.utc)
    outcome = await service.mark_all_through(sales_id=principal.sales_id, through=through)
    return {"read_through": _rfc3339(outcome.read_through), "unread_count": outcome.unread_count}


@router.get("/training/sessions")
async def list_training_sessions(
    request: Request,
    response: Response,
    limit: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None),
    project_key: str | None = Query(default=None, max_length=64),
    principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    history: ChatHistoryService = Depends(get_chat_history_service),  # noqa: B008
) -> dict[str, Any]:
    """Private training history for the authenticated sales (admin: all).

    Sales principals only ever see rows they own (owner predicate in the
    repository). Admin sees every training row and the read is audited.
    Customer sessions can never hydrate this view: the repository predicates
    are mutually exclusive on answer_mode.
    """
    correlation_id = resolve_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Authorization"

    resolved_limit = TRAINING_DEFAULT_PAGE_SIZE if limit is None else limit
    if resolved_limit > TRAINING_MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="limit exceeds 50",
            headers=error_headers_with_correlation(correlation_id),
        )
    if project_key is not None and not _PROJECT_KEY_QUERY_PATTERN.fullmatch(project_key):
        raise HTTPException(
            status_code=422,
            detail="invalid project_key filter",
            headers=error_headers_with_correlation(correlation_id),
        )
    cursor_updated_at = None
    cursor_session_id = None
    if cursor is not None:
        try:
            cursor_updated_at, cursor_session_id = parse_training_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
                headers=error_headers_with_correlation(correlation_id),
            ) from exc
    owner = None if principal.role == "admin" else principal.firebase_uid
    if owner is None:
        await record_staff_action(
            get_staff_audit_store(),
            principal=principal,
            action=STAFF_AUDIT_ACTION_TRAINING_SESSION_READ,
            detail={"scope": "list", "correlation_id": correlation_id},
        )
    rows = await history.training_sessions(
        owner_firebase_uid=owner,
        limit=resolved_limit + 1,
        cursor_updated_at=cursor_updated_at,
        cursor_session_id=cursor_session_id,
        context_project_key=project_key,
    )
    has_more = len(rows) > resolved_limit
    page = rows[:resolved_limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_training_cursor(last.last_active_at, last.session_id)
    return {
        "items": [
            {
                "id": row.session_id,
                "title": row.title,
                "context": {"project_key": row.context_project_key},
                "created_at": _rfc3339(row.created_at),
                "updated_at": _rfc3339(row.last_active_at),
                "message_count": row.message_count,
            }
            for row in page
        ],
        "next_cursor": next_cursor,
    }


@router.get("/training/sessions/{session_id}")
async def get_training_session_detail(
    session_id: str,
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    history: ChatHistoryService = Depends(get_chat_history_service),  # noqa: B008
) -> dict[str, Any]:
    """One training session transcript, capped by the shared contract.

    Ownership: sales get 404 for absent sessions and 403 for another sales'
    session only after an existence check (admin-scoped read). Admin reads
    any session and each read is audited. Bodies above 500 messages or 1 MiB
    answer 413 with NO partial transcript.
    """
    correlation_id = resolve_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Authorization"

    def _err(status_code: int, detail: str) -> HTTPException:
        return HTTPException(
            status_code=status_code,
            detail=detail,
            headers=error_headers_with_correlation(correlation_id),
        )

    owner = None if principal.role == "admin" else principal.firebase_uid
    session = await history.training_session(session_id=session_id, owner_firebase_uid=owner)
    if session is None:
        # Distinguish "not mine" (exists, different owner) from "absent" only
        # as 403/404; the unscoped probe never leaks content, only existence
        # to an authenticated staff caller.
        probe = await history.training_session(session_id=session_id)
        if probe is not None and principal.role != "admin":
            raise _err(403, "Training session is not owned by this account")
        raise _err(404, "Training session not found")
    if principal.role == "admin":
        await record_staff_action(
            get_staff_audit_store(),
            principal=principal,
            action=STAFF_AUDIT_ACTION_TRAINING_SESSION_READ,
            detail={
                "scope": "detail",
                "session_id": session.session_id,
                "owner_uid": session.owner_firebase_uid,
                "correlation_id": correlation_id,
            },
        )
    rows = await history.training_messages(session_id=session_id)
    if len(rows) > TRAINING_MESSAGE_HARD_CAP:
        raise _err(413, "Transcript exceeds the 500 message cap")
    messages = filter_training_messages_for_response(rows)
    if transcript_size_bytes(messages) > TRANSCRIPT_MAX_BODY_BYTES:
        raise _err(413, "Transcript exceeds the 1 MiB body cap")
    body: dict[str, Any] = {
        "id": session.session_id,
        "title": session.title,
        "context": {"project_key": session.context_project_key},
        "answer_mode": "training",
        "created_at": _rfc3339(session.created_at),
        "updated_at": _rfc3339(session.last_active_at),
        "message_count": session.message_count,
        "messages": messages,
    }
    if principal.role == "admin":
        body["owner_firebase_uid"] = session.owner_firebase_uid
    return body
