"""Postgres adapter for Zalo Bot webhook event persistence (ZB-01).

Dedupe is a single atomic statement: INSERT ... ON CONFLICT DO NOTHING
RETURNING id. Callers must NOT SELECT-exists-then-INSERT (race-prone).
"""

from __future__ import annotations

from api.infrastructure.adapters.postgres_leads import get_lead_pool


async def insert_event(
    *,
    chat_id: str,
    message_id: str,
    event_name: str,
    text: str,
) -> int | None:
    """Persist one Zalo webhook event; return the new row id or None on conflict.

    Returns None when an event with the same (chat_id, message_id)
    already exists — the ON CONFLICT DO NOTHING path is provably a single
    atomic statement, not a check-then-act sequence.
    """
    # Reuse shared application pool despite its historical lead-specific name;
    # webhook ingress must not create a second connection pool.
    pool = await get_lead_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO zalo_bot_events
              (chat_id, message_id, event_name, text)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id, message_id) DO NOTHING
            RETURNING id""",
            chat_id,
            message_id,
            event_name,
            text,
        )
    return row["id"] if row else None
