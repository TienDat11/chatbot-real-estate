"""Postgres adapter for lead and sales operations."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime
from typing import Any

import asyncpg

from api.application.services.sql_leg import build_dsn, _pooler_safe_kwargs
from api.infrastructure.ports.leads import (
    AssignmentDecision,
    AssignmentLogRow,
    LeadRow,
    SalesRow,
    SalesStats,
)

logger = logging.getLogger("api.adapters.postgres_leads")

_LEAD_COLUMNS = "id, session_id, project_key, device_id, name, phone, consent, note, budget_vnd, created_at, status, assigned_sales_id, lock_expires_at, escal_count, last_action_at, closed_at, rejection_reason, reengage_at, mirror_status, consent_service, consent_marketing, consent_at, consent_version, marketing_withdrawn_at, customer_identity"  # noqa: E501
# Lock window for broker-assigned leads. Mirrors lead_service.LEAD_LOCK_MINUTES
# so the atomic row-lock path and the legacy update path set the same window.
_LEAD_LOCK_MINUTES = 5
_lead_pool: asyncpg.Pool | None = None
# Event loop the cached pool was created on: asyncpg connections are bound to
# their creating loop, so a pool reused across loops fails every acquire.
_lead_pool_loop: asyncio.AbstractEventLoop | None = None
# Per-loop asyncio lock to prevent concurrent pool creation; threading.Lock
# guards the lock-dict (sync dict ops only — no blocking on IO).
_pools_guard = threading.Lock()
_pool_locks: dict[int, asyncio.Lock] = {}


def _get_pool_lock(loop_id: int) -> asyncio.Lock:
    """Return (or create) the per-loop lock inside the thread-safe guard."""
    with _pools_guard:
        lock = _pool_locks.get(loop_id)
        if lock is None:
            lock = asyncio.Lock()
            _pool_locks[loop_id] = lock
        return lock


async def get_lead_pool() -> asyncpg.Pool:
    global _lead_pool, _lead_pool_loop
    running = asyncio.get_running_loop()
    # Fast path: cached pool is still valid — no lock contention.
    if (
        _lead_pool is not None
        and not _lead_pool.is_closing()
        and _lead_pool_loop is not None
        and not _lead_pool_loop.is_closed()
        and _lead_pool_loop is running
    ):
        return _lead_pool
    # One async lock per loop so concurrent tasks on the same loop do not
    # create two pools; the lock is created on this loop and only ever
    # awaited from it. Matches the postgres_project_registry pattern.
    lock = _get_pool_lock(id(running))
    async with lock:
        # Re-check inside the lock (double-checked locking).
        if (
            _lead_pool is not None
            and not _lead_pool.is_closing()
            and _lead_pool_loop is not None
            and not _lead_pool_loop.is_closed()
            and _lead_pool_loop is running
        ):
            return _lead_pool
        # Cached pool is stale (dead or foreign loop — e.g. TestClient spins a
        # new loop per test): drop it best-effort and rebuild on THIS loop.
        stale = _lead_pool
        _lead_pool = None
        _lead_pool_loop = None
        if stale is not None:
            try:
                stale.terminate()
            except Exception:  # noqa: BLE001 — old pool may already be unusable
                logger.warning("stale lead pool terminate failed (ignored)", exc_info=True)
        _lead_pool = await asyncpg.create_pool(build_dsn(), min_size=1, max_size=5, **_pooler_safe_kwargs())
        _lead_pool_loop = running
        return _lead_pool


async def close_lead_pool() -> None:
    global _lead_pool, _lead_pool_loop
    if _lead_pool is not None and not _lead_pool.is_closing():
        await _lead_pool.close()
    _lead_pool = None
    _lead_pool_loop = None


class PostgresLeadRepository:
    async def get_sales_by_key(self, access_key: str) -> SalesRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.id, s.access_key, s.full_name, s.role, s.phone, s.is_active,
                          s.priority, s.last_seen_at, s.firebase_uid,
                          MAX(l.created_at) FILTER (WHERE l.action IN ('assign', 'escalate')) AS last_assigned_at
                   FROM sales s LEFT JOIN sales_assignment_log l ON l.sales_id = s.id
                   WHERE s.access_key = $1 AND s.is_active GROUP BY s.id""",  # noqa: E501
                access_key,
            )
        return SalesRow(**dict(row)) if row else None

    async def get_sales_by_firebase_uid(self, firebase_uid: str) -> SalesRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.id, s.access_key, s.full_name, s.role, s.phone, s.is_active,
                          s.priority, s.last_seen_at, s.firebase_uid,
                          MAX(l.created_at) FILTER (WHERE l.action IN ('assign', 'escalate')) AS last_assigned_at
                   FROM sales s LEFT JOIN sales_assignment_log l ON l.sales_id = s.id
                   WHERE s.firebase_uid = $1 AND s.is_active GROUP BY s.id""",  # noqa: E501
                firebase_uid,
            )
        return SalesRow(**dict(row)) if row else None

    async def get_sales_for_admin(self, firebase_uid: str) -> SalesRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.id, s.access_key, s.full_name, s.role, s.phone, s.is_active,
                          s.priority, s.last_seen_at, s.firebase_uid,
                          MAX(l.created_at) FILTER (WHERE l.action IN ('assign', 'escalate')) AS last_assigned_at
                   FROM sales s LEFT JOIN sales_assignment_log l ON l.sales_id = s.id
                   WHERE s.firebase_uid = $1 GROUP BY s.id""",  # noqa: E501
                firebase_uid,
            )
        return SalesRow(**dict(row)) if row else None

    async def update_sales_last_seen(self, sales_id: int) -> None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sales SET last_seen_at = now(), updated_at = now() WHERE id = $1", sales_id
            )

    async def list_sales(self, *, include_disabled: bool = True) -> list[SalesRow]:
        pool = await get_lead_pool()
        where = "" if include_disabled else " WHERE s.is_active"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT s.id, s.access_key, s.full_name, s.role, s.phone,
                          s.is_active, s.priority, s.last_seen_at, s.firebase_uid,
                          MAX(l.created_at) FILTER (WHERE l.action IN ('assign', 'escalate')) AS last_assigned_at
                   FROM sales s LEFT JOIN sales_assignment_log l ON l.sales_id = s.id"""  # noqa: E501
                + where
                + " GROUP BY s.id ORDER BY s.id"
            )
        return [SalesRow(**dict(row)) for row in rows]

    async def set_sales_active(self, firebase_uid: str, *, is_active: bool) -> SalesRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE sales SET is_active = $2, updated_at = now()
                   WHERE firebase_uid = $1
                   RETURNING id, access_key, full_name, role, phone, is_active,
                             priority, last_seen_at, firebase_uid,
                             NULL::timestamptz AS last_assigned_at""",
                firebase_uid,
                is_active,
            )
        return SalesRow(**dict(row)) if row else None

    async def create_sales_mapping(
        self, *, firebase_uid: str, full_name: str, phone: str | None = None, priority: int = 0
    ) -> SalesRow:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO sales (full_name, phone, role, firebase_uid, priority)
                   VALUES ($1, $2, 'sales', $3, $4)
                   ON CONFLICT (firebase_uid) DO UPDATE SET
                     full_name = EXCLUDED.full_name, phone = EXCLUDED.phone,
                     role = 'sales', priority = EXCLUDED.priority,
                     is_active = TRUE, updated_at = now()
                   RETURNING id, access_key, full_name, role, phone, is_active,
                             priority, last_seen_at, firebase_uid,
                             NULL::timestamptz AS last_assigned_at""",
                full_name,
                phone,
                firebase_uid,
                priority,
            )
        return SalesRow(**dict(row))

    async def list_active_sales(self) -> list[SalesRow]:
        return await self.list_sales(include_disabled=False)

    async def create_lead(
        self,
        *,
        session_id: str | None,
        project_key: str | None,
        device_id: str | None,
        name: str | None,
        phone: str,
        consent: bool,
        note: str | None,
        budget_vnd: int | None,
        customer_identity: str | None = None,
    ) -> LeadRow:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                (
                    "INSERT INTO leads (session_id, project_key, device_id, name, phone, consent, "
                    "consent_service, note, budget_vnd, customer_identity) VALUES "
                    "($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING "
                    + _LEAD_COLUMNS
                ),
                session_id,
                project_key,
                device_id,
                name,
                phone,
                consent,
                consent,
                note,
                budget_vnd,
                customer_identity,
            )
        return LeadRow(**dict(row))

    async def create_lead_if_phone_available(
        self,
        *,
        cooldown_seconds: int,
        session_id: str | None,
        project_key: str | None,
        device_id: str | None,
        name: str | None,
        phone: str,
        consent: bool,
        note: str | None,
        budget_vnd: int | None,
        customer_identity: str | None = None,
    ) -> LeadRow | None:
        """Check and insert under one transaction-scoped phone advisory lock."""
        pool = await get_lead_pool()
        from api.application.services.lead_mirror_service import compute_customer_id

        customer_id = compute_customer_id(phone)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", customer_id
                )
                if await conn.fetchval(
                    (
                        "SELECT 1 FROM leads WHERE phone = $1 AND created_at >= now() "
                        "- ($2 * interval '1 second') LIMIT 1"
                    ),
                    phone,
                    max(cooldown_seconds, 0),
                ):
                    return None
                row = await conn.fetchrow(
                    (
                        "INSERT INTO leads (session_id, project_key, device_id, name, phone, "
                        "consent, consent_service, note, budget_vnd, customer_identity) VALUES "
                        "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING "
                        + _LEAD_COLUMNS
                    ),
                    session_id,
                    project_key,
                    device_id,
                    name,
                    phone,
                    consent,
                    consent,
                    note,
                    budget_vnd,
                    customer_identity,
                )
        return LeadRow(**dict(row))

    async def get_active_leads_for_sales(self, sales_id: int, limit: int = 50) -> list[LeadRow]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT "
                + _LEAD_COLUMNS
                + " FROM leads WHERE assigned_sales_id = $1 AND status IN ('assigned', 'callback') ORDER BY lock_expires_at ASC NULLS LAST LIMIT $2",  # noqa: E501
                sales_id,
                limit,
            )
        return [LeadRow(**dict(row)) for row in rows]

    async def search_crm_leads(
        self,
        *,
        assigned_sales_id: int | None,
        project_key: str | None,
        status: str | None,
        reengage_from: date | None,
        reengage_to: date | None,
        tz_name: str,
        cursor_created_at: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[LeadRow]:
        """ONE keyset-paginated SELECT for the CRM leads page (FR-31).

        Only fixed SQL fragments and positional ``$n`` markers are
        concatenated; every caller-supplied value is bound as a parameter, so
        no client input can ever reach the query text. The reengage bounds are
        translated to instants inside SQL via ``AT TIME ZONE $tz`` so calendar
        days follow the business timezone; NULL ``reengage_at`` rows fall out
        of those comparisons on their own. Keyset predicate
        ``(created_at, id) < ($a, $b)`` continues the ``created_at DESC,
        id DESC`` order without OFFSET. Fetches ``limit + 1`` rows so the
        caller can compute ``has_more`` with no second count query.
        """
        pool = await get_lead_pool()
        predicates: list[str] = []
        values: list[Any] = []
        if assigned_sales_id is not None:
            predicates.append("assigned_sales_id = $" + str(len(values) + 1))
            values.append(assigned_sales_id)
        if project_key is not None:
            predicates.append("project_key = $" + str(len(values) + 1))
            values.append(project_key)
        if status is not None:
            predicates.append("status = $" + str(len(values) + 1))
            values.append(status)
        if reengage_from is not None or reengage_to is not None:
            tz_marker = "$" + str(len(values) + 1)
            values.append(tz_name)
            if reengage_from is not None:
                from_marker = "$" + str(len(values) + 1)
                values.append(reengage_from)
                predicates.append(
                    "reengage_at >= (" + from_marker + "::date AT TIME ZONE " + tz_marker + "::text)"
                )
            if reengage_to is not None:
                to_marker = "$" + str(len(values) + 1)
                values.append(reengage_to)
                predicates.append(
                    "reengage_at < ((" + to_marker + "::date + 1) AT TIME ZONE " + tz_marker + "::text)"
                )
        if cursor_created_at is not None and cursor_id is not None:
            created_marker = "$" + str(len(values) + 1)
            values.append(cursor_created_at)
            id_marker = "$" + str(len(values) + 1)
            values.append(cursor_id)
            predicates.append(
                "(created_at, id) < (" + created_marker + ", " + id_marker + ")"
            )
        where = (" WHERE " + " AND ".join(predicates)) if predicates else ""
        values.append(limit)
        sql = (
            "SELECT "
            + _LEAD_COLUMNS
            + " FROM leads"
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT $"
            + str(len(values))
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *values)
        return [LeadRow(**dict(row)) for row in rows]

    async def get_lead_by_id(self, lead_id: int) -> LeadRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT " + _LEAD_COLUMNS + " FROM leads WHERE id = $1", lead_id
            )
        return LeadRow(**dict(row)) if row else None

    async def update_lead(
        self,
        lead_id: int,
        *,
        status: str,
        assigned_sales_id: int | None = None,
        lock_expires_at: datetime | None = None,
        close: bool = False,
        expected_assigned_sales_id: int | None = None,
    ) -> LeadRow | None:
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
        owner_clause = (
            " AND assigned_sales_id = $" + str(len(values) + 1)
            if expected_assigned_sales_id is not None
            else ""
        )
        if expected_assigned_sales_id is not None:
            values.append(expected_assigned_sales_id)
        sql = (
            "UPDATE leads SET "
            + ", ".join(sets)
            + " WHERE id = $1"
            + owner_clause
            + " RETURNING "
            + _LEAD_COLUMNS
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *values)
        return LeadRow(**dict(row)) if row else None

    async def add_assignment_log(
        self, lead_id: int, sales_id: int | None, action: str, note: str | None
    ) -> AssignmentLogRow:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO sales_assignment_log (lead_id, sales_id, action, note)
                   VALUES ($1, $2, $3, $4) RETURNING id, lead_id, sales_id, action, note, created_at""",  # noqa: E501
                lead_id,
                sales_id,
                action,
                note,
            )
        return AssignmentLogRow(**dict(row))

    async def get_tried_sales_ids(self, lead_id: int) -> list[int]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT sales_id FROM sales_assignment_log WHERE lead_id = $1 AND sales_id IS NOT NULL",  # noqa: E501
                lead_id,
            )
        return [int(row["sales_id"]) for row in rows]

    async def assign_lead(
        self,
        lead_id: int,
        *,
        excluded_sales_ids: list[int],
        action: str = "assign",
        note: str | None = None,
        expected_assigned_sales_id: int | None = None,
        prelude_actions: tuple[tuple[str, int | None, str | None], ...] = (),
        preferred_firebase_uid: str | None = None,
    ) -> AssignmentDecision:
        """Decide and apply one lead assignment in a single transaction.

        A per-lead row lock (``SELECT ... FOR UPDATE``) serializes concurrent
        assignment decisions: the second request blocks until the first commits,
        then re-reads the freshly committed row. The ownership predicate is
        re-checked INSIDE the lock, so candidate selection, the guarded
        mutation and the assignment-log insertion cannot diverge. A former
        owner whose guarded update would affect zero rows gets no prelude log,
        no assignment log and no row change (its caller observes a conflict).
        All queries are parameterized; no values are string-interpolated.

        ``preferred_firebase_uid`` (env SALES_PREFERRED_FIREBASE_UID) is a
        narrow test/verification override: when set to a mapped, active sales
        that is still a candidate, that identity sorts first regardless of
        recency or priority; a blank/unset value (or an unmapped/inactive/
        excluded uid) leaves the candidate set untouched and the LRU-first /
        priority-tiebreak ordering intact.
        """
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize concurrent decisions for this lead; a missing row
                # still completes the transaction so callers see a conflict.
                await conn.execute("SELECT id FROM leads WHERE id = $1 FOR UPDATE", lead_id)
                lead_row = await conn.fetchrow(
                    "SELECT " + _LEAD_COLUMNS + " FROM leads WHERE id = $1"
                    " AND ($2::bigint IS NULL OR assigned_sales_id = $2)"
                    " AND status NOT IN ('booked', 'lost', 'expired')",
                    lead_id,
                    expected_assigned_sales_id,
                )
                if lead_row is None:
                    # Ownership predicate failed (missing, reparented, closed):
                    # abort BEFORE any log insertion.
                    return AssignmentDecision(lead=None, next_sales=None)
                logs_written: list[str] = []
                for pre_action, pre_sales_id, pre_note in prelude_actions:
                    await conn.execute(
                        "INSERT INTO sales_assignment_log (lead_id, sales_id, action, note)"
                        " VALUES ($1, $2, $3, $4)",
                        lead_id,
                        pre_sales_id,
                        pre_action,
                        pre_note,
                    )
                    logs_written.append(pre_action)
                excluded = [int(sales_id) for sales_id in excluded_sales_ids]
                candidate = await conn.fetchrow(
                    """SELECT s.id, s.access_key, s.full_name, s.role, s.phone,
                              s.is_active, s.priority, s.last_seen_at,
                              s.firebase_uid,
                              (SELECT MAX(l2.created_at)
                                 FROM sales_assignment_log l2
                                WHERE l2.sales_id = s.id
                                  AND l2.action IN ('assign', 'escalate')
                              ) AS last_assigned_at
                         FROM sales s
                        WHERE s.is_active
                          AND NOT EXISTS (
                                SELECT 1
                                  FROM sales_assignment_log l
                                 WHERE l.lead_id = $1
                                   AND l.sales_id = s.id
                                   AND l.sales_id IS NOT NULL
                              )
                          AND ($2::bigint[] IS NULL OR s.id <> ALL($2))
                        -- The preferred sort key must be a non-NULL boolean: a
                        -- bare AND with s.firebase_uid = $3 yields NULL for
                        -- unmapped rows, and PostgreSQL sorts NULLs FIRST under
                        -- DESC, silently burying the preferred identity.
                        ORDER BY (CASE WHEN $3::text <> '' AND s.firebase_uid = $3
                                       THEN TRUE ELSE FALSE END) DESC,
                                 last_assigned_at ASC NULLS FIRST,
                                 s.priority DESC, s.id
                        LIMIT 1""",
                    lead_id,
                    excluded,
                    preferred_firebase_uid,
                )
                if candidate is None:
                    updated = await conn.fetchrow(
                        "UPDATE leads SET status = 'expired',"
                        " last_action_at = now(), closed_at = now()"
                        " WHERE id = $1 RETURNING " + _LEAD_COLUMNS,
                        lead_id,
                    )
                    if updated is not None:
                        await conn.execute(
                            "INSERT INTO sales_assignment_log"
                            " (lead_id, sales_id, action, note)"
                            " VALUES ($1, NULL, 'expired', '[ESCALATED-ALL]')",
                            lead_id,
                        )
                        logs_written.append("expired")
                    return AssignmentDecision(
                        lead=LeadRow(**dict(updated)) if updated is not None else None,
                        next_sales=None,
                        logs_written=tuple(logs_written),
                    )
                updated = await conn.fetchrow(
                    "UPDATE leads SET status = 'assigned',"
                    " assigned_sales_id = $2,"
                    " lock_expires_at = now() + ($3 * interval '1 minute'),"
                    " last_action_at = now()"
                    " WHERE id = $1 RETURNING " + _LEAD_COLUMNS,
                    lead_id,
                    candidate["id"],
                    _LEAD_LOCK_MINUTES,
                )
                if updated is None:
                    # Under the row lock the lead exists and passed the
                    # predicate, so the guarded update must match.
                    return AssignmentDecision(lead=None, next_sales=None)
                await conn.execute(
                    "INSERT INTO sales_assignment_log (lead_id, sales_id, action, note)"
                    " VALUES ($1, $2, $3, $4)",
                    lead_id,
                    candidate["id"],
                    action,
                    note,
                )
                logs_written.append(action)
                return AssignmentDecision(
                    lead=LeadRow(**dict(updated)),
                    next_sales=SalesRow(**dict(candidate)),
                    logs_written=tuple(logs_written),
                )

    async def get_sales_stats(self, sales_id: int) -> SalesStats:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """WITH today_logs AS (
                         SELECT action, created_at, lead_id
                         FROM sales_assignment_log
                         WHERE sales_id = $1 AND created_at >= CURRENT_DATE
                       ), counts AS (
                         SELECT action, COUNT(*) AS count FROM today_logs GROUP BY action
                       ), answer_times AS (
                         SELECT EXTRACT(
                           EPOCH FROM (called.created_at - assigned.created_at)
                         ) AS seconds
                         FROM today_logs called
                         JOIN sales_assignment_log assigned
                           ON assigned.lead_id = called.lead_id
                          AND assigned.sales_id = $1
                          AND assigned.action IN ('assign', 'escalate')
                          AND assigned.created_at <= called.created_at
                         WHERE called.action = 'call'
                         AND NOT EXISTS (
                           SELECT 1 FROM sales_assignment_log earlier
                           WHERE earlier.lead_id = called.lead_id
                             AND earlier.sales_id = $1
                             AND earlier.action IN ('assign', 'escalate')
                             AND earlier.created_at <= called.created_at
                             AND earlier.created_at > assigned.created_at
                         )
                       )
                       SELECT action, count, NULL::double precision AS avg_answer_seconds
                       FROM counts
                       UNION ALL
                       SELECT NULL::text, NULL::bigint, AVG(seconds)
                       FROM answer_times""",
                sales_id,
            )
        counts = {str(row["action"]): int(row["count"]) for row in rows if row["action"]}
        avg_answer_seconds = next(
            (
                float(row["avg_answer_seconds"])
                for row in rows
                if row["avg_answer_seconds"] is not None
            ),
            None,
        )
        return SalesStats(
            today={
                "assigned": counts.get("assign", 0),
                "called": counts.get("call", 0),
                "heard": counts.get("call", 0),
                "booked": counts.get("booked", 0),
                "no_answer": counts.get("no_answer", 0),
                "escalated": counts.get("escalate", 0),
            },
            avg_answer_seconds=avg_answer_seconds,
            avg_answer_seconds_reason=(
                None
                if avg_answer_seconds is not None
                else "No assigned-to-call response observations today"
            ),
        )

    async def get_sales_by_id(self, sales_id: int) -> SalesRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.id, s.access_key, s.full_name, s.role, s.phone, s.is_active,
                          s.priority, s.last_seen_at, s.firebase_uid,
                          MAX(l.created_at) FILTER (WHERE l.action IN ('assign', 'escalate')) AS last_assigned_at
                   FROM sales s LEFT JOIN sales_assignment_log l ON l.sales_id = s.id
                   WHERE s.id = $1 GROUP BY s.id""",  # noqa: E501
                sales_id,
            )
        return SalesRow(**dict(row)) if row else None

    async def stamp_call_started(self, lead_id: int) -> bool:
        # One-statement CAS claim (FR-33): two concurrent callers race on the
        # same row; only the first UPDATE that sees call_notified_at IS NULL
        # returns a row, so exactly one background dispatch can win. All bind
        # params; no client-supplied SQL fragments.
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchval(
                """UPDATE leads SET call_notified_at = now()
                   WHERE id = $1 AND status = 'called' AND call_notified_at IS NULL
                   RETURNING id""",
                lead_id,
            )
        return row is not None

    async def set_lead_mirror_status(self, lead_id: int, *, mirror_status: str) -> LeadRow | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE leads SET mirror_status = $2 WHERE id = $1 RETURNING " + _LEAD_COLUMNS,
                lead_id,
                mirror_status,
            )
        return LeadRow(**dict(row)) if row else None

    async def list_stale_mirror_leads(self, *, stale_before: datetime, limit: int) -> list[LeadRow]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT "
                + _LEAD_COLUMNS
                + " FROM leads WHERE mirror_status IN ('pending', 'failed') AND created_at < $1 ORDER BY created_at ASC LIMIT $2",  # noqa: E501
                stale_before,
                limit,
            )
        return [LeadRow(**dict(row)) for row in rows]

    async def get_leads_by_phone(self, phone: str) -> list[LeadRow]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT " + _LEAD_COLUMNS + " FROM leads WHERE phone = $1 ORDER BY created_at DESC",
                phone,
            )
        return [LeadRow(**dict(row)) for row in rows]

    async def get_leads_by_customer_id(self, customer_id: str) -> list[LeadRow]:
        # The customer_id IS hmac_sha256(phone, secret) hex, so the lookup is a
        # WHERE-clause HMAC recomputation (pgcrypto) rather than a stored
        # denormalized column — the secret only ever travels as a bind param
        # and the digest stays derivable, never persisted.
        pool = await get_lead_pool()
        from api.infrastructure.config.config import get_settings  # noqa: PLC0415

        hmac_secret = get_settings().lead_mirror_hmac_secret
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT """
                + _LEAD_COLUMNS
                + """ FROM leads
                   WHERE encode(hmac(convert_to(phone, 'UTF8'), convert_to($1, 'UTF8'), 'sha256'), 'hex') = $2
                   ORDER BY created_at DESC""",  # noqa: E501
                hmac_secret,
                customer_id,
            )
        return [LeadRow(**dict(row)) for row in rows]

    async def update_lead_crm_state(
        self,
        lead_id: int,
        *,
        status: str,
        rejection_reason: str | None = None,
        reengage_at: datetime | None = None,
        assigned_sales_id: int | None = None,
        admin_authorized: bool = False,
    ) -> LeadRow | None:
        pool = await get_lead_pool()
        # The owner predicate is part of the mutation, not a prior read. This
        # closes reassignment TOCTOU: a former owner gets zero rows atomically.
        owner_clause = "" if admin_authorized else " AND assigned_sales_id = $5"
        args: list[Any] = [lead_id, status, rejection_reason, reengage_at]
        if not admin_authorized:
            args.append(assigned_sales_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE leads
                   SET status = $2, rejection_reason = $3, reengage_at = $4,
                       last_action_at = now(), mirror_status = 'pending'
                   WHERE id = $1"""
                + owner_clause
                + " RETURNING "
                + _LEAD_COLUMNS,
                *args,
            )
        return LeadRow(**dict(row)) if row else None

    async def set_marketing_consent_withdrawn_for_customer(
        self,
        customer_id: str,
        *,
        assigned_sales_id: int | None = None,
        admin_authorized: bool = False,
    ) -> list[LeadRow]:
        # The opt-out is CUSTOMER-scoped: every lead sharing the customer's
        # phone HMAC flips, never just the acting sales' own rows. A non-admin
        # may trigger it only while they still own at least one of the
        # customer's leads — the ownership precondition lives INSIDE the same
        # statement as the UPDATE (an EXISTS over the customer's rows), so a
        # reassignment racing the mutation cannot widen or narrow the blast
        # radius (same TOCTOU closure as update_lead_crm_state). The first
        # withdrawal timestamp is preserved on repeat calls (idempotent), and
        # pending re-approach timestamps are cancelled with the consent.
        pool = await get_lead_pool()
        from api.infrastructure.config.config import get_settings  # noqa: PLC0415

        hmac_secret = get_settings().lead_mirror_hmac_secret
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """WITH customer_rows AS (
                       SELECT id FROM leads
                       WHERE encode(hmac(convert_to(phone, 'UTF8'), convert_to($1, 'UTF8'), 'sha256'), 'hex') = $2
                   ),
                   caller_still_owns_any AS (
                       SELECT 1 FROM leads
                       WHERE encode(hmac(convert_to(phone, 'UTF8'), convert_to($1, 'UTF8'), 'sha256'), 'hex') = $2
                         AND assigned_sales_id = $4
                       LIMIT 1
                   )
                   UPDATE leads
                   SET marketing_withdrawn_at = COALESCE(marketing_withdrawn_at, now()),
                       consent_marketing = false,
                       reengage_at = NULL,
                       mirror_status = 'pending'
                   WHERE id IN (SELECT id FROM customer_rows)
                     AND ($3::boolean OR EXISTS (SELECT 1 FROM caller_still_owns_any))
                   RETURNING """  # noqa: E501
                + _LEAD_COLUMNS,
                hmac_secret,
                customer_id,
                admin_authorized,
                assigned_sales_id,
            )
        return [LeadRow(**dict(row)) for row in rows]

    async def list_marketing_eligible_rejected_leads(self) -> list[LeadRow]:
        # Marketing-consent gate as a PG pre-filter (story 9.4): only leads a
        # re-approach campaign may ever touch. Legacy NULL consent_marketing
        # rows are excluded — the gate is opt-in by definition.
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT """
                + _LEAD_COLUMNS
                + """ FROM leads
                   WHERE status = 'lost'
                     AND rejection_reason IS NOT NULL
                     AND COALESCE(consent_marketing, false) = true
                     AND marketing_withdrawn_at IS NULL
                   ORDER BY created_at DESC""",
            )
        return [LeadRow(**dict(row)) for row in rows]
