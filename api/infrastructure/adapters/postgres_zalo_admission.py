"""Short PostgreSQL transactions; immutable source receipt precedes technical counter."""

from __future__ import annotations

import asyncio
import asyncpg

from api.application.ports.zalo_bot import ZaloAdmissionDecision, ZaloAdmissionUnavailable
from api.infrastructure.adapters.postgres_zalo_events import _transaction
from api.infrastructure.adapters.postgres_leads import get_lead_pool
from api.infrastructure.config.config import get_settings


class PostgresZaloAdmission:
    def __init__(self, *, settings=None):
        self._settings = settings if settings is not None else get_settings()

    async def admit(self, identity, source, *, event_id, claim_token):
        if not self._settings.zalo_bot_enabled:
            return ZaloAdmissionDecision.LOST_CLAIM
        try:
            # A source PK race gets exactly one duplicate-only transaction retry.
            async with asyncio.timeout(5):
                try:
                    return await self._admit(identity, source, event_id, claim_token, duplicate_only=False)
                except asyncpg.UniqueViolationError:
                    return await self._admit(identity, source, event_id, claim_token, duplicate_only=True)
        except Exception:
            pass
        raise ZaloAdmissionUnavailable()

    async def _admit(self, identity, source, event_id, token, *, duplicate_only):
        pool = await get_lead_pool()
        async with _transaction(pool) as conn:
            event = await conn.fetchrow(
                """SELECT identity_key,source_event_key FROM zalo_bot_events
                WHERE id=$1 AND status='processing' AND claim_token=$2
                  AND lease_until > clock_timestamp() FOR UPDATE""", event_id, token,
            )
            if event is None:
                return ZaloAdmissionDecision.LOST_CLAIM
            if event["identity_key"] != identity or event["source_event_key"] != source:
                return ZaloAdmissionDecision.LOST_CLAIM
            receipt = await conn.fetchrow(
                "SELECT identity_key,decision FROM zalo_bot_admissions WHERE source_event_key=$1", source,
            )
            if receipt is not None:
                if receipt["identity_key"] != identity:
                    raise ZaloAdmissionUnavailable()
                decision = {"started": ZaloAdmissionDecision.ALREADY_STARTED,
                            "braked": ZaloAdmissionDecision.ALREADY_BRAKED}.get(receipt["decision"])
                if decision is None:
                    raise ZaloAdmissionUnavailable()
                # MVCC actor fence only; never touch/lock the burst counter on receipt reads.
                if not await self._owned(conn, identity, token, event_id, 0):
                    return ZaloAdmissionDecision.LOST_CLAIM
                return decision
            if duplicate_only:
                raise ZaloAdmissionUnavailable()
            await conn.execute(
                """INSERT INTO zalo_bot_burst_windows (identity_key,window_start,admitted_count)
                VALUES ($1,now(),0) ON CONFLICT (identity_key) DO NOTHING""", identity,
            )
            counter = await conn.fetchrow(
                """SELECT window_start,admitted_count FROM zalo_bot_burst_windows
                WHERE identity_key=$1 FOR UPDATE""", identity,
            )
            minimum = self._settings.zalo_bot_core_timeout_seconds + self._settings.zalo_bot_lease_margin_seconds
            if not await self._owned(conn, identity, token, event_id, minimum):
                return ZaloAdmissionDecision.LOST_CLAIM
            window = await conn.fetchval(
                "SELECT to_timestamp(floor(extract(epoch FROM clock_timestamp())/$1)*$1)",
                self._settings.zalo_bot_burst_window_seconds,
            )
            count = counter["admitted_count"] if counter["window_start"] == window else 0
            started = count < self._settings.zalo_bot_burst_max_events
            decision = "started" if started else "braked"
            await conn.execute(
                """INSERT INTO zalo_bot_admissions (source_event_key,identity_key,decision)
                VALUES ($1,$2,$3)""", source, identity, decision,
            )
            await conn.execute(
                """UPDATE zalo_bot_burst_windows SET window_start=$2,admitted_count=$3
                WHERE identity_key=$1""", identity, window, count + int(started),
            )
            # Recheck after potentially delayed receipt/counter writes. Raising rolls
            # everything back; an uncertain commit is never permission at caller.
            if not await self._owned(conn, identity, token, event_id, minimum):
                raise ZaloAdmissionUnavailable()
        return ZaloAdmissionDecision.STARTED if started else ZaloAdmissionDecision.BRAKED

    @staticmethod
    async def _owned(conn, identity, token, event_id, minimum):
        return await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM zalo_bot_actor_leases a
            JOIN zalo_bot_events e ON e.identity_key=a.identity_key
            WHERE a.identity_key=$1 AND a.holder_token=$2 AND e.id=$3
              AND e.claim_token=$2 AND e.status='processing'
              AND a.lease_until > clock_timestamp()+make_interval(secs => $4)
              AND e.lease_until > clock_timestamp()+make_interval(secs => $4))""",
            identity, token, event_id, minimum,
        )
