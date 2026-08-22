"""CRM workspace API — customer search, PII reveal, lead status, consent
withdrawal (story 9.3 / ISSUE-09 backend half).

Routes are thin: every handler resolves the authenticated principal through
``require_sales_or_admin``, delegates to ``crm_customer_service`` for the
business invariants (PII minimality, ownership scoping, mirror refresh), and
maps the service's typed domain errors onto HTTP codes — no business logic
lives in this layer.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.application.services.crm_customer_service import (
    CrmCustomerNotFoundError,
    CrmInvalidCustomerPhoneError,
    CrmLeadAccessDeniedError,
    CrmLeadNotFoundError,
    search_customer_leads_by_phone,
    reveal_customer_phone_for_assigned_sales_only,
    update_lead_crm_status_and_mirror,
    withdraw_customer_marketing_consent_and_mirror,
)
from api.application.services.lead_service import mask_phone
from api.infrastructure.ports.leads import (
    LeadRepository,
    LeadRow,
    get_lead_repository,
)
from api.infrastructure.ports.realtime_mirror import (
    RealtimeLeadMirror,
    get_realtime_lead_mirror,
)
from api.infrastructure.dependencies import get_staff_audit_store
from api.application.ports.staff_audit import StaffAuditStore
from api.interfaces.api.deps import AuthenticatedPrincipal, require_sales_or_admin

router = APIRouter(prefix="/api/crm", tags=["crm"])


# ----- Response models (masked-phone views only; never the raw number) -----

class CrmLeadSummary(BaseModel):
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
        ..., pattern="^(called|callback|booked|lost)$"
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


def _lead_summary(lead: LeadRow) -> CrmLeadSummary:
    """Project one lead row into the masked CRM view — the single place the
    wire shape is defined, guaranteeing no raw phone can slip into a body."""
    return CrmLeadSummary(
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

@router.get("/customers/search", response_model=CrmCustomerSearchResponse)
async def search_customer_by_phone(
    phone: str = Query(..., min_length=9, max_length=20),
    authenticated_principal: AuthenticatedPrincipal = Depends(
        require_sales_or_admin
    ),
    repo: LeadRepository = Depends(get_lead_repository),
) -> CrmCustomerSearchResponse:
    """Resolve a phone to customer_id + lead history (masked phones only)."""
    try:
        outcome = await search_customer_leads_by_phone(repo, phone=phone)
    except CrmInvalidCustomerPhoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CrmCustomerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CrmCustomerSearchResponse(
        customer_id=outcome.customer_id,
        masked_phone=outcome.masked_phone,
        leads=[_lead_summary(lead) for lead in outcome.leads],
    )


@router.get(
    "/customers/{customer_id}/phone",
    response_model=CrmCustomerPhoneRevealResponse,
)
async def reveal_customer_phone(
    customer_id: str,
    authenticated_principal: AuthenticatedPrincipal = Depends(
        require_sales_or_admin
    ),
    repo: LeadRepository = Depends(get_lead_repository),
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),
) -> CrmCustomerPhoneRevealResponse:
    """Full-phone reveal, restricted to the assigned sales (or an admin).

    Each successful reveal is audit-logged by the service (actor uid, role,
    customer_id, lead ids — never the number itself)."""
    try:
        revealed_phone = await reveal_customer_phone_for_assigned_sales_only(
            repo,
            customer_id=customer_id,
            principal=authenticated_principal,
            audit_store=audit_store,
        )
    except CrmCustomerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CrmLeadAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return CrmCustomerPhoneRevealResponse(
        customer_id=customer_id, phone=revealed_phone
    )


@router.patch(
    "/leads/{lead_id}/status", response_model=CrmLeadStatusPatchResponse
)
async def patch_lead_crm_status(
    lead_id: int,
    payload: CrmLeadStatusPatchRequest,
    authenticated_principal: AuthenticatedPrincipal = Depends(
        require_sales_or_admin
    ),
    repo: LeadRepository = Depends(get_lead_repository),
    mirror: RealtimeLeadMirror = Depends(get_realtime_lead_mirror),
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),
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
    authenticated_principal: AuthenticatedPrincipal = Depends(
        require_sales_or_admin
    ),
    repo: LeadRepository = Depends(get_lead_repository),
    mirror: RealtimeLeadMirror = Depends(get_realtime_lead_mirror),
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),
) -> CrmWithdrawMarketingConsentResponse:
    """Stamp marketing-withdrawal on every lead of the customer and re-push
    each row to the realtime mirror (``consent_marketing`` flips to false)."""
    try:
        withdrawn_leads = await withdraw_customer_marketing_consent_and_mirror(
            repo, mirror, customer_id=customer_id, principal=authenticated_principal
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
