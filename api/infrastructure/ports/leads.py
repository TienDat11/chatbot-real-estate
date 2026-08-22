"""Persistence port for the lead-routing workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class SalesRow:
    id: int
    access_key: str
    full_name: str
    role: str | None
    phone: str | None
    is_active: bool
    priority: int
    last_seen_at: datetime | None
    last_assigned_at: datetime | None = None


@dataclass(frozen=True)
class LeadRow:
    id: int
    session_id: str | None
    project_key: str | None
    device_id: str | None
    name: str | None
    phone: str
    consent: bool
    note: str | None
    budget_vnd: int | None
    created_at: datetime
    status: str
    assigned_sales_id: int | None
    lock_expires_at: datetime | None
    escal_count: int
    last_action_at: datetime | None
    closed_at: datetime | None
    # Story 9.2 consent split + mirror bookkeeping. `consent_service` /
    # `consent_marketing` default None so legacy fakes and pre-migration reads
    # keep constructing rows; `consent` stays the transition fallback.
    rejection_reason: str | None = None
    reengage_at: datetime | None = None
    mirror_status: str = "pending"
    consent_service: bool | None = None
    consent_marketing: bool | None = None
    consent_at: datetime | None = None
    consent_version: str | None = None
    marketing_withdrawn_at: datetime | None = None


@dataclass(frozen=True)
class AssignmentLogRow:
    id: int
    lead_id: int
    sales_id: int | None
    action: str
    note: str | None
    created_at: datetime


@dataclass(frozen=True)
class SalesStats:
    today: dict[str, int]
    avg_answer_seconds: float | None


class LeadRepository(Protocol):
    async def get_sales_by_key(self, access_key: str) -> SalesRow | None: ...
    async def update_sales_last_seen(self, sales_id: int) -> None: ...
    async def list_active_sales(self) -> list[SalesRow]: ...
    async def create_lead(self, *, session_id: str | None, project_key: str | None, device_id: str | None, name: str | None, phone: str, consent: bool, note: str | None, budget_vnd: int | None) -> LeadRow: ...
    async def get_active_leads_for_sales(self, sales_id: int, limit: int = 50) -> list[LeadRow]: ...
    async def get_lead_by_id(self, lead_id: int) -> LeadRow | None: ...
    async def update_lead(self, lead_id: int, *, status: str, assigned_sales_id: int | None = None, lock_expires_at: datetime | None = None, close: bool = False) -> LeadRow | None: ...
    async def add_assignment_log(self, lead_id: int, sales_id: int | None, action: str, note: str | None) -> AssignmentLogRow: ...
    async def get_tried_sales_ids(self, lead_id: int) -> list[int]: ...
    async def get_sales_stats(self, sales_id: int) -> SalesStats: ...
    async def get_sales_by_id(self, sales_id: int) -> SalesRow | None: ...
    async def set_lead_mirror_status(self, lead_id: int, *, mirror_status: str) -> LeadRow | None: ...
    async def list_stale_mirror_leads(self, *, stale_before: datetime, limit: int) -> list[LeadRow]: ...
    # Story 9.3 CRM reads/writes. Phone-keyed lookup serves the customer
    # search; customer_id (HMAC of phone) keyed lookup serves endpoints that
    # only carry the digested identifier, so the raw phone never travels back
    # to the client as a lookup key.
    async def get_leads_by_phone(self, phone: str) -> list[LeadRow]: ...
    async def get_leads_by_customer_id(self, customer_id: str) -> list[LeadRow]: ...
    async def update_lead_crm_state(self, lead_id: int, *, status: str, rejection_reason: str | None = None, reengage_at: datetime | None = None) -> LeadRow | None: ...
    async def set_marketing_consent_withdrawn_for_customer(self, customer_id: str) -> list[LeadRow]: ...
    # Story 9.4 re-approach loader: the PG-side PRE-FILTER of the marketing
    # consent gate (status lost + a stored reason + consent_marketing true +
    # no withdrawal). The workflow re-checks the gate in Python anyway — this
    # query only keeps obviously-ineligible rows out of the embedding spend.
    async def list_marketing_eligible_rejected_leads(self) -> list[LeadRow]: ...


async def get_lead_repository() -> LeadRepository:
    from api.infrastructure.adapters.postgres_leads import PostgresLeadRepository
    return PostgresLeadRepository()
