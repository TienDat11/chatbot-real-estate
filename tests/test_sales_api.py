"""Story 6.4: sales API key auth, LRU routing, and customer lead submission."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.application.services.lead_service import (
    choose_next_sales,
    create_customer_lead,
    handle_lead_action,
)
from api.infrastructure.ports.leads import AssignmentLogRow, LeadRow, SalesRow, SalesStats, get_lead_repository
from api.interfaces.api.main import create_app


def sales_row(
    sales_id: int,
    *,
    key: str | None = None,
    priority: int = 1,
    last_assigned_at: datetime | None = None,
) -> SalesRow:
    return SalesRow(
        id=sales_id,
        access_key=key or f"key-{sales_id}",
        full_name=f"Sales {sales_id}",
        role=None,
        phone=None,
        is_active=True,
        priority=priority,
        last_seen_at=None,
        last_assigned_at=last_assigned_at,
    )


def lead_row(lead_id: int, *, assigned_sales_id: int | None = None, status: str = "new") -> LeadRow:
    now = datetime.now()
    return LeadRow(
        id=lead_id,
        session_id="session-1",
        name="Khách test",
        phone="0905123456",
        consent=True,
        note="Quan tâm căn 2PN",
        budget_vnd=4_000_000_000,
        created_at=now,
        status=status,
        assigned_sales_id=assigned_sales_id,
        lock_expires_at=None,
        escal_count=0,
        last_action_at=None,
        closed_at=None,
    )


class FakeLeadRepository:
    def __init__(self) -> None:
        self.sales = [sales_row(1, priority=10), sales_row(2, priority=5)]
        self.leads: dict[int, LeadRow] = {}
        self.logs: list[AssignmentLogRow] = []
        self.last_seen: list[int] = []
        self.next_lead_id = 1

    async def get_sales_by_key(self, access_key: str) -> SalesRow | None:
        return next((sales for sales in self.sales if sales.access_key == access_key), None)

    async def update_sales_last_seen(self, sales_id: int) -> None:
        self.last_seen.append(sales_id)

    async def list_active_sales(self) -> list[SalesRow]:
        return self.sales

    async def create_lead(self, **kwargs) -> LeadRow:
        lead = lead_row(self.next_lead_id)
        self.next_lead_id += 1
        lead = replace(
            lead,
            session_id=kwargs["session_id"],
            name=kwargs["name"],
            phone=kwargs["phone"],
            consent=kwargs["consent"],
            note=kwargs["note"],
            budget_vnd=kwargs["budget_vnd"],
        )
        self.leads[lead.id] = lead
        return lead

    async def get_active_leads_for_sales(self, sales_id: int, limit: int = 50) -> list[LeadRow]:
        return [lead for lead in self.leads.values() if lead.assigned_sales_id == sales_id and lead.status in ("assigned", "callback")][:limit]

    async def get_lead_by_id(self, lead_id: int) -> LeadRow | None:
        return self.leads.get(lead_id)

    async def update_lead(self, lead_id: int, *, status: str, assigned_sales_id: int | None = None, lock_expires_at=None, close: bool = False) -> LeadRow | None:
        lead = self.leads.get(lead_id)
        if lead is None:
            return None
        lead = replace(
            lead,
            status=status,
            assigned_sales_id=lead.assigned_sales_id if assigned_sales_id is None else assigned_sales_id,
            lock_expires_at=lock_expires_at if lock_expires_at is not None else lead.lock_expires_at,
            closed_at=datetime.now() if close else lead.closed_at,
        )
        self.leads[lead_id] = lead
        return lead

    async def add_assignment_log(self, lead_id: int, sales_id: int | None, action: str, note: str | None) -> AssignmentLogRow:
        row = AssignmentLogRow(len(self.logs) + 1, lead_id, sales_id, action, note, datetime.now())
        self.logs.append(row)
        return row

    async def get_tried_sales_ids(self, lead_id: int) -> list[int]:
        return [log.sales_id for log in self.logs if log.lead_id == lead_id and log.sales_id is not None]

    async def get_sales_stats(self, sales_id: int) -> SalesStats:
        return SalesStats(today={"assigned": 0, "called": 0, "heard": 0, "booked": 0, "no_answer": 0, "escalated": 0}, avg_answer_seconds=None)


def test_choose_next_sales_is_lru_before_priority() -> None:
    older = datetime(2026, 1, 1)
    newer = datetime(2026, 1, 2)
    selected = choose_next_sales(
        [sales_row(1, priority=99, last_assigned_at=newer), sales_row(2, priority=1, last_assigned_at=older)],
        [],
    )
    assert selected is not None
    assert selected.id == 2


@pytest.mark.asyncio
async def test_submit_lead_assigns_highest_priority_when_all_unassigned() -> None:
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        name="Anh Test",
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    assert lead.status == "assigned"
    assert lead.assigned_sales_id == 1
    assert repo.logs[-1].action == "assign"


@pytest.mark.asyncio
async def test_no_answer_reassigns_to_next_sales_immediately() -> None:
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    updated = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")
    assert updated is not None
    assert updated.status == "assigned"
    assert updated.assigned_sales_id == 2
    assert [entry.action for entry in repo.logs][-2:] == ["no_answer", "assign"]


def test_sales_key_auth_and_customer_submit_http() -> None:
    repo = FakeLeadRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    client = TestClient(app)

    unauthorized = client.get("/api/sales/leads")
    assert unauthorized.status_code == 422
    invalid = client.get("/api/sales/leads", headers={"X-Sales-Key": "not-a-key"})
    assert invalid.status_code == 401

    submitted = client.post(
        "/api/lead",
        json={"session_id": "session-http", "phone": "0905 123 456", "consent": True},
    )
    assert submitted.status_code == 201
    assert submitted.json()["will_call_within_minutes"] == 5

    board = client.get("/api/sales/leads", headers={"X-Sales-Key": "key-1"})
    assert board.status_code == 200
    assert board.json()["leads"][0]["phone"] == "0905123456"
    assert repo.last_seen == [1]


def test_customer_submit_rejects_missing_consent_and_invalid_phone() -> None:
    repo = FakeLeadRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    client = TestClient(app)

    assert client.post("/api/lead", json={"phone": "0905123456", "consent": False}).status_code == 400
    assert client.post("/api/lead", json={"phone": "123", "consent": True}).status_code == 422
