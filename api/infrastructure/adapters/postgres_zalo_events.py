"""Postgres adapter for Zalo Bot webhook event persistence (ZB-01).

Dedupe is a single atomic statement: INSERT ... ON CONFLICT DO NOTHING
RETURNING id. Callers must NOT SELECT-exists-then-INSERT (race-prone).
"""

from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING

from api.infrastructure.adapters import postgres_zalo_bindings

if TYPE_CHECKING:
    from api.application.services.zalo_bot_processor import ZaloEvent

_DB_TIMEOUT = 5.0


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


class PostgresZaloEventStore:
    """Roundtrip state only; shared core owns conversations and replay."""

    async def _fetchrow(self, query: str, *args):
        try:
            async with asyncio.timeout(_DB_TIMEOUT):
                pool = await get_lead_pool()
                async with pool.acquire() as conn:
                    return await conn.fetchrow(query, *args, timeout=_DB_TIMEOUT)
        except Exception:
            pass
        raise RuntimeError("Zalo Bot event storage unavailable")

    async def _update(self, query: str, *args) -> None:
        if await self._fetchrow(query, *args) is None:
            raise RuntimeError("Zalo Bot event storage unavailable")

    async def get_event(self, event_id: int) -> ZaloEvent | None:
        from api.application.services.zalo_bot_processor import ZaloEvent

        row = await self._fetchrow(
            """SELECT id, chat_id, text, status, identity_key, source_event_key,
                      assistant_outcome, outbound_message_id
            FROM zalo_bot_events WHERE id = $1""",
            event_id,
        )
        if row is None:
            return None
        try:
            values = dict(row)
            outcome = values["assistant_outcome"]
            if isinstance(outcome, str):
                outcome = json.loads(outcome)
            if outcome is not None and not isinstance(outcome, dict):
                raise ValueError
            values["assistant_outcome"] = outcome
            return ZaloEvent(**values)
        except Exception:
            pass
        raise RuntimeError("Zalo Bot event storage unavailable")

    async def authorize(self, identity: str) -> int | None:
        return await postgres_zalo_bindings.authorize(identity)

    async def set_identity(self, event_id: int, identity: str, source: str) -> None:
        await self._update(
            """UPDATE zalo_bot_events
            SET identity_key = $2, source_event_key = $3, updated_at = now()
            WHERE id = $1 RETURNING id""",
            event_id,
            identity,
            source,
        )

    async def cache_outcome(self, event_id: int, outcome: dict) -> None:
        await self._update(
            """UPDATE zalo_bot_events
            SET assistant_outcome = $2::jsonb, updated_at = now()
            WHERE id = $1 RETURNING id""",
            event_id,
            json.dumps(outcome, ensure_ascii=False),
        )

    async def record_outbound(self, event_id: int, message_id: str) -> None:
        await self._update(
            """UPDATE zalo_bot_events
            SET outbound_message_id = $2, updated_at = now()
            WHERE id = $1 RETURNING id""",
            event_id,
            message_id,
        )

    async def set_status(self, event_id: int, status: str) -> None:
        if status not in {"pending", "rejected", "done"}:
            raise ValueError("Invalid Zalo Bot event status")
        await self._update(
            """UPDATE zalo_bot_events
            SET status = $2, updated_at = now(),
                completed_at = CASE WHEN $2 = 'done' THEN now() ELSE NULL END
            WHERE id = $1 RETURNING id""",
            event_id,
            status,
        )
