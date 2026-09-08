"""Persistence port for the lead-routing workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
    # Identity Toolkit uid mapped by provisioning (issue 4F); None for
    # pre-Firebase rows where access_key itself doubles as the Firebase uid.
    # Defaulted so legacy fakes and pre-migration reads stay source-compatible.
    firebase_uid: str | None = None


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
    # FCM recipient resolution: verified anon identity subject when the lead
    # carried one, else the HMAC phone digest (never the raw phone). Defaulted
    # so legacy fakes and pre-migration reads keep constructing rows.
    customer_identity: str | None = None


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
    avg_answer_seconds_reason: str | None = None


@dataclass(frozen=True)
class AssignmentDecision:
    """Outcome of ONE atomic lead assignment decision (single transaction).

    ``lead`` is the post-decision row, or ``None`` when the ownership
    predicate failed (lead missing, already owned by someone else, or already
    closed) — a failed predicate never mutates the row and never writes a log.
    ``next_sales`` is the newly selected owner, or ``None`` when the decision
    expired the lead because every candidate was exhausted (owner predicate
    succeeded and the lead is now closed/expired).
    ``logs_written`` lists the assignment-log actions inserted inside the
    decision transaction, in commit order — a single ``assign``/``escalate``
    for a plain reassignment, or ``("no_answer", "assign")`` for the
    broker action path. Duplicate/stale insertions are excluded because the
    per-lead row lock serializes concurrent decisions for the same lead.
    """

    lead: LeadRow | None
    next_sales: SalesRow | None
    logs_written: tuple[str, ...] = ()


class LeadRepository(Protocol):
    async def get_sales_by_key(self, access_key: str) -> SalesRow | None: ...
    async def get_sales_by_firebase_uid(self, firebase_uid: str) -> SalesRow | None: ...
    async def get_sales_for_admin(self, firebase_uid: str) -> SalesRow | None: ...
    async def update_sales_last_seen(self, sales_id: int) -> None: ...
    async def list_active_sales(self) -> list[SalesRow]: ...
    async def list_sales(self, *, include_disabled: bool = True) -> list[SalesRow]: ...
    async def set_sales_active(self, firebase_uid: str, *, is_active: bool) -> SalesRow | None: ...
    async def create_sales_mapping(
        self, *, firebase_uid: str, full_name: str, phone: str | None = None, priority: int = 0
    ) -> SalesRow: ...
    async def create_lead(
        self,
        *,
        session_id: str | None,
        project_key: str | None,
        device_id: str | None,
        name: str | None,
        phone: str,
        consent: bool,
        note: str | None,
        budget_vnd: int | None,
        customer_identity: str | None = None,
    ) -> LeadRow: ...
    async def get_active_leads_for_sales(self, sales_id: int, limit: int = 50) -> list[LeadRow]: ...
    async def get_lead_by_id(self, lead_id: int) -> LeadRow | None: ...
    async def update_lead(
        self,
        lead_id: int,
        *,
        status: str,
        assigned_sales_id: int | None = None,
        lock_expires_at: datetime | None = None,
        close: bool = False,
        expected_assigned_sales_id: int | None = None,
    ) -> LeadRow | None: ...
    async def create_lead_if_phone_available(
        self,
        *,
        cooldown_seconds: int,
        session_id: str | None,
        project_key: str | None,
        device_id: str | None,
        name: str | None,
        phone: str,
        consent: bool,
        note: str | None,
        budget_vnd: int | None,
        customer_identity: str | None = None,
    ) -> LeadRow | None: ...
    async def add_assignment_log(
        self, lead_id: int, sales_id: int | None, action: str, note: str | None
    ) -> AssignmentLogRow: ...
    async def get_tried_sales_ids(self, lead_id: int) -> list[int]: ...
    async def assign_lead(
        self,
        lead_id: int,
        *,
        excluded_sales_ids: list[int],
        action: str = "assign",
        note: str | None = None,
        expected_assigned_sales_id: int | None = None,
        prelude_actions: tuple[tuple[str, int | None, str | None], ...] = (),
        preferred_firebase_uid: str | None = None,
    ) -> AssignmentDecision: ...
    async def get_sales_stats(self, sales_id: int) -> SalesStats: ...
    async def get_sales_by_id(self, sales_id: int) -> SalesRow | None: ...
    async def set_lead_mirror_status(
        self, lead_id: int, *, mirror_status: str
    ) -> LeadRow | None: ...
    # FR-33 exactly-once call-started claim: single CAS statement — true iff
    # the row was transitioned to 'called' and had no prior stamp. False for
    # a missing lead, a non-'called' status, or an already-stamped row.
    async def stamp_call_started(self, lead_id: int) -> bool: ...
    async def list_stale_mirror_leads(
        self, *, stale_before: datetime, limit: int
    ) -> list[LeadRow]: ...
    # Story 9.3 CRM reads/writes. Phone-keyed lookup serves the customer
    # search; customer_id (HMAC of phone) keyed lookup serves endpoints that
    # only carry the digested identifier, so the raw phone never travels back
    # to the client as a lookup key.
    async def get_leads_by_phone(self, phone: str) -> list[LeadRow]: ...
    async def get_leads_by_customer_id(self, customer_id: str) -> list[LeadRow]: ...
    async def update_lead_crm_state(
        self,
        lead_id: int,
        *,
        status: str,
        rejection_reason: str | None = None,
        reengage_at: datetime | None = None,
        assigned_sales_id: int | None = None,
        admin_authorized: bool = False,
    ) -> LeadRow | None: ...
    async def set_marketing_consent_withdrawn_for_customer(
        self,
        customer_id: str,
        *,
        assigned_sales_id: int | None = None,
        admin_authorized: bool = False,
    ) -> list[LeadRow]: ...
    # Story 9.4 re-approach loader: the PG-side PRE-FILTER of the marketing
    # consent gate (status lost + a stored reason + consent_marketing true +
    # no withdrawal). The workflow re-checks the gate in Python anyway — this
    # query only keeps obviously-ineligible rows out of the embedding spend.
    async def list_marketing_eligible_rejected_leads(self) -> list[LeadRow]: ...
    # FR-31 server-paginated CRM lead listing: returns at most ``limit + 1``
    # rows (the extra row signals ``has_more``) ordered by created_at DESC,
    # id DESC. Every predicate is bound from keyword args by the adapter —
    # this port never receives SQL fragments.
    async def search_crm_leads(
        self,
        *,
        assigned_sales_id: int | None,
        project_key: str | None,
        status: str | None,
        reengage_from: date | None,
        reengage_to: date | None,
        tz_name: str,
        cursor_created_at: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[LeadRow]: ...


async def get_lead_repository() -> LeadRepository:
    from api.infrastructure.adapters.postgres_leads import PostgresLeadRepository

    return PostgresLeadRepository()
