"""Postgres storage for FCM registration tokens.

Rows are addressed by (identity_type, identity_key): 'firebase_uid' for staff
and 'anon_customer' for verified anonymous chat users. The legacy firebase_uid
column is kept in sync for firebase-type rows (expand-and-contract: the old
unique constraint still exists until a later release drops it).
"""

from __future__ import annotations

from api.application.ports.fcm_notifications import IDENTITY_TYPE_FIREBASE
from api.infrastructure.adapters.postgres_leads import get_lead_pool

# Abuse brake: a stolen/rotating client could otherwise register unbounded
# tokens per identity; oldest-by-last_seen loses first.
MAX_TOKENS_PER_IDENTITY = 5


class PostgresFcmTokenRepository:
    async def register_fcm_token(
        self, *, identity_type: str, identity_key: str, token: str, platform: str
    ) -> None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            # Insert + cap-eviction share ONE transaction guarded by a
            # per-identity advisory lock: concurrent registrations for the same
            # identity serialize, so the 5-token cap can never be exceeded and
            # an eviction can never race away a row another session just wrote.
            # The lock is transaction-scoped (released at commit/rollback).
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"fcm-register:{identity_type}:{identity_key}",
                )
                fresh_id = await conn.fetchval(
                    """
                    INSERT INTO fcm_device_tokens
                        (identity_type, identity_key, firebase_uid, token, platform,
                         enabled, last_seen_at)
                    VALUES ($1, $2, $3, $4, $5, true, now())
                    ON CONFLICT (identity_type, identity_key, token) DO UPDATE SET
                        platform = EXCLUDED.platform,
                        enabled = true,
                        last_seen_at = now(),
                        updated_at = now()
                    RETURNING id
                    """,
                    identity_type,
                    identity_key,
                    identity_key if identity_type == IDENTITY_TYPE_FIREBASE else None,
                    token,
                    platform,
                )
                # `id <> $4` pins the freshly registered row: even a clock-skew
                # last_seen_at tie cannot order it out of the retained window.
                await conn.execute(
                    """
                    DELETE FROM fcm_device_tokens
                    WHERE identity_type = $1 AND identity_key = $2 AND enabled
                      AND id <> $4
                      AND id NOT IN (
                        SELECT id FROM fcm_device_tokens
                        WHERE identity_type = $1 AND identity_key = $2 AND enabled
                        ORDER BY last_seen_at DESC, id DESC
                        LIMIT $3
                      )
                    """,
                    identity_type,
                    identity_key,
                    MAX_TOKENS_PER_IDENTITY,
                    fresh_id,
                )

    async def remove_fcm_token(self, *, identity_type: str, identity_key: str, token: str) -> None:
        # Scoped to the caller's own identity row set: one principal can never
        # disable another principal's registration for the same token string.
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE fcm_device_tokens
                SET enabled = false, updated_at = now()
                WHERE identity_type = $1 AND identity_key = $2 AND token = $3
                """,
                identity_type,
                identity_key,
                token,
            )

    async def list_fcm_tokens(self, *, identity_type: str, identity_key: str) -> list[str]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT token FROM fcm_device_tokens
                WHERE identity_type = $1 AND identity_key = $2 AND enabled
                """,
                identity_type,
                identity_key,
            )
        return [str(row["token"]) for row in rows]

    async def list_fcm_tokens_for_sales(self, *, sales_id: int) -> list[str]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.token FROM fcm_device_tokens d
                JOIN sales s ON s.firebase_uid = d.identity_key
                WHERE d.identity_type = $2 AND s.id = $1 AND d.enabled
                """,
                sales_id,
                IDENTITY_TYPE_FIREBASE,
            )
        return [str(row["token"]) for row in rows]

    async def prune_token(self, *, token: str) -> None:
        # A dead token is dead for every identity that registered it (FCM
        # revocation is device-level), so pruning is intentionally global.
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE fcm_device_tokens
                SET enabled = false, updated_at = now()
                WHERE token = $1 AND enabled
                """,
                token,
            )


__all__ = ["MAX_TOKENS_PER_IDENTITY", "PostgresFcmTokenRepository"]
