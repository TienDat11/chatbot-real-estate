"""Postgres adapter for lead and sales operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from api.application.services.sql_leg import build_dsn
from api.infrastructure.ports.leads import AssignmentLogRow, LeadRow, SalesRow, SalesStats

_LEAD_COLUMNS = "id, session_id, project_key, device_id, name, phone, consent, note, budget_vnd, created_at, status, assigned_sales_id, lock_expires_at, escal_count, last_action_at, closed_at"
_lead_pool: asyncpg.Pool | None = None


async def get_lead_pool() -> asyncpg.Pool:
    global _lead_pool
    if _lead_pool is None or _lead_pool.is_closing():
        _lead_pool = await asyncpg.create_pool(build_dsn(), min_size=1, max_size=5)
    return _lead_pool


async def close_lead_pool() -> None:
    global _lead_pool
    if _lead_pool is not None and not _lead_pool.is_closing():
        await _lead_pool.close()
    _lead_pool = None


class PostgresLeadRepository:
    async def get_sales_by_key(self, access_key: str) -> SalesRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.id, s.access_key, s.full_name, s.role, s.phone, s.is_active,
                          s.priority, s.last_seen_at,
                          MAX(l.created_at) FILTER (WHERE l.action IN ('assign', 'escalate')) AS last_assigned_at
                   FROM sales s LEFT JOIN sales_assignment_log l ON l.sales_id = s.id
                   WHERE s.access_key = $1 AND s.is_active GROUP BY s.id""",
                access_key,
            )
        return SalesRow(**dict(row)) if row else None

    async def update_sales_last_seen(self, sales_id: int) -> None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE sales SET last_seen_at = now(), updated_at = now() WHERE id = $1", sales_id)

    async def list_active_sales(self) -> list[SalesRow]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT s.id, s.access_key, s.full_name, s.role, s.phone, s.is_active,
                          s.priority, s.last_seen_at,
                          MAX(l.created_at) FILTER (WHERE l.action IN ('assign', 'escalate')) AS last_assigned_at
                   FROM sales s LEFT JOIN sales_assignment_log l ON l.sales_id = s.id
                   WHERE s.is_active GROUP BY s.id"""
            )
        return [SalesRow(**dict(row)) for row in rows]

    async def create_lead(self, *, session_id: str | None, project_key: str | None, device_id: str | None, name: str | None, phone: str, consent: bool, note: str | None, budget_vnd: int | None) -> LeadRow:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO leads (session_id, project_key, device_id, name, phone, consent, note, budget_vnd) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING " + _LEAD_COLUMNS,
                session_id, project_key, device_id, name, phone, consent, note, budget_vnd,
            )
        return LeadRow(**dict(row))

    async def get_active_leads_for_sales(self, sales_id: int, limit: int = 50) -> list[LeadRow]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT " + _LEAD_COLUMNS + " FROM leads WHERE assigned_sales_id = $1 AND status IN ('assigned', 'callback') ORDER BY lock_expires_at ASC NULLS LAST LIMIT $2",
                sales_id, limit,
            )
        return [LeadRow(**dict(row)) for row in rows]

    async def get_lead_by_id(self, lead_id: int) -> LeadRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT " + _LEAD_COLUMNS + " FROM leads WHERE id = $1", lead_id)
        return LeadRow(**dict(row)) if row else None

    async def update_lead(self, lead_id: int, *, status: str, assigned_sales_id: int | None = None, lock_expires_at: datetime | None = None, close: bool = False) -> LeadRow | None:
        pool = await get_lead_pool()
        sets = ["status = $2", "last_action_at = now()"]
        values: list[Any] = [lead_id, status]
        if assigned_sales_id is not None:
            sets.append("assigned_sales_id = $" + str(len(values) + 1))
            values.append(assigned_sales_id)
        if lock_expires_at is not None:
            sets.append("lock_expires_at = $" + str(len(values) + 1))
            values.append(lock_expires_at)
        if close:
            sets.append("closed_at = now()")
        sql = "UPDATE leads SET " + ", ".join(sets) + " WHERE id = $1 RETURNING " + _LEAD_COLUMNS
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *values)
        return LeadRow(**dict(row)) if row else None

    async def add_assignment_log(self, lead_id: int, sales_id: int | None, action: str, note: str | None) -> AssignmentLogRow:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO sales_assignment_log (lead_id, sales_id, action, note)
                   VALUES ($1, $2, $3, $4) RETURNING id, lead_id, sales_id, action, note, created_at""",
                lead_id, sales_id, action, note,
            )
        return AssignmentLogRow(**dict(row))

    async def get_tried_sales_ids(self, lead_id: int) -> list[int]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT sales_id FROM sales_assignment_log WHERE lead_id = $1 AND sales_id IS NOT NULL", lead_id)
        return [int(row["sales_id"]) for row in rows]

    async def get_sales_stats(self, sales_id: int) -> SalesStats:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT action, COUNT(*) AS count FROM sales_assignment_log WHERE sales_id = $1 AND created_at >= CURRENT_DATE GROUP BY action", sales_id)
        counts = {str(row["action"]): int(row["count"]) for row in rows}
        return SalesStats(today={"assigned": counts.get("assign", 0), "called": counts.get("call", 0), "heard": counts.get("call", 0), "booked": counts.get("booked", 0), "no_answer": counts.get("no_answer", 0), "escalated": counts.get("escalate", 0)}, avg_answer_seconds=None)
