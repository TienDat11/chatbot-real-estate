"""Reassign lead 105 to the E2E sales account and refresh its mirror."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.application.services.lead_mirror_service import sync_lead_mirror_after_commit
from api.infrastructure.adapters.postgres_leads import PostgresLeadRepository, get_lead_pool
from api.infrastructure.dependencies import get_realtime_lead_mirror
from api.infrastructure.ports.leads import LeadRow


async def main() -> None:
    repo = PostgresLeadRepository()
    pool = await get_lead_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            sales = await conn.fetchrow(
                "SELECT id, firebase_uid FROM sales WHERE id = $1",
                6,
            )
            if sales is None:
                raise RuntimeError("E2E sales account id=6 was not found")
            lead = await conn.fetchrow(
                "UPDATE leads SET assigned_sales_id = $1, mirror_status = 'pending', "
                "last_action_at = now() WHERE id = $2 RETURNING "
                "id, session_id, project_key, device_id, name, phone, consent, note, "
                "budget_vnd, created_at, status, assigned_sales_id, lock_expires_at, "
                "escal_count, last_action_at, closed_at, rejection_reason, reengage_at, "
                "mirror_status, consent_service, consent_marketing, consent_at, "
                "consent_version, marketing_withdrawn_at",
                6,
                105,
            )
            if lead is None:
                raise RuntimeError("Lead id=105 was not found")
    mirror = await get_realtime_lead_mirror()
    status = await sync_lead_mirror_after_commit(
        repo=repo, lead=LeadRow(**dict(lead)), mirror=mirror
    )
    print(f"lead_id=105 assigned_sales_id=6 mirror_status={status}")


if __name__ == "__main__":
    asyncio.run(main())
