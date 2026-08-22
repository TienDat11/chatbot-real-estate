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

from api.application.services.lead_mirror_service import (
    compute_customer_id,
    sync_lead_mirror_after_commit,
)
from api.application.services.lead_service import (
    mask_phone,
    normalize_phone,
    validate_phone,
)
from api.infrastructure.ports.leads import LeadRepository, LeadRow
from api.infrastructure.ports.realtime_mirror import RealtimeLeadMirror
from api.interfaces.api.deps import AuthenticatedPrincipal

logger = logging.getLogger("api.crm_customer_service")

# Mirrors the broker-board PATCH contract (story 6.4) so the CRM surface never
# widens the state machine the DB CHECK constraint already enforces.
CRM_ALLOWED_LEAD_STATUSES = frozenset({"called", "callback", "booked", "lost"})


class CrmInvalidCustomerPhoneError(ValueError):
    """The search phone is not a well-formed Vietnamese mobile number."""


class CrmCustomerNotFoundError(LookupError):
    """No leads exist for the addressed customer_id / phone."""


class CrmLeadNotFoundError(LookupError):
    """No lead exists for the addressed lead_id."""


class CrmLeadAccessDeniedError(PermissionError):
    """The sales principal is not the assigned owner of the addressed lead."""


@dataclass(frozen=True)
class CustomerSearchOutcome:
    customer_id: str
    masked_phone: str
    leads: list[LeadRow]


@dataclass(frozen=True)
class LeadStatusUpdateOutcome:
    updated_lead: LeadRow
    # Post-sync convergence flag returned by sync_lead_mirror_after_commit —
    # the LeadRow snapshot predates the mirror push, so the flag is carried
    # separately to stay truthful about the final row state.
    mirror_status: str


def _is_unrestricted_admin(principal: AuthenticatedPrincipal) -> bool:
    return principal.role == "admin"


def principal_is_assigned_owner_of_any_lead(
    principal: AuthenticatedPrincipal, leads: list[LeadRow]
) -> bool:
    """Ownership test: the principal's sales_id (resolved from its verified
    firebase uid through ``sales.access_key``) must be the assignee of at
    least one of the customer's leads — the uid therefore round-trips to the
    assignment without the route ever handling raw uids."""
    return any(lead.assigned_sales_id == principal.sales_id for lead in leads)


def _ensure_principal_owns_any_customer_lead(
    principal: AuthenticatedPrincipal, leads: list[LeadRow]
) -> None:
    if _is_unrestricted_admin(principal):
        return
    if not principal_is_assigned_owner_of_any_lead(principal, leads):
        raise CrmLeadAccessDeniedError(
            "Caller is not the assigned sales for any of this customer's leads"
        )


def _ensure_principal_owns_lead(
    principal: AuthenticatedPrincipal, lead: LeadRow
) -> None:
    if _is_unrestricted_admin(principal):
        return
    if lead.assigned_sales_id != principal.sales_id:
        raise CrmLeadAccessDeniedError("Caller is not the assigned sales for this lead")


async def search_customer_leads_by_phone(
    repo: LeadRepository, *, phone: str
) -> CustomerSearchOutcome:
    """Resolve a phone to its customer_id (server-side HMAC) and lead history.

    The query phone is normalized exactly like the submit path, so the digest
    matches the one the mirror writes for the same subscriber.
    """
    normalized_phone = normalize_phone(phone)
    if not validate_phone(normalized_phone):
        raise CrmInvalidCustomerPhoneError("Phone is not a valid Vietnamese mobile number")
    customer_id = compute_customer_id(normalized_phone)
    leads = await repo.get_leads_by_phone(normalized_phone)
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
) -> str:
    """Owner-gated raw-phone reveal: only the assigned sales (or an admin).

    Every successful reveal emits one audit log line. The line deliberately
    records actor + customer_id + lead ids — never the number itself, so the
    audit trail cannot become a secondary PII leak channel.
    """
    leads = await repo.get_leads_by_customer_id(customer_id)
    if not leads:
        raise CrmCustomerNotFoundError("No leads found for this customer")
    _ensure_principal_owns_any_customer_lead(principal, leads)
    logger.info(
        "crm.customer_phone_revealed actor_firebase_uid=%s actor_role=%s "
        "customer_id=%s lead_ids=%s",
        principal.firebase_uid,
        principal.role,
        customer_id,
        [lead.id for lead in leads],
    )
    return leads[0].phone


async def update_lead_crm_status_and_mirror(
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
    *,
    lead_id: int,
    status: str,
    rejection_reason: str | None,
    reengage_at: datetime | None,
    principal: AuthenticatedPrincipal,
) -> LeadStatusUpdateOutcome:
    """Patch a lead's CRM status, then refresh the realtime mirror document."""
    if status not in CRM_ALLOWED_LEAD_STATUSES:
        raise ValueError(
            f"Status must be one of {sorted(CRM_ALLOWED_LEAD_STATUSES)}"
        )
    existing_lead = await repo.get_lead_by_id(lead_id)
    if existing_lead is None:
        raise CrmLeadNotFoundError("Lead not found")
    _ensure_principal_owns_lead(principal, existing_lead)

    updated_lead = await repo.update_lead_crm_state(
        lead_id,
        status=status,
        rejection_reason=rejection_reason,
        reengage_at=reengage_at,
    )
    if updated_lead is None:
        raise CrmLeadNotFoundError("Lead not found")
    mirror_status = await sync_lead_mirror_after_commit(
        updated_lead, repo=repo, mirror=mirror
    )
    return LeadStatusUpdateOutcome(
        updated_lead=updated_lead, mirror_status=mirror_status
    )


async def withdraw_customer_marketing_consent_and_mirror(
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
    *,
    customer_id: str,
    principal: AuthenticatedPrincipal,
) -> list[LeadRow]:
    """Stamp marketing consent withdrawal on every lead of the customer.

    The withdrawal is customer-scoped (one person, many leads) but still
    ownership-gated: the acting sales must own at least one of the customer's
    leads, mirroring the reveal rule. Every updated row is re-pushed to the
    mirror so realtime clients see ``consent_marketing`` flip to false.
    """
    customer_leads = await repo.get_leads_by_customer_id(customer_id)
    if not customer_leads:
        raise CrmCustomerNotFoundError("No leads found for this customer")
    _ensure_principal_owns_any_customer_lead(principal, customer_leads)

    withdrawn_leads = await repo.set_marketing_consent_withdrawn_for_customer(
        customer_id
    )
    for withdrawn_lead in withdrawn_leads:
        await sync_lead_mirror_after_commit(withdrawn_lead, repo=repo, mirror=mirror)
    return withdrawn_leads
