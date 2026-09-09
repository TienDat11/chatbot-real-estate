"""CRM customer/lead use-cases for the broker workspace (story 9.3).

The application layer owns the two business invariants the routes must never
implement inline:

1. **PII minimality** — a customer is addressed by ``customer_id`` (HMAC of
   the phone) and lead views carry only the masked phone; the raw number is
   revealed through exactly one owner-gated use-case that emits an audit log
   line per reveal.
2. **Ownership scoping** — a sales principal may only touch leads whose
   assignment resolves back to their own firebase uid (via the PG
   ``sales.access_key`` mapping carried on the principal as ``sales_id``);
   admins are unrestricted.

Every mutation re-pushes the Firestore mirror through
``sync_lead_mirror_after_commit`` so realtime clients converge without
re-reading PG — the same hybrid-D1 contract as lead submission (story 9.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from api.application.ports.reengage_queue import ReengageQueueStore
from api.application.ports.staff_audit import (
    STAFF_AUDIT_ACTION_LEAD_STATUS_UPDATED,
    STAFF_AUDIT_ACTION_MARKETING_CONSENT_WITHDRAWN,
    STAFF_AUDIT_ACTION_PHONE_REVEALED,
    StaffAuditStore,
)
from api.application.services.chat_history_service import ChatHistoryService
from api.application.services.lead_mirror_service import (
    compute_customer_id,
    sync_lead_mirror_after_commit,
)
from api.application.services.lead_service import (
    mask_phone,
    normalize_phone,
    validate_phone,
)
from api.application.services.phone_reveal_audit import (
    build_phone_reveal_audit_entry,
    record_phone_reveal_entry,
)
from api.application.services.staff_audit_service import record_staff_action
from api.infrastructure.ports.leads import LeadRepository, LeadRow
from api.infrastructure.ports.realtime_mirror import RealtimeLeadMirror
from api.interfaces.api.deps import AuthenticatedPrincipal

logger = logging.getLogger("api.crm_customer_service")

# Mirrors the broker-board PATCH contract (story 6.4) so the CRM surface never
# widens the state machine the DB CHECK constraint already enforces.
CRM_ALLOWED_LEAD_STATUSES = frozenset(
    {"callback_required", "called", "call_completed", "callback", "booked", "lost"}
)
CRM_SEARCH_RESULT_LIMIT = 100


class CrmInvalidCustomerPhoneError(ValueError):
    """The search phone is not a well-formed Vietnamese mobile number."""


class CrmCustomerNotFoundError(LookupError):
    """No leads exist for the addressed customer_id / phone."""


class CrmLeadNotFoundError(LookupError):
    """No lead exists for the addressed lead_id."""


class CrmLeadAccessDeniedError(PermissionError):
    """The sales principal is not the assigned owner of the addressed lead."""


class CrmPhoneRevealAuditUnavailable(RuntimeError):
    """The phone reveal audit could not be durably recorded."""


@dataclass(frozen=True)
class CustomerSearchOutcome:
    customer_id: str
    masked_phone: str
    leads: list[LeadRow]


@dataclass(frozen=True)
class LeadConversationOutcome:
    session_id: str | None
    project_key: str | None
    messages: list[Any]


@dataclass(frozen=True)
class LeadStatusUpdateOutcome:
    updated_lead: LeadRow
    # Post-sync convergence flag returned by sync_lead_mirror_after_commit —
    # the LeadRow snapshot predates the mirror push, so the flag is carried
    # separately to stay truthful about the final row state.
    mirror_status: str
    # Status BEFORE the mutation (read under the same ownership check): the
    # call-started exactly-once dispatch keys on the assigned->called edge.
    previous_status: str


def _is_unrestricted_admin(principal: AuthenticatedPrincipal) -> bool:
    return principal.role == "admin"


def principal_is_assigned_owner_of_any_lead(
    principal: AuthenticatedPrincipal, leads: list[LeadRow]
) -> bool:
    """Ownership test: the principal's sales_id (resolved from its verified
    firebase uid through ``sales.access_key``) must be the assignee of at
    least one of the customer's leads — the uid therefore round-trips to the
    assignment without the route ever handling raw uids."""
    if principal.sales_id is None:
        return False
    return any(
        lead.assigned_sales_id is not None and lead.assigned_sales_id == principal.sales_id
        for lead in leads
    )


def _ensure_principal_owns_any_customer_lead(
    principal: AuthenticatedPrincipal, leads: list[LeadRow]
) -> None:
    if _is_unrestricted_admin(principal):
        return
    if not principal_is_assigned_owner_of_any_lead(principal, leads):
        raise CrmLeadAccessDeniedError(
            "Caller is not the assigned sales for any of this customer's leads"
        )


def _ensure_principal_owns_lead(principal: AuthenticatedPrincipal, lead: LeadRow) -> None:
    if _is_unrestricted_admin(principal):
        return
    if (
        lead.assigned_sales_id is None
        or principal.sales_id is None
        or lead.assigned_sales_id != principal.sales_id
    ):
        raise CrmLeadAccessDeniedError("Caller is not the assigned sales for this lead")


async def search_customer_leads_by_phone(
    repo: LeadRepository,
    *,
    phone: str,
    principal: AuthenticatedPrincipal,
) -> CustomerSearchOutcome:
    """Resolve a phone to only the leads the caller may disclose.

    The phone lookup remains a bound repository query. Admins retain the full
    customer history; sales receive only rows assigned to their own sales id.
    Filtering before constructing the outcome ensures unassigned and other
    sales rows cannot disclose project, name, status, consent, or timestamps.
    """
    normalized_phone = normalize_phone(phone)
    if not validate_phone(normalized_phone):
        raise CrmInvalidCustomerPhoneError("Phone is not a valid Vietnamese mobile number")
    customer_id = compute_customer_id(normalized_phone)
    leads = (await repo.get_leads_by_phone(normalized_phone))[:CRM_SEARCH_RESULT_LIMIT]
    if not _is_unrestricted_admin(principal):
        leads = [
            lead
            for lead in leads
            if principal.sales_id is not None and lead.assigned_sales_id == principal.sales_id
        ][:CRM_SEARCH_RESULT_LIMIT]
    if not leads:
        raise CrmCustomerNotFoundError("No leads found for this phone")
    return CustomerSearchOutcome(
        customer_id=customer_id,
        masked_phone=mask_phone(normalized_phone),
        leads=leads,
    )


async def reveal_customer_phone_for_assigned_sales_only(
    repo: LeadRepository,
    *,
    customer_id: str,
    principal: AuthenticatedPrincipal,
    audit_store: StaffAuditStore | None = None,
    correlation_id: str | None = None,
) -> str:
    """Owner-gated raw-phone reveal: only the assigned sales (or an admin).

    Every successful reveal emits one durable audit entry built through the
    closed allowlist in ``phone_reveal_audit`` (action, actor, customer_id,
    lead_ids, occurred_at, correlation_id — never the number itself), so the
    audit trail cannot become a secondary PII leak channel.
    """
    leads = await repo.get_leads_by_customer_id(customer_id)
    if not leads:
        raise CrmCustomerNotFoundError("No leads found for this customer")
    _ensure_principal_owns_any_customer_lead(principal, leads)
    entry = build_phone_reveal_audit_entry(
        principal=principal,
        action=STAFF_AUDIT_ACTION_PHONE_REVEALED,
        customer_id=customer_id,
        lead_ids=[lead.id for lead in leads],
        correlation_id=correlation_id or "",
    )
    if not await record_phone_reveal_entry(audit_store, entry=entry):
        raise CrmPhoneRevealAuditUnavailable("Phone reveal audit unavailable")
    return leads[0].phone


async def get_lead_conversation_for_staff(
    repo: LeadRepository,
    history: ChatHistoryService,
    *,
    lead_id: int,
    principal: AuthenticatedPrincipal,
) -> LeadConversationOutcome:
    """Transcript read for the assigned sales (or an admin).

    BE-CRM empty state: a legitimate lead with no prior chat is NOT a 404 —
    the CRM renders an empty transcript (200, ``session_id=None``, the lead's
    own project key, zero messages). Only an absent lead 404s and only a
    non-owner 403s, so the status codes keep discriminating "no such lead"
    from "not yours" while "no chat yet" degrades to the empty view.
    """
    lead = await repo.get_lead_by_id(lead_id)
    if lead is None:
        raise CrmLeadNotFoundError("Lead not found")
    _ensure_principal_owns_lead(principal, lead)
    conversation = await history.lead_conversation(lead_id=lead_id)
    if conversation is None:
        return LeadConversationOutcome(
            session_id=None,
            project_key=lead.project_key,
            messages=[],
        )
    session_id, project_key, messages = conversation
    return LeadConversationOutcome(
        session_id=session_id,
        project_key=project_key,
        messages=messages,
    )


async def update_lead_crm_status_and_mirror(
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
    *,
    lead_id: int,
    status: str,
    rejection_reason: str | None,
    reengage_at: datetime | None,
    principal: AuthenticatedPrincipal,
    audit_store: StaffAuditStore | None = None,
) -> LeadStatusUpdateOutcome:
    """Patch a lead's CRM status, then refresh the realtime mirror document."""
    if status not in CRM_ALLOWED_LEAD_STATUSES:
        raise ValueError(f"Status must be one of {sorted(CRM_ALLOWED_LEAD_STATUSES)}")
    existing_lead = await repo.get_lead_by_id(lead_id)
    if existing_lead is None:
        raise CrmLeadNotFoundError("Lead not found")
    _ensure_principal_owns_lead(principal, existing_lead)

    updated_lead = await repo.update_lead_crm_state(
        lead_id,
        status=status,
        rejection_reason=rejection_reason,
        reengage_at=reengage_at,
        assigned_sales_id=principal.sales_id,
        admin_authorized=_is_unrestricted_admin(principal),
    )
    if updated_lead is None:
        if not _is_unrestricted_admin(principal):
            raise CrmLeadAccessDeniedError("Lead ownership changed before mutation")
        raise CrmLeadNotFoundError("Lead not found")
    mirror_status = await sync_lead_mirror_after_commit(updated_lead, repo=repo, mirror=mirror)
    await record_staff_action(
        audit_store,
        principal=principal,
        action=STAFF_AUDIT_ACTION_LEAD_STATUS_UPDATED,
        customer_id=compute_customer_id(updated_lead.phone),
        lead_id=lead_id,
        # old/new status pair is the audit-relevant context; the rejection
        # reason is free-text a customer may have written — keep it out.
        detail={"old_status": existing_lead.status, "new_status": updated_lead.status},
    )
    return LeadStatusUpdateOutcome(
        updated_lead=updated_lead, mirror_status=mirror_status, previous_status=existing_lead.status
    )


