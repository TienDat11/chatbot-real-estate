"""Trusted administrative bindings; never expose these functions as enrolment routes."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from functools import wraps

from api.application.services.messenger_bot_identity import opaque_identity
from api.infrastructure.adapters.postgres_leads import get_lead_pool

_DB_TIMEOUT = 5.0
_IDENTITY = re.compile(r"messenger-bot:identity:v1:[0-9a-f]{64}")


def _validate_identity(identity: str) -> None:
    if not isinstance(identity, str) or _IDENTITY.fullmatch(identity) is None:
        raise ValueError("Invalid Messenger Bot binding input")


def _validate_sales_id(sales_id: int) -> None:
    if isinstance(sales_id, bool) or not isinstance(sales_id, int) or not 0 < sales_id < 2**63:
        raise ValueError("Invalid Messenger Bot binding input")


@asynccontextmanager
async def _connection():
    async with asyncio.timeout(_DB_TIMEOUT):
        pool = await get_lead_pool()
        async with pool.acquire() as conn:
            yield conn


def _safe_errors(operation):
    @wraps(operation)
    async def wrapped(*args, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except ValueError:
            error = ValueError("Invalid Messenger Bot binding input")
        except Exception:
            error = RuntimeError("Messenger Bot binding storage unavailable")
        # Sanitize outside __aexit__: contextlib otherwise reattaches the error
        # thrown into the async generator even after its own handler exits.
        raise error

    return wrapped


@_safe_errors
async def authorize(identity: str) -> int | None:
    """Read current binding AND current sales authority on every invocation."""
    _validate_identity(identity)
    async with _connection() as conn:
        row = await conn.fetchrow(
            """SELECT s.id AS sales_id
            FROM messenger_sales_bindings AS b
            JOIN sales AS s ON s.id = b.sales_id
            WHERE b.opaque_identity = $1
              AND b.is_active = TRUE AND s.is_active = TRUE""",
            identity,
            timeout=_DB_TIMEOUT,
        )
        return row["sales_id"] if row else None


@_safe_errors
async def provision_binding(
    psid: str,
    sales_id: int,
    identity_secret: str,
    *,
    ownership_verified: bool,
) -> bool:
    """Trusted operator must verify ownership and select an existing ACTIVE sales.

    Same owner is idempotent, including revoked rows; conflicts never overwrite.
    The attestation is an administrative caller responsibility, not inbound data.
    """
    if ownership_verified is not True:
        raise ValueError("Messenger Bot binding ownership verification required")
    _validate_sales_id(sales_id)
    identity = opaque_identity(psid, identity_secret)
    async with _connection() as conn:
        async with conn.transaction():
            sales = await conn.fetchrow(
                "SELECT id FROM sales WHERE id = $1 AND is_active = TRUE FOR UPDATE",
                sales_id,
                timeout=_DB_TIMEOUT,
            )
            if sales is None:
                return False
            await conn.execute(
                """INSERT INTO messenger_sales_bindings (opaque_identity, sales_id, is_active)
                VALUES ($1, $2, TRUE)
                ON CONFLICT (opaque_identity) DO NOTHING""",
                identity,
                sales_id,
                timeout=_DB_TIMEOUT,
            )
            binding = await conn.fetchrow(
                "SELECT sales_id FROM messenger_sales_bindings WHERE opaque_identity = $1 FOR UPDATE",
                identity,
                timeout=_DB_TIMEOUT,
            )
            return binding is not None and binding["sales_id"] == sales_id


@_safe_errors
async def revoke_binding(identity: str) -> bool:
    """Revoke independently of sales status; absent binding returns False."""
    _validate_identity(identity)
    async with _connection() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE messenger_sales_bindings SET is_active = FALSE
                WHERE opaque_identity = $1 RETURNING sales_id""",
                identity,
                timeout=_DB_TIMEOUT,
            )
            return row is not None


@_safe_errors
async def reenable_binding(identity: str, sales_id: int, *, approved: bool) -> bool:
    """Explicit trusted approval only; never change conversation ownership."""
    if approved is not True:
        raise ValueError("Messenger Bot binding approval required")
    _validate_identity(identity)
    _validate_sales_id(sales_id)
    async with _connection() as conn:
        async with conn.transaction():
            sales = await conn.fetchrow(
                "SELECT id FROM sales WHERE id = $1 AND is_active = TRUE FOR UPDATE",
                sales_id,
                timeout=_DB_TIMEOUT,
            )
            if sales is None:
                return False
            row = await conn.fetchrow(
                """UPDATE messenger_sales_bindings SET is_active = TRUE
                WHERE opaque_identity = $1 AND sales_id = $2 RETURNING sales_id""",
                identity,
                sales_id,
                timeout=_DB_TIMEOUT,
            )
            return row is not None
