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
    async def create_lead(self, *, session_id: str | None, name: str | None, phone: str, consent: bool, note: str | None, budget_vnd: int | None) -> LeadRow: ...
    async def get_active_leads_for_sales(self, sales_id: int, limit: int = 50) -> list[LeadRow]: ...
    async def get_lead_by_id(self, lead_id: int) -> LeadRow | None: ...
    async def update_lead(self, lead_id: int, *, status: str, assigned_sales_id: int | None = None, lock_expires_at: datetime | None = None, close: bool = False) -> LeadRow | None: ...
    async def add_assignment_log(self, lead_id: int, sales_id: int | None, action: str, note: str | None) -> AssignmentLogRow: ...
    async def get_tried_sales_ids(self, lead_id: int) -> list[int]: ...
    async def get_sales_stats(self, sales_id: int) -> SalesStats: ...


async def get_lead_repository() -> LeadRepository:
    from api.infrastructure.adapters.postgres_leads import PostgresLeadRepository
    return PostgresLeadRepository()