async def withdraw_customer_marketing_consent_and_mirror(
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
    *,
    customer_id: str,
    principal: AuthenticatedPrincipal,
    audit_store: StaffAuditStore | None = None,
    reengage_queue_store: ReengageQueueStore | None = None,
) -> list[LeadRow]:
    """Stamp marketing consent withdrawal on every lead of the customer.

    The withdrawal is customer-scoped (one person, many leads) but still
    ownership-gated: the acting sales must own at least one of the customer's
    leads, mirroring the reveal rule. The gate is re-checked atomically inside
    the repository mutation, so the update applies to ALL of the customer's
    rows — including rows assigned to other sales — and a mid-flight
    reassignment cannot silently narrow it. Pending re-approach suggestions
    (reengage queue) are cancelled best-effort. Every updated row is re-pushed
    to the mirror so realtime clients see ``consent_marketing`` flip to false.
    """
    customer_leads = await repo.get_leads_by_customer_id(customer_id)
    if not customer_leads:
        raise CrmCustomerNotFoundError("No leads found for this customer")
    _ensure_principal_owns_any_customer_lead(principal, customer_leads)

    withdrawn_leads = await repo.set_marketing_consent_withdrawn_for_customer(
        customer_id,
        assigned_sales_id=principal.sales_id,
        admin_authorized=_is_unrestricted_admin(principal),
    )
    if not withdrawn_leads:
        # Zero rows from the repository mutation: the customer's rows were all
        # reassigned away (non-admin lost ownership mid-flight) or vanished.
        if not _is_unrestricted_admin(principal):
            raise CrmLeadAccessDeniedError(
                "Caller is no longer the assigned sales for any of this customer's leads"
            )
        raise CrmCustomerNotFoundError("No leads found for this customer")
    if reengage_queue_store is not None:
        try:
            await reengage_queue_store.cancel_queue_entries_for_customer(customer_id)
        except Exception:  # noqa: BLE001 — suggestion cleanup must never fail the opt-out
            logger.warning(
                "reengage queue cancel failed for customer_id=%s", customer_id, exc_info=True
            )
    for withdrawn_lead in withdrawn_leads:
        await sync_lead_mirror_after_commit(withdrawn_lead, repo=repo, mirror=mirror)
    await record_staff_action(
        audit_store,
        principal=principal,
        action=STAFF_AUDIT_ACTION_MARKETING_CONSENT_WITHDRAWN,
        customer_id=customer_id,
        detail={"withdrawn_lead_ids": [lead.id for lead in withdrawn_leads]},
    )
    return withdrawn_leads
