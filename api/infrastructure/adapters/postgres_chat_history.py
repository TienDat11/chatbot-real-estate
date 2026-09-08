"""Postgres adapter for durable chat history."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from api.application.services.chat_history_service import (
    TRANSCRIPT_MAX_MESSAGES,
    ChatMessage,
    ChatSessionSummary,
    TrainingSessionSummary,
)
from api.infrastructure.adapters.postgres_leads import get_lead_pool

logger = logging.getLogger(__name__)


def _is_history_degraded(exc: BaseException) -> bool:
    """Identify legacy deployments where the chat-history schema is unavailable.

    The column exception is intentionally limited to chat-history statements. A
    missing column in another query must continue to surface as an application
    error rather than being silently converted into an empty history.

    # TRANSIENT: remove after 2026-08-26 upgrade migration is verified in prod.
    """
    return isinstance(exc, asyncpg.UndefinedTableError) or (
        isinstance(exc, asyncpg.UndefinedColumnError) and "chat_" in str(exc)
    ) or (
        exc.__class__.__name__ == "OperationalError" and "chat_" in str(exc)
    )


def _warn_history_degraded(exc: BaseException) -> None:
    logger.warning("Chat history persistence unavailable; degrading gracefully: %s", exc)


def _normalize_meta(value: Any) -> dict[str, Any] | None:
    """Normalize JSONB values returned by asyncpg without a JSON codec."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            logger.warning("Invalid chat message metadata JSON; dropping metadata")
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# Training transcript meta allowlist (G3-r6 §10.2): a staff transcript body is
# safe, but the meta of a TRAINING turn must never carry internal customer
# routing fields. Keys below are the only ones that survive projection —
# citations (sources/facts) and image references, nothing else.
_TRAINING_META_ALLOWED_KEYS = frozenset({"sources", "facts", "images"})


def _sanitize_training_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    return {key: value for key, value in meta.items() if key in _TRAINING_META_ALLOWED_KEYS}


