"""CRM workspace API — customer search, PII reveal, lead status, consent
withdrawal (story 9.3 / ISSUE-09 backend half).

Routes are thin: every handler resolves the authenticated principal through
``require_sales_or_admin``, delegates to ``crm_customer_service`` for the
business invariants (PII minimality, ownership scoping, mirror refresh), and
maps the service's typed domain errors onto HTTP codes — no business logic
lives in this layer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from api.application.ports.reengage_queue import ReengageQueueStore
from api.application.ports.staff_audit import StaffAuditStore
from api.application.services.call_started_notification import queue_dispatch_call_started_once
from api.application.services.chat_history_service import (
    TRANSCRIPT_MAX_BODY_BYTES,
    TRANSCRIPT_MAX_MESSAGES,
    ChatHistoryService,
    project_safe_meta,
    transcript_size_bytes,
)
from api.application.services.crm_customer_service import (
    CrmCustomerNotFoundError,
    CrmInvalidCustomerPhoneError,
    CrmLeadAccessDeniedError,
    CrmLeadNotFoundError,
    CrmPhoneRevealAuditUnavailable,
    get_lead_conversation_for_staff,
    reveal_customer_phone_for_assigned_sales_only,
    search_customer_leads_by_phone,
    update_lead_crm_status_and_mirror,
    withdraw_customer_marketing_consent_and_mirror,
)
from api.application.services.crm_lead_query import (
    CrmLeadsPageQuery,
    query_crm_leads_page,
)
from api.application.services.lead_mirror_service import compute_customer_id
from api.application.services.lead_service import mask_phone
from api.application.services.phone_reveal_rate_limit import (
    RevealRateLimitedError,
    enforce_reveal_rate_limit,
    record_successful_reveal,
)
from api.infrastructure.dependencies import (
    get_chat_history_repository,
    get_reengage_queue_store,
    get_staff_audit_store,
)
from api.infrastructure.ports.leads import (
    LeadRepository,
    LeadRow,
    get_lead_repository,
)
from api.infrastructure.ports.realtime_mirror import (
    RealtimeLeadMirror,
    get_realtime_lead_mirror,
)
from api.interfaces.api.deps import AuthenticatedPrincipal, require_sales_or_admin
from api.interfaces.api.deps.admin import (
    SALES_OR_ADMIN_ALLOWED_ROLES,
    _bearer_credentials_extractor,
    _verifier_from_dependency_injection,
    resolve_authenticated_principal,
)
from api.interfaces.api.http_contract import resolve_correlation_id

router = APIRouter(prefix="/api/crm", tags=["crm"])


async def _require_transcript_sales_or_admin(
    request: Request,
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
        _bearer_credentials_extractor
    ),
) -> AuthenticatedPrincipal:
    """Preserve correlation and cache headers when transcript auth fails."""
    correlation_id = resolve_correlation_id(request)
    headers = {
        "X-Correlation-ID": correlation_id,
        "Cache-Control": "no-store, private",
        "Vary": "Authorization",
    }
    try:
        return await resolve_authenticated_principal(
            allowed_roles_for_dependency=SALES_OR_ADMIN_ALLOWED_ROLES,
            bearer_credentials=bearer_credentials,
            firebase_token_verifier=_verifier_from_dependency_injection(),
        )
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail="Authorization failed",
            headers={**(exc.headers or {}), **headers},
        ) from exc


# ----- Response models (masked-phone views only; never the raw number) -----


class CrmLeadSummary(BaseModel):
    # ``id`` is retained for existing CRM consumers (customer HMAC id); use
    # ``lead_id`` for routes that address the PostgreSQL lead row.
    id: str
    lead_id: int
    project_key: str | None
    display_name: str | None
    masked_phone: str
    lead_status: str
    assigned_sales_id: int | None
    created_at: datetime
    rejection_reason: str | None = None
    reengage_at: datetime | None = None
    consent_service: bool | None = None
    consent_marketing: bool | None = None
    marketing_consent_withdrawn_at: datetime | None = None


class CrmCustomerSearchRequest(BaseModel):
    phone: str = Field(..., min_length=1, max_length=64)


class CrmCustomerSearchResponse(BaseModel):
    customer_id: str
    masked_phone: str
    leads: list[CrmLeadSummary]


class CrmCustomerPhoneRevealResponse(BaseModel):
    customer_id: str
    phone: str


class CrmLeadStatusPatchRequest(BaseModel):
    # Same closed status set as the broker-board PATCH (story 6.4) so the CRM
    # never widens the lead state machine the DB CHECK already enforces.
    status: str = Field(
        ..., pattern="^(callback_required|called|call_completed|callback|booked|lost)$"
    )
    rejection_reason: str | None = Field(default=None, max_length=200)
    reengage_at: datetime | None = None


class CrmLeadStatusPatchResponse(BaseModel):
    ok: bool
    lead_id: int
    lead_status: str
    mirror_status: str
    lead: CrmLeadSummary


class CrmWithdrawMarketingConsentResponse(BaseModel):
    ok: bool
    customer_id: str
    updated_lead_ids: list[int]


class CrmLeadsPageResponse(BaseModel):
    items: list[CrmLeadSummary]
    next_cursor: str | None
    has_more: bool
    server_time: str


class CrmConversationMessage(BaseModel):
    role: str
    content: str
    meta: dict[str, object] | None
    created_at: datetime


class CrmConversationResponse(BaseModel):
    session_id: str | None
    project_key: str | None
    messages: list[CrmConversationMessage]


def _lead_summary(lead: LeadRow) -> CrmLeadSummary:
    """Project one lead row into the masked CRM view — the single place the
    wire shape is defined, guaranteeing no raw phone can slip into a body."""
    return CrmLeadSummary(
        id=compute_customer_id(lead.phone),
        lead_id=lead.id,
        project_key=lead.project_key,
        display_name=lead.name,
        masked_phone=mask_phone(lead.phone),
        lead_status=lead.status,
        assigned_sales_id=lead.assigned_sales_id,
        created_at=lead.created_at,
        rejection_reason=lead.rejection_reason,
        reengage_at=lead.reengage_at,
        consent_service=lead.consent_service,
        consent_marketing=lead.consent_marketing,
        marketing_consent_withdrawn_at=lead.marketing_withdrawn_at,
    )


# ----- Endpoints -----


@router.get("/leads/{lead_id}/conversation", response_model=CrmConversationResponse)
async def get_lead_conversation(
    lead_id: int,
    request: Request,
    response: Response,
    authenticated_principal: AuthenticatedPrincipal = Depends(  # noqa: B008
        _require_transcript_sales_or_admin
    ),
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    history_repository=Depends(get_chat_history_repository),  # noqa: B008
) -> CrmConversationResponse:
    """Return a safe transcript for the assigned sales owner or an admin."""
    correlation_id = resolve_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Authorization"
    error_headers = {
        "X-Correlation-ID": correlation_id,
        "Cache-Control": "no-store, private",
        "Vary": "Authorization",
    }
    try:
        outcome = await get_lead_conversation_for_staff(
            repo,
            ChatHistoryService(history_repository),
            lead_id=lead_id,
            principal=authenticated_principal,
        )
    except CrmLeadNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Conversation not found", headers=error_headers
        ) from exc
    except CrmLeadAccessDeniedError as exc:
        raise HTTPException(
            status_code=403, detail="Not authorized", headers=error_headers
        ) from exc
    if len(outcome.messages) > TRANSCRIPT_MAX_MESSAGES:
        raise HTTPException(
            status_code=413,
            detail="Conversation exceeds message limit",
            headers=error_headers,
        )
    messages = [
        {
            "role": message.role,
            "content": message.content,
            "meta": project_safe_meta(message.meta),
            "created_at": message.created_at.isoformat(),
        }
        for message in outcome.messages
    ]
    if transcript_size_bytes(messages) > TRANSCRIPT_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Conversation exceeds body limit",
            headers=error_headers,
        )
    return CrmConversationResponse(
        session_id=outcome.session_id,
        project_key=outcome.project_key,
        messages=messages,
    )


async def _search_customer(
    phone: str,
    authenticated_principal: AuthenticatedPrincipal,
    repo: LeadRepository,
) -> CrmCustomerSearchResponse:
    """Resolve a phone without allowing it into a URL, error, or response."""
    try:
        outcome = await search_customer_leads_by_phone(
            repo, phone=phone, principal=authenticated_principal
        )
    except CrmInvalidCustomerPhoneError as exc:
        raise HTTPException(status_code=422, detail="Invalid phone number") from exc
    except CrmCustomerNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Customer not found") from exc
    return CrmCustomerSearchResponse(
        customer_id=outcome.customer_id,
        masked_phone=outcome.masked_phone,
        leads=[_lead_summary(lead) for lead in outcome.leads],
    )


@router.post("/customers/search", response_model=CrmCustomerSearchResponse)
async def search_customer_by_phone(
    payload: CrmCustomerSearchRequest,
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
) -> CrmCustomerSearchResponse:
    """Resolve a phone from a JSON body; raw phone never appears in a URL."""
    return await _search_customer(payload.phone, authenticated_principal, repo)


@router.get("/customers/search", response_class=JSONResponse)
async def legacy_search_customer_by_phone(
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
) -> JSONResponse:
    """Deprecated PII-unsafe route; deliberately does not accept query phone."""
    return JSONResponse(
        status_code=410,
        content={"detail": "Legacy phone search is deprecated; use POST /api/crm/customers/search"},
        headers={"Deprecation": "true"},
    )


@router.get(
    "/customers/{customer_id}/phone",
    response_model=CrmCustomerPhoneRevealResponse,
)
async def reveal_customer_phone(
    customer_id: str,
    request: Request,
    response: Response,
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),  # noqa: B008
) -> CrmCustomerPhoneRevealResponse:
    """Full-phone reveal, restricted to the assigned sales (or an admin).

    G3-r6 contract hardening (behavior-preserving for authorized callers):
    the budget is checked BEFORE the PII read (an over-budget probe never
    touches the number), each successful reveal emits exactly one allowlisted
    audit record (never the phone), and the response carries the correlation
    id plus private no-store caching so the raw number never lands in a
    shared cache. 401/403/404 outcomes stay exactly as before.
    """
    correlation_id = resolve_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Authorization"
    try:
        enforce_reveal_rate_limit(authenticated_principal.firebase_uid)
    except RevealRateLimitedError as exc:
        raise HTTPException(
            status_code=429,
            detail="Phone reveal rate limit exceeded",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    try:
        revealed_phone = await reveal_customer_phone_for_assigned_sales_only(
            repo,
            customer_id=customer_id,
            principal=authenticated_principal,
            audit_store=audit_store,
            correlation_id=correlation_id,
        )
    except CrmCustomerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CrmLeadAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except CrmPhoneRevealAuditUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Phone reveal temporarily unavailable",
            headers={
                "X-Correlation-ID": correlation_id,
                "Cache-Control": "no-store, private",
                "Vary": "Authorization",
            },
        ) from exc
    record_successful_reveal(authenticated_principal.firebase_uid)
    return CrmCustomerPhoneRevealResponse(customer_id=customer_id, phone=revealed_phone)


@router.patch("/leads/{lead_id}/status", response_model=CrmLeadStatusPatchResponse)
async def patch_lead_crm_status(
    lead_id: int,
    payload: CrmLeadStatusPatchRequest,
    background_tasks: BackgroundTasks,
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    mirror: RealtimeLeadMirror = Depends(get_realtime_lead_mirror),  # noqa: B008
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),  # noqa: B008
) -> CrmLeadStatusPatchResponse:
    """Update a lead's CRM status in PG, then refresh the Firestore mirror so
    realtime clients converge without re-reading PG."""
    try:
        outcome = await update_lead_crm_status_and_mirror(
            repo,
            mirror,
            lead_id=lead_id,
            status=payload.status,
            rejection_reason=payload.rejection_reason,
            reengage_at=payload.reengage_at,
            principal=authenticated_principal,
            audit_store=audit_store,
        )
    except CrmLeadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CrmLeadAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # FR-33 exactly-once client push: only the assigned->called edge claims
    # the DB stamp and dispatches, as a post-response task the FCM latency
    # can never hold the PATCH response on.
    if (
        outcome.previous_status == "assigned"
        and outcome.updated_lead.status == "called"
    ):
        queue_dispatch_call_started_once(background_tasks, repo, lead_id=lead_id)
    return CrmLeadStatusPatchResponse(
        ok=True,
        lead_id=outcome.updated_lead.id,
        lead_status=outcome.updated_lead.status,
        mirror_status=outcome.mirror_status,
        lead=_lead_summary(outcome.updated_lead),
    )


@router.post(
    "/customers/{customer_id}/withdraw-marketing-consent",
    response_model=CrmWithdrawMarketingConsentResponse,
)
async def withdraw_marketing_consent(
    customer_id: str,
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    mirror: RealtimeLeadMirror = Depends(get_realtime_lead_mirror),  # noqa: B008
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),  # noqa: B008
    reengage_queue_store: ReengageQueueStore = Depends(get_reengage_queue_store),  # noqa: B008
) -> CrmWithdrawMarketingConsentResponse:
    """Stamp marketing-withdrawal on every lead of the customer and re-push
    each row to the realtime mirror (``consent_marketing`` flips to false).
    Pending re-approach suggestions for the customer are cancelled too."""
    try:
        withdrawn_leads = await withdraw_customer_marketing_consent_and_mirror(
            repo,
            mirror,
            customer_id=customer_id,
            principal=authenticated_principal,
            audit_store=audit_store,
            reengage_queue_store=reengage_queue_store,
        )
    except CrmCustomerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CrmLeadAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return CrmWithdrawMarketingConsentResponse(
        ok=True,
        customer_id=customer_id,
        updated_lead_ids=[lead.id for lead in withdrawn_leads],
    )


# Mirrors the leads.status CHECK in db/lead_schema.sql; anything else is 422
# before it can ever reach the (parameter-bound) query.
_LEAD_STATUS_VALUES = frozenset(
    {
        "new",
        "assigned",
        "called",
        "callback",
        "callback_required",
        "call_completed",
        "booked",
        "lost",
        "no_answer",
        "expired",
    }
)


@router.get("/leads", response_model=CrmLeadsPageResponse)
async def list_crm_leads(
    project_key: str | None = Query(default=None, max_length=64),  # noqa: B008
    status: str | None = Query(default=None, max_length=32),  # noqa: B008
    reengage_from: date | None = Query(default=None),  # noqa: B008
    reengage_to: date | None = Query(default=None),  # noqa: B008
    cursor: str | None = Query(default=None, max_length=512),  # noqa: B008
    limit: int = Query(default=20, ge=1, le=100),  # noqa: B008
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales_or_admin),  # noqa: B008
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
) -> CrmLeadsPageResponse:
    """Server-paginated, filtered lead listing (FR-31, contract C1).

    Keyset-paginated (no OFFSET, no total count); sales principals are scoped
    to their own leads inside the service, admins see all. All SQL is
    parameter-bound in the adapter; this layer only validates enum/date inputs.
    """
    if status is not None and status not in _LEAD_STATUS_VALUES:
        raise HTTPException(status_code=422, detail="Invalid status")
    try:
        page = await query_crm_leads_page(
            repo,
            principal=authenticated_principal,
            query=CrmLeadsPageQuery(
                project_key=project_key,
                status=status,
                reengage_from=reengage_from,
                reengage_to=reengage_to,
                cursor=cursor,
                limit=limit,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc
    return CrmLeadsPageResponse(
        items=[_lead_summary(lead) for lead in page.rows],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        server_time=datetime.now(timezone.utc).isoformat(),
    )
