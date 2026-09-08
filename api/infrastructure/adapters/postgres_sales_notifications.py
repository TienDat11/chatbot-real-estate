"""Postgres adapter for the sales-notification read model (G3-r6).

Implements both ``SalesNotificationsRepository`` and
``AssignedLeadsRepository``: every statement carries the live
``leads.assigned_sales_id`` predicate so assignment revocation is immediate,
and read marking is an idempotent upsert on the ``(sales_id, lead_id)``
primary key with ``LEAST`` timestamp merge (the earliest read wins over a
stale replay). Raw phones never leave this module: the projection masks in
Python and the row objects only carry ``masked_phone``.
"""

from __future__ import annotations

from datetime import datetime

from api.application.ports.sales_notifications import NotificationLeadRow
from api.application.services.lead_service import mask_phone
from api.infrastructure.adapters.postgres_leads import get_lead_pool


class PostgresSalesNotificationsRepository:
    """One adapter, two narrow read-model protocols (notifications + leads)."""

    # --- SalesNotificationsRepository -----------------------------------------

    async def list_assigned_notification_leads(
        self,
        *,
        sales_id: int,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_lead_id: int | None,
    ) -> list[NotificationLeadRow]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT l.id, l.project_key, l.phone, l.name, l.created_at
                   FROM leads l
                   WHERE l.assigned_sales_id = $1
                     AND ($2::timestamptz IS NULL
                          OR (l.created_at, l.id) < ($2::timestamptz, $3::bigint))
                   ORDER BY l.created_at DESC, l.id DESC
                   LIMIT $4""",
                sales_id,
                cursor_created_at,
                cursor_lead_id,
                limit,
            )
        return [
            NotificationLeadRow(
                lead_id=row["id"],
                project_key=row["project_key"],
                masked_phone=mask_phone(row["phone"]) if row["phone"] else None,
                display_name=row["name"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def count_assigned_leads(self, *, sales_id: int) -> int:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM leads WHERE assigned_sales_id = $1", sales_id
            )
        return int(value or 0)

    async def count_unread_leads(self, *, sales_id: int) -> int:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """SELECT COUNT(*) FROM leads l
                   WHERE l.assigned_sales_id = $1
                     AND NOT EXISTS (
                         SELECT 1 FROM sales_notification_reads r
                         WHERE r.sales_id = $1 AND r.lead_id = l.id
                     )""",
                sales_id,
            )
        return int(value or 0)

    async def get_read_states(
        self, *, sales_id: int, lead_ids: list[int]
    ) -> dict[int, datetime]:
        if not lead_ids:
            return {}
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT r.lead_id, r.read_at
                   FROM sales_notification_reads r
                   WHERE r.sales_id = $1 AND r.lead_id = ANY($2::bigint[])""",
                sales_id,
                lead_ids,
            )
        return {int(row["lead_id"]): row["read_at"] for row in rows}

    async def mark_read(self, *, sales_id: int, lead_ids: list[int]) -> list[int]:
        """All-or-none idempotent marking of leads the caller is CURRENTLY
        assigned to: the INSERT selects only assignment-confirmed ids, and
        ON CONFLICT keeps the earliest read timestamp (read wins)."""
        if not lead_ids:
            return []
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """WITH requested AS (SELECT (unnest($2::bigint[]))::bigint AS lead_id)
                       INSERT INTO sales_notification_reads (sales_id, lead_id, read_at)
                       SELECT $1, l.id, now()
                       FROM leads l
                       JOIN requested q ON q.lead_id = l.id
                       WHERE l.assigned_sales_id = $1
                       ON CONFLICT (sales_id, lead_id) DO UPDATE
                         SET read_at = LEAST(sales_notification_reads.read_at, EXCLUDED.read_at)
                       RETURNING lead_id""",
                    sales_id,
                    lead_ids,
                )
        return sorted(int(row["lead_id"]) for row in rows)

    async def mark_read_through(self, *, sales_id: int, through: datetime) -> list[int]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """INSERT INTO sales_notification_reads (sales_id, lead_id, read_at)
                       SELECT $1, l.id, now()
                       FROM leads l
                       WHERE l.assigned_sales_id = $1 AND l.created_at <= $2
                       ON CONFLICT (sales_id, lead_id) DO UPDATE
                         SET read_at = LEAST(sales_notification_reads.read_at, EXCLUDED.read_at)
                       RETURNING lead_id""",
                    sales_id,
                    through,
                )
        return sorted(int(row["lead_id"]) for row in rows)

    # --- AssignedLeadsRepository (transcript re-check seam) --------------------

    async def get_lead_assignments(self, lead_ids: list[int]) -> dict[int, int | None]:
        """lead_id -> assigned_sales_id (None = unassigned) for existing leads.

        The transcript route distinguishes 404 (lead absent) from 403 (lead
        exists but the caller is not its assignee) with this single bounded
        read; no lead content crosses the boundary.
        """
        if not lead_ids:
            return {}
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, assigned_sales_id FROM leads WHERE id = ANY($1::bigint[])",
                lead_ids,
            )
        return {int(row["id"]): row["assigned_sales_id"] for row in rows}


# Structural typing: the adapter satisfies both Protocols without ABC
# registration (typing.Protocol is checked structurally, not nominally).

__all__ = ["PostgresSalesNotificationsRepository"]