class PostgresChatHistoryRepository:
    async def append_turn(
        self,
        *,
        session_id: str,
        device_id: str | None,
        project_key: str,
        user_content: str,
        assistant_content: str,
        assistant_meta: dict[str, Any],
        identity_key: str | None = None,
    ) -> bool:
        try:
            pool = await get_lead_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    session_result = await conn.execute(
                        """INSERT INTO chat_sessions(session_id, device_id, identity_key, project_key, title, message_count)
                        VALUES($1,$2,$3,$4,$5,0)
                        ON CONFLICT(session_id) DO UPDATE SET last_active_at=now()
                        WHERE chat_sessions.device_id IS NOT DISTINCT FROM EXCLUDED.device_id
                          AND chat_sessions.identity_key IS NOT DISTINCT FROM EXCLUDED.identity_key
                          AND chat_sessions.project_key = EXCLUDED.project_key
                          AND chat_sessions.answer_mode IS NULL""",  # noqa: E501
                        session_id,
                        device_id,
                        identity_key,
                        project_key,
                        user_content[:120],
                    )
                    if session_result == "UPDATE 0":
                        raise PermissionError("chat session ownership mismatch")
                    await conn.executemany(
                        """INSERT INTO chat_messages(session_id, project_key, role, content, meta) VALUES($1,$2,$3,$4,$5::jsonb)""",  # noqa: E501
                        [
                            (session_id, project_key, "user", user_content, "{}"),
                            (
                                session_id,
                                project_key,
                                "assistant",
                                assistant_content,
                                json.dumps(assistant_meta, ensure_ascii=False),
                            ),
                        ],
                    )
                    await conn.execute(
                        "UPDATE chat_sessions SET message_count=message_count+2,last_active_at=now() WHERE session_id=$1",  # noqa: E501
                        session_id,
                    )
            return True
        except Exception as exc:
            if not _is_history_degraded(exc):
                raise
            _warn_history_degraded(exc)
            return False

    async def list_sessions(
        self, *, device_id: str, project_key: str, identity_key: str | None = None, limit: int = 50
    ) -> list[ChatSessionSummary]:
        try:
            pool = await get_lead_pool()
            async with pool.acquire() as conn:
                if identity_key:
                    rows = await conn.fetch(
                        "SELECT session_id, device_id, identity_key, project_key, title, message_count, handed_off, last_active_at FROM chat_sessions WHERE device_id=$1 AND project_key=$2 AND identity_key=$4 AND answer_mode IS NULL ORDER BY last_active_at DESC LIMIT $3",  # noqa: E501
                        device_id,
                        project_key,
                        limit,
                        identity_key,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT session_id, device_id, identity_key, project_key, title, message_count, handed_off, last_active_at FROM chat_sessions WHERE device_id=$1 AND project_key=$2 AND answer_mode IS NULL ORDER BY last_active_at DESC LIMIT $3",  # noqa: E501
                        device_id,
                        project_key,
                        limit,
                    )
            return [ChatSessionSummary(**dict(row)) for row in rows]
        except Exception as exc:
            if not _is_history_degraded(exc):
                raise
            _warn_history_degraded(exc)
            return []

    async def get_session(
        self,
        *,
        session_id: str,
        device_id: str | None = None,
        project_key: str | None = None,
        identity_key: str | None = None,
        staff_scoped: bool = False,
    ) -> ChatSessionSummary | None:
        try:
            pool = await get_lead_pool()
            async with pool.acquire() as conn:
                if device_id is None and project_key is None and identity_key is None:
                    row = await conn.fetchrow(
                        """SELECT session_id, device_id, identity_key, project_key,
                        title, message_count, handed_off, last_active_at
                        FROM chat_sessions WHERE session_id=$1""",
                        session_id,
                    )
                elif staff_scoped:
                    # Staff-created customer sessions persist with identity_key
                    # NULL (the principal's turn_context carries no anon
                    # subject), so the anon triple can never match them; the
                    # device+project pair is the staff ownership scope and
                    # answer_mode IS NULL keeps training rows unreachable.
                    if device_id is None or project_key is None:
                        return None
                    row = await conn.fetchrow(
                        """SELECT session_id, device_id, identity_key, project_key,
                        title, message_count, handed_off, last_active_at
                        FROM chat_sessions
                        WHERE session_id=$1 AND device_id=$2
                          AND project_key=$3 AND answer_mode IS NULL""",
                        session_id,
                        device_id,
                        project_key,
                    )
                elif device_id is None or project_key is None or identity_key is None:
                    return None
                else:
                    row = await conn.fetchrow(
                        """SELECT session_id, device_id, identity_key, project_key,
                        title, message_count, handed_off, last_active_at
                        FROM chat_sessions
                        WHERE session_id=$1 AND device_id=$2
                          AND identity_key=$3 AND project_key=$4
                          AND answer_mode IS NULL""",
                        session_id,
                        device_id,
                        identity_key,
                        project_key,
                    )
            return ChatSessionSummary(**dict(row)) if row else None
        except Exception as exc:
            if not _is_history_degraded(exc):
                raise
            _warn_history_degraded(exc)
            return None

    async def list_messages(self, *, session_id: str) -> list[ChatMessage]:
        try:
            pool = await get_lead_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT role, content, meta, created_at FROM chat_messages WHERE session_id=$1 ORDER BY id",  # noqa: E501
                    session_id,
                )
            return [
                ChatMessage(
                    role=row["role"],
                    content=row["content"],
                    meta=_normalize_meta(row["meta"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        except Exception as exc:
            if not _is_history_degraded(exc):
                raise
            _warn_history_degraded(exc)
            return []

    async def get_session_for_handoff(
        self,
        *,
        session_id: str,
        project_key: str,
        device_id: str | None = None,
        identity_key: str | None = None,
    ) -> ChatSessionSummary | None:
        """Resolve a durable customer session for lead handoff.

        A session id is an opaque routing value, not a credential: at least
        one verifiable ownership claim (signed identity key OR device id)
        must equal the stored row, and the project must match. NULL claims
        never match, so a bare session id still cannot claim a session —
        the predicate is OR-of-claims instead of the strict all-of read
        used for customer self-service, because lead submissions legitimately
        arrive with only one of the two transports.
        """
        if device_id is None and identity_key is None:
            return None
        try:
            pool = await get_lead_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT session_id, device_id, identity_key, project_key,
                    title, message_count, handed_off, last_active_at
                    FROM chat_sessions
                    WHERE session_id=$1 AND project_key=$2
                      AND answer_mode IS NULL
                      AND (($3::text IS NOT NULL AND identity_key = $3)
                           OR ($4::text IS NOT NULL AND device_id = $4))""",  # noqa: E501
                    session_id,
                    project_key,
                    identity_key,
                    device_id,
                )
            return ChatSessionSummary(**dict(row)) if row else None
        except Exception as exc:
            if not _is_history_degraded(exc):
                raise
            _warn_history_degraded(exc)
            return None

    async def get_session_for_lead(
        self, *, lead_id: int
    ) -> tuple[str, str | None, list[ChatMessage]] | None:
        """Transcript source session for a lead, or None when truly unlinked.

        Two arms, both restricted to customer rows (``answer_mode IS NULL``):
        the explicit handoff pointer (``chat_sessions.lead_id``) set after the
        lead committed, or — when that best-effort update was skipped (missing
        identity header, degraded history) — the session_id, project_key and
        device_id claims the lead itself committed atomically in the same
        INSERT. The lead row is the ownership anchor, so a transcript can only
        hydrate for the session that same submission claimed; cross-project
        and foreign-session reads stay impossible; truly unlinked leads return
        None and the CRM read path renders that as its empty state (200).
        """
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT cs.session_id, cs.project_key
                FROM chat_sessions cs
                JOIN leads l ON l.id = $1
                WHERE cs.answer_mode IS NULL
                  AND (
                        cs.lead_id = $1
                        OR (
                            l.session_id IS NOT NULL
                            AND l.device_id IS NOT NULL
                            AND cs.session_id = l.session_id
                            AND cs.project_key = l.project_key
                            AND cs.device_id = l.device_id
                        )
                      )
                LIMIT 1""",  # noqa: E501
                lead_id,
            )
        if not row:
            return None
        session_id = str(row["session_id"])
        return session_id, row["project_key"], await self.list_messages(session_id=session_id)

    async def mark_handed_off(self, *, session_id: str, lead_id: int) -> None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    "UPDATE chat_sessions SET lead_id=$2, handed_off=true, last_active_at=now() WHERE session_id=$1 AND answer_mode IS NULL",  # noqa: E501
                    session_id,
                    lead_id,
                )
            except asyncpg.UndefinedTableError:
                # Legacy deployments may not have applied the additive history migration yet.
                return

    async def get_cta_state(
        self, *, session_id: str, device_id: str, project_key: str, identity_key: str
    ) -> tuple[int, bool, bool]:
        """Read CTA state only for the fully authorized session scope.

        The predicates intentionally mirror ``get_session``.  A session id is
        not an authorization credential, so an unscoped lookup must never
        expose message count, handoff, or phone-submission state.
        """
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT cs.message_count, cs.handed_off,
                    EXISTS(
                        SELECT 1 FROM leads l
                        WHERE l.session_id = cs.session_id AND l.phone IS NOT NULL
                    ) AS phone_given
                    FROM chat_sessions cs
                    WHERE cs.session_id=$1 AND cs.device_id=$2
                      AND cs.identity_key=$3 AND cs.project_key=$4
                      AND cs.answer_mode IS NULL""",
                session_id,
                device_id,
                identity_key,
                project_key,
            )
        if row is None:
            return 0, False, False
        return (
            int(row["message_count"] or 0) // 2,
            bool(row["handed_off"]),
            bool(row["phone_given"]),
        )

    async def cleanup(self, *, retention_days: int) -> int:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            # Customer retention only: private training history is durable
            # (resumable by contract) and never swept by the customer TTL.
            result = await conn.execute(
                "DELETE FROM chat_sessions WHERE answer_mode IS NULL AND last_active_at < now() - ($1 * interval '1 day')",  # noqa: E501
                retention_days,
            )
        return int(result.split()[-1])

    # --- G3-r6 private training history ----------------------------------------

    async def bind_training_session(
        self,
        *,
        session_id: str,
        owner_firebase_uid: str,
        context_project_key: str,
        title: str | None = None,
    ) -> bool:
        """Idempotent owner/session binding; False on any foreign shape.

        A brand-new (never-seen) session id is created ONLY with the training
        marker, so a customer row can never be reclassified. Re-binds match
        (owner, context) exactly; a mismatch means the id is already owned by
        someone else (or is a customer row) and is refused loudly.
        """
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """INSERT INTO chat_sessions(
                           session_id, project_key, title, message_count,
                           owner_firebase_uid, answer_mode, context_project_key
                       )
                       VALUES ($1, $2, $3, 0, $4, 'training', $2)
                       ON CONFLICT(session_id) DO NOTHING""",
                    session_id,
                    context_project_key,
                    (title or "")[:120] or None,
                    owner_firebase_uid,
                )
                if result.endswith("1"):
                    return True
                row = await conn.fetchrow(
                    """SELECT owner_firebase_uid, context_project_key, answer_mode
                       FROM chat_sessions WHERE session_id=$1""",
                    session_id,
                )
                if row is None:
                    return False
                return (
                    row["answer_mode"] == "training"
                    and row["owner_firebase_uid"] == owner_firebase_uid
                    and row["context_project_key"] == context_project_key
                )

    async def append_training_turn(
        self,
        *,
        session_id: str,
        owner_firebase_uid: str,
        context_project_key: str,
        user_content: str,
        assistant_content: str,
        assistant_meta: dict[str, Any],
        user_message_id: str | None = None,
    ) -> bool:
        """Append one training turn, guarded by the bound owner/context.

        Dedupe contract: a retried request whose (session, content) user turn
        was already persisted appends the ASSISTANT answer only — the client
        turn is never duplicated for a repeated request id/content pair, and
        the whole operation is one transaction (all-or-none).
        """
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                session = await conn.fetchrow(
                    """SELECT 1 AS ok FROM chat_sessions
                       WHERE session_id=$1 AND answer_mode='training'
                         AND owner_firebase_uid=$2 AND context_project_key=$3""",
                    session_id,
                    owner_firebase_uid,
                    context_project_key,
                )
                if session is None:
                    return False
                user_exists = await conn.fetchval(
                    """SELECT 1 FROM chat_messages
                       WHERE session_id=$1 AND role='user' AND content=$2 LIMIT 1""",
                    session_id,
                    user_content,
                )
                inserts: list[tuple[str, str, Any]] = [
                    (
                        "assistant",
                        assistant_content,
                        json.dumps(
                            _sanitize_training_meta(assistant_meta) or {}, ensure_ascii=False
                        ),
                    )
                ]
                if not user_exists:
                    inserts.insert(
                        0, ("user", user_content, json.dumps({}, ensure_ascii=False))
                    )
                await conn.executemany(
                    """INSERT INTO chat_messages(session_id, project_key, role, content, meta)
                       VALUES($1,$2,$3,$4,$5::jsonb)""",
                    [
                        (session_id, context_project_key, role, content, meta)
                        for role, content, meta in inserts
                    ],
                )
                await conn.execute(
                    "UPDATE chat_sessions SET message_count=message_count+$2, last_active_at=now() WHERE session_id=$1",  # noqa: E501
                    session_id,
                    len(inserts),
                )
        return True

    async def list_training_sessions(
        self,
        *,
        owner_firebase_uid: str | None,
        limit: int,
        cursor_updated_at: datetime | None,
        cursor_session_id: str | None,
        context_project_key: str | None = None,
    ) -> list[TrainingSessionSummary]:
        # owner_firebase_uid=None means the admin view (audit enforced at the
        # route layer): no owner predicate is applied, so staff oversight is
        # possible without a second query path.
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT session_id, owner_firebase_uid, context_project_key, title,
                          message_count, started_at AS created_at, last_active_at
                   FROM chat_sessions
                   WHERE answer_mode = 'training'
                     AND ($1::text IS NULL OR owner_firebase_uid = $1::text)
                     AND ($2::text IS NULL OR context_project_key = $2::text)
                     AND ($3::timestamptz IS NULL
                          OR (last_active_at, session_id) < ($3::timestamptz, $4::text))
                   ORDER BY last_active_at DESC, session_id DESC
                   LIMIT $5""",
                owner_firebase_uid,
                context_project_key,
                cursor_updated_at,
                cursor_session_id,
                limit,
            )
        return [TrainingSessionSummary(**dict(row)) for row in rows]

    async def get_training_session(
        self, *, session_id: str, owner_firebase_uid: str | None = None
    ) -> TrainingSessionSummary | None:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            if owner_firebase_uid is None:
                row = await conn.fetchrow(
                    """SELECT session_id, owner_firebase_uid, context_project_key, title,
                              message_count, started_at AS created_at, last_active_at
                       FROM chat_sessions
                       WHERE session_id=$1 AND answer_mode='training'""",
                    session_id,
                )
            else:
                row = await conn.fetchrow(
                    """SELECT session_id, owner_firebase_uid, context_project_key, title,
                              message_count, started_at AS created_at, last_active_at
                       FROM chat_sessions
                       WHERE session_id=$1 AND answer_mode='training'
                         AND owner_firebase_uid=$2""",
                    session_id,
                    owner_firebase_uid,
                )
        return TrainingSessionSummary(**dict(row)) if row else None

    async def list_training_messages(self, *, session_id: str) -> list[tuple[int, ChatMessage]]:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            bound = await conn.fetchval(
                "SELECT 1 FROM chat_sessions WHERE session_id=$1 AND answer_mode='training'",
                session_id,
            )
            if bound is None:
                return []
            rows = await conn.fetch(
                """SELECT id, role, content, meta, created_at FROM chat_messages
                   WHERE session_id=$1 ORDER BY created_at ASC, id ASC LIMIT $2""",
                session_id,
                TRANSCRIPT_MAX_MESSAGES + 1,
            )
        return [
            (
                int(row["id"]),
                ChatMessage(
                    role=row["role"],
                    content=row["content"],
                    meta=_sanitize_training_meta(_normalize_meta(row["meta"])),
                    created_at=row["created_at"],
                ),
            )
            for row in rows
        ]

    async def get_message_count(self, *, session_id: str) -> int:
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id=$1", session_id
            )
        return int(value or 0)

    async def validate_customer_identity_constraint(self) -> str:
        """VALIDATE the NOT-VALID customer-identity constraint (G3-r6 §10.3).

        Returns:
            "validated"  — every customer row satisfies the identity rule.
            "pending:<n>" — n violating customer rows exist; NOTHING is
            written or deleted (the fix is a data-review task, not a repair).
            "absent"     — the constraint does not exist in this deployment.
        """
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                """SELECT 1 FROM pg_constraint
                   WHERE conname='chk_sessions_customer_identity'
                     AND conrelid='chat_sessions'::regclass"""
            )
            if exists is None:
                return "absent"
            violation = await conn.fetchval(
                """SELECT l.conname FROM pg_constraint l
                   WHERE l.conname='chk_sessions_customer_identity'
                     AND l.convalidated = false
                   LIMIT 1"""
            )
            if violation is None:
                return "validated"
            offenders = await conn.fetchval(
                """SELECT COUNT(*) FROM chat_sessions
                   WHERE answer_mode IS NULL
                     AND identity_key IS NULL AND device_id IS NULL"""
            )
            if int(offenders or 0) > 0:
                return f"pending:{int(offenders)}"
            await conn.execute(
                "ALTER TABLE chat_sessions VALIDATE CONSTRAINT chk_sessions_customer_identity"
            )
            return "validated"


repository = PostgresChatHistoryRepository()
