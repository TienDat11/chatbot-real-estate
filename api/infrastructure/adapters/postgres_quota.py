"""Postgres adapter for durable anonymous quota + per-IP rate limiting.

Secure wave Issue 1B. Reuses the project's existing RW pool accessor
(get_lead_pool — the same transactional core the leads/audit adapters write
through) instead of standing up a second pool; quota writes are tiny
single-statement upserts so pool pressure stays negligible.

Every write here is ONE atomic statement evaluated by Postgres under the row
lock (spec §4 R2/R3): concurrent API workers can never exceed the allowance or
double-grant the bonus, regardless of process count.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from api.application.services.quota_service import (
    QuotaRecord,
    check_ip_limit,
)
from api.infrastructure.adapters.postgres_leads import get_lead_pool


class PostgresQuotaStore:
    """QuotaRecordStore implementation; also exposes the RateLimitPort surface."""

    async def consume_turn_atomically(
        self, identity_key: str, effective_cap: int, *, project_key: str
    ) -> QuotaRecord | None:
        """Increment used_turns once iff allowance remains; None means exhausted.

        Single conditional upsert: a fresh identity inserts used=1 directly;
        an existing row is incremented only while
        used_turns < effective_cap + bonus_turns. When that guard fails,
        Postgres skips the update AND the insert, returning zero rows. The row
        is scoped by the (identity_key, project_key) composite PK so each
        project carries its own allowance.
        """
        reservation_id = str(uuid4())
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """INSERT INTO anon_quota AS target_row
                           (identity_key, project_key, used_turns)
                       VALUES ($1, $2, 1)
                       ON CONFLICT (identity_key, project_key) DO UPDATE
                           SET used_turns = target_row.used_turns + 1,
                               granted_turns = CASE
                                   WHEN target_row.used_turns >= $3 + target_row.bonus_turns
                                   THEN target_row.granted_turns - 1
                                   ELSE target_row.granted_turns
                               END,
                               updated_at = now()
                         WHERE target_row.used_turns < $3 + target_row.bonus_turns + target_row.granted_turns
                       RETURNING used_turns, bonus_turns, granted_turns, bonus_granted,
                         CASE WHEN used_turns <= $3 THEN 'base'
                              WHEN used_turns <= $3 + bonus_turns THEN 'bonus'
                              ELSE 'granted' END AS allowance_class""",  # noqa: E501
                    identity_key,
                    project_key,
                    effective_cap,
                )
                if row:
                    await connection.execute(
                        """INSERT INTO quota_reservations
                           (reservation_id, identity_key, project_key, allowance_class)
                           VALUES ($1, $2, $3, $4)""",
                        reservation_id, identity_key, project_key, row["allowance_class"],
                    )
        if row is None:
            return None
        values = dict(row)
        values["reservation_id"] = reservation_id
        return QuotaRecord(**values)

    async def finalize_turn_atomically(
        self, identity_key: str, *, project_key: str, reservation_id: str
    ) -> bool:
        """Finalize a successful reservation; retries are harmless no-ops."""
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            result = await connection.execute(
                """DELETE FROM quota_reservations
                   WHERE reservation_id = $1::uuid AND identity_key = $2 AND project_key = $3""",
                reservation_id, identity_key, project_key,
            )
        return result.endswith("1")

    async def refund_turn_atomically(
        self,
        identity_key: str,
        *,
        project_key: str,
        reservation_id: str | None = None,
    ) -> QuotaRecord | None:
        """Decrement usage for one reservation, then consume that reservation.

        The reservation row is locked before the counter update; deleting it in
        the same transaction makes duplicate/concurrent refunds idempotent.
        """
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                reservation = await connection.fetchrow(
                    """SELECT reservation_id, allowance_class
                       FROM quota_reservations
                       WHERE reservation_id = COALESCE($3::uuid, (
                           SELECT reservation_id FROM quota_reservations
                           WHERE identity_key = $1 AND project_key = $2
                           ORDER BY created_at DESC LIMIT 1
                       ))
                         AND identity_key = $1 AND project_key = $2
                       FOR UPDATE""",
                    identity_key,
                    project_key,
                    reservation_id,
                )
                if reservation is None:
                    return None
                allowance_class = reservation["allowance_class"]
                row = await connection.fetchrow(
                    """UPDATE anon_quota
                       SET used_turns = used_turns - 1,
                           granted_turns = CASE WHEN $3 = 'granted'
                                                THEN granted_turns + 1
                                                ELSE granted_turns END,
                           updated_at = now()
                       WHERE identity_key = $1 AND project_key = $2 AND used_turns > 0
                       RETURNING used_turns, bonus_turns, granted_turns, bonus_granted""",
                    identity_key,
                    project_key,
                    allowance_class,
                )
                if row is None:
                    return None
                await connection.execute(
                    "DELETE FROM quota_reservations WHERE reservation_id = $1::uuid",
                    reservation["reservation_id"],
                )
                return QuotaRecord(**dict(row))

    async def grant_one_time_bonus(
        self, identity_key: str, bonus_turn_count: int, *, project_key: str = ""
    ) -> bool:
        """Add the bonus exactly once per identity; False when already granted.

        The WHERE bonus_granted = false guard makes re-submitted leads a no-op;
        concurrent grants serialize on the row lock and the loser re-evaluates
        the guard against the committed flag.
        """
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """INSERT INTO anon_quota AS target_row
                       (identity_key, project_key, bonus_turns, bonus_granted)
                   VALUES ($1, $2, $3, TRUE)
                   ON CONFLICT (identity_key, project_key) DO UPDATE
                       SET bonus_turns = target_row.bonus_turns + $3,
                           bonus_granted = TRUE,
                           updated_at = now()
                     WHERE target_row.bonus_granted = FALSE
                   RETURNING TRUE AS granted_now""",
                identity_key,
                project_key,
                bonus_turn_count,
            )
        return row is not None

    async def link_identity_atomically(self, anon_identity_key: str, firebase_uid: str) -> bool:
        """Link once and merge quota usage/bonuses into the Firebase identity."""
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """INSERT INTO identity_links (anon_identity_key, firebase_uid)
                       VALUES ($1, $2)
                       ON CONFLICT DO NOTHING
                       RETURNING anon_identity_key""",
                    anon_identity_key,
                    firebase_uid,
                )
                if row is None:
                    return False
                await connection.execute(
                    """INSERT INTO anon_quota
                       (identity_key, project_key, used_turns, bonus_turns,
                        granted_turns, bonus_granted)
                       SELECT $2, project_key, used_turns, bonus_turns,
                              granted_turns, bonus_granted
                       FROM anon_quota WHERE identity_key = $1
                       ON CONFLICT (identity_key, project_key) DO UPDATE SET
                         used_turns = anon_quota.used_turns + EXCLUDED.used_turns,
                         bonus_turns = GREATEST(anon_quota.bonus_turns, EXCLUDED.bonus_turns),
                         granted_turns = anon_quota.granted_turns + EXCLUDED.granted_turns,
                         bonus_granted = anon_quota.bonus_granted OR EXCLUDED.bonus_granted,
                         updated_at = now()""",
                    anon_identity_key,
                    f"firebase:{firebase_uid}",
                )
                await connection.execute(
                    "UPDATE chat_sessions SET identity_key = $2 WHERE identity_key = $1",
                    anon_identity_key,
                    f"firebase:{firebase_uid}",
                )
                await connection.execute(
                    "DELETE FROM anon_quota WHERE identity_key = $1",
                    anon_identity_key,
                )
                await connection.execute(
                    """INSERT INTO identity_link_audit (anon_identity_key, firebase_uid)
                       VALUES ($1, $2)""",
                    anon_identity_key,
                    firebase_uid,
                )
        return True

    async def grant_turns_atomically(
        self, identity_key: str, turn_count: int, request_id: str, *, project_key: str
    ) -> bool:
        """Apply one admin grant, deduplicated by (identity, project, request_id)."""
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    """INSERT INTO quota_grant_audit
                       (request_id, identity_key, project_key, granted_turns)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (request_id) DO NOTHING
                       RETURNING request_id""",
                    request_id,
                    identity_key,
                    project_key,
                    turn_count,
                )
                if inserted is None:
                    return False
                await connection.execute(
                    """INSERT INTO anon_quota (identity_key, project_key, granted_turns)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (identity_key, project_key) DO UPDATE
                       SET granted_turns = anon_quota.granted_turns + EXCLUDED.granted_turns,
                           updated_at = now()""",
                    identity_key,
                    project_key,
                    turn_count,
                )
        return True

    async def fetch_quota_record(
        self, identity_key: str, *, project_key: str
    ) -> QuotaRecord | None:
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT used_turns, bonus_turns, granted_turns, bonus_granted
                   FROM anon_quota
                   WHERE identity_key = $1 AND project_key = $2""",
                identity_key,
                project_key,
            )
        return QuotaRecord(**dict(row)) if row else None

    async def record_ip_window_request(
        self, ip_address: str, kind: str, window_start: datetime
    ) -> int:
        """Upsert-increment one fixed-window bucket; returns the new counter."""
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """INSERT INTO ip_rate_limit AS target_row
                       (ip, kind, window_start, counter)
                   VALUES ($1::inet, $2, $3, 1)
                   ON CONFLICT (ip, kind, window_start)
                   DO UPDATE SET counter = target_row.counter + 1
                   RETURNING counter""",
                ip_address,
                kind,
                window_start,
            )
        return int(row["counter"])

    async def purge_stale_records(
        self, ip_window_retention_seconds: int, identity_retention_seconds: int
    ) -> tuple[int, int]:
        """Delete expired windows and stale identities; returns (ip, quota) counts."""
        pool = await get_lead_pool()
        async with pool.acquire() as connection:
            ip_status = await connection.execute(
                """DELETE FROM ip_rate_limit
                   WHERE window_start < now() - ($1::bigint * interval '1 second')""",
                ip_window_retention_seconds,
            )
            async with connection.transaction():
                # Reconcile abandoned reservations before deleting their quota
                # rows: stale work is treated as failed and refunded exactly once.
                reservation_retention_seconds = min(ip_window_retention_seconds, 24 * 3600)
                stale_reservations = await connection.fetch(
                    """SELECT reservation_id, identity_key, project_key, allowance_class
                       FROM quota_reservations
                       WHERE created_at < now() - ($1::bigint * interval '1 second')
                       FOR UPDATE""",
                    reservation_retention_seconds,
                )
                for reservation in stale_reservations:
                    await connection.execute(
                        """UPDATE anon_quota
                           SET used_turns = used_turns - 1,
                               granted_turns = CASE WHEN $3 = 'granted'
                                                    THEN granted_turns + 1
                                                    ELSE granted_turns END,
                               updated_at = now()
                           WHERE identity_key = $1 AND project_key = $2 AND used_turns > 0""",
                        reservation["identity_key"],
                        reservation["project_key"],
                        reservation["allowance_class"],
                    )
                    await connection.execute(
                        "DELETE FROM quota_reservations WHERE reservation_id = $1::uuid",
                        reservation["reservation_id"],
                    )
                quota_status = await connection.execute(
                    """DELETE FROM anon_quota
                       WHERE updated_at < now() - ($1::bigint * interval '1 second')
                         AND NOT EXISTS (
                           SELECT 1 FROM quota_reservations r
                           WHERE r.identity_key = anon_quota.identity_key
                             AND r.project_key = anon_quota.project_key
                         )""",
                    identity_retention_seconds,
                )
        return _deleted_row_count(ip_status), _deleted_row_count(quota_status)

    async def check_ip_rate_limit(
        self,
        ip_address: str,
        kind: str,
        max_requests_per_window: int,
        window_seconds: int,
    ) -> bool:
        """RateLimitPort surface for Issue 1A's anon mint limiter (duck-typed).

        Signature contract kept stable so anon_routes can bind this method to
        its own Protocol without importing application internals; True = allowed.
        Delegates to check_ip_limit so validation and semantics live in ONE place.
        """
        return await check_ip_limit(
            ip_address, kind, max_requests_per_window, window_seconds, storage=self
        )

    async def check_rate_limit_allowed(
        self, ip_address: str, kind: str, limit: int, window_seconds: int
    ) -> bool:
        """Conformance alias for Issue 1A's RateLimitPort protocol method name.

        anon_identity.RateLimitPort declares this exact signature; routing the
        Postgres store through one delegation keeps both names honest without
        duplicating throttle logic.
        """
        return await self.check_ip_rate_limit(ip_address, kind, limit, window_seconds)


def _deleted_row_count(execute_status: str) -> int:
    # asyncpg returns command tags like "DELETE 12"; the tail is the row count.
    return int(execute_status.split()[-1])
