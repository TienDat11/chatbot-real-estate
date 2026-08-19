"""Lead routing and broker action orchestration."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from api.infrastructure.ports.leads import LeadRepository, LeadRow, SalesRow

logger = logging.getLogger("api.lead_service")

_PHONE_SEPARATORS = re.compile(r"[\s,.-]")
_PHONE_PATTERN = re.compile(r"^(0|\+84)(3[2-9]|5[6-9]|7[0-9]|8[1-9]|9[0-9])[0-9]{7}$")
LEAD_LOCK_MINUTES = 5


def normalize_phone(value: str) -> str:
    return _PHONE_SEPARATORS.sub("", value.strip())


def validate_phone(value: str) -> bool:
    return bool(_PHONE_PATTERN.fullmatch(normalize_phone(value)))


def mask_phone(value: str) -> str:
    value = normalize_phone(value)
    if len(value) < 7:
        return "***"
    return value[:4] + "***" + value[-3:]


def choose_next_sales(active_sales: list[SalesRow], excluded_ids: list[int]) -> SalesRow | None:
    """Route LRU-first, with priority only as the tiebreaker."""
    candidates = [sales for sales in active_sales if sales.id not in excluded_ids]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda sales: (
            sales.last_assigned_at is not None,
            sales.last_assigned_at or datetime.min,
            -sales.priority,
            sales.id,
        ),
    )


async def _assign(
    repo: LeadRepository,
    lead_id: int,
    *,
    excluded_ids: list[int] | None = None,
    action: str = "assign",
    note: str | None = None,
) -> tuple[SalesRow | None, LeadRow | None]:
    tried_ids = await repo.get_tried_sales_ids(lead_id)
    sales = choose_next_sales(await repo.list_active_sales(), list(set((excluded_ids or []) + tried_ids)))
    if sales is None:
        lead = await repo.update_lead(lead_id, status="expired", close=True)
        if lead:
            await repo.add_assignment_log(lead_id, None, "expired", "[ESCALATED-ALL]")
        return None, lead

    lead = await repo.update_lead(
        lead_id,
        status="assigned",
        assigned_sales_id=sales.id,
        lock_expires_at=datetime.now() + timedelta(minutes=LEAD_LOCK_MINUTES),
    )
    if lead:
        await repo.add_assignment_log(lead_id, sales.id, action, note or "LRU routing")
    return sales, lead


async def create_customer_lead(
    repo: LeadRepository,
    *,
    session_id: str | None,
    name: str | None,
    phone: str,
    consent: bool,
    note: str | None,
    budget_vnd: int | None,
) -> LeadRow:
    lead = await repo.create_lead(
        session_id=session_id,
        name=name,
        phone=phone,
        consent=consent,
        note=note,
        budget_vnd=budget_vnd,
    )
    _, assigned = await _assign(repo, lead.id)
    return assigned or lead


async def handle_lead_action(
    repo: LeadRepository,
    sales: SalesRow,
    lead_id: int,
    action: str,
    note: str | None = None,
) -> LeadRow | None:
    lead = await repo.get_lead_by_id(lead_id)
    if not lead or lead.assigned_sales_id != sales.id:
        return None

    if action == "called":
        if lead.status != "assigned":
            return None
        await repo.add_assignment_log(lead.id, sales.id, "call", note)
        return await repo.update_lead(lead.id, status="called")

    if action == "no_answer":
        if lead.status not in ("assigned", "called"):
            return None
        await repo.add_assignment_log(lead.id, sales.id, "no_answer", note)
        _, reassigned = await _assign(
            repo,
            lead.id,
            excluded_ids=[sales.id],
            action="assign",
            note="Reassign after no_answer",
        )
        return reassigned

    if action == "callback":
        if lead.status not in ("assigned", "called"):
            return None
        await repo.add_assignment_log(lead.id, sales.id, "callback", note)
        return await repo.update_lead(
            lead.id,
            status="callback",
            lock_expires_at=datetime.now() + timedelta(minutes=15),
        )

    if action in ("booked", "lost"):
        if lead.status not in ("assigned", "called", "callback"):
            return None
        await repo.add_assignment_log(lead.id, sales.id, action, note)
        return await repo.update_lead(lead.id, status=action, close=True)

    return None


async def get_sales_dashboard(repo: LeadRepository, sales: SalesRow) -> dict[str, Any]:
    leads = await repo.get_active_leads_for_sales(sales.id)
    stats = await repo.get_sales_stats(sales.id)
    return {
        "server_time": datetime.now().isoformat(),
        "leads": [
            {
                "lead_id": lead.id,
                "name": lead.name,
                "phone": mask_phone(lead.phone) if lead.phone else None,
                "note": lead.note,
                "budget_vnd": lead.budget_vnd,
                "created_at": lead.created_at.isoformat(),
                "lock_expires_at": lead.lock_expires_at.isoformat() if lead.lock_expires_at else None,
                "escal_count": lead.escal_count,
            }
            for lead in leads
        ],
        "stats": {"today": stats.today, "avg_answer_seconds": stats.avg_answer_seconds},
    }
