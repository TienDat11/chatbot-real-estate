"""Postgres adapter for the durable staff audit trail (story 9.5 / ISSUE-11).

One INSERT per audited action. PG is chosen over a Firestore `audit/`
collection because the audited entities (leads, sales mapping) live here, the
write rides the same pool as the mutation being observed, and the rows stay
queryable for future admin audit views without a new Firebase surface.
"""

from __future__ import annotations

import json
import logging

from api.application.ports.staff_audit import StaffAuditEntry
from api.infrastructure.adapters.postgres_leads import get_lead_pool

logger = logging.getLogger("api.adapters.postgres_staff_audit")


class PostgresStaffAuditStore:
    async def record_entry(self, entry: StaffAuditEntry) -> None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO staff_audit_log
                       (actor_firebase_uid, actor_role, actor_sales_id,
                        action, customer_id, lead_id, detail)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)""",
                entry.actor_firebase_uid,
                entry.actor_role,
                entry.actor_sales_id,
                entry.action,
                entry.customer_id,
                entry.lead_id,
                json.dumps(entry.detail),
            )
