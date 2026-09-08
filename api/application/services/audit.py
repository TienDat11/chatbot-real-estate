"""Append-only audit trail (plan §4.8 + db/audit.sql).

Inserts one row per query with redacted literals and hashed prompt/answer
(replay §17.6); the writer never raises, so a failed audit cannot break the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any

import asyncpg

from api.infrastructure.config.config import get_settings as get_cfg, settings
from api.domain.services.utils import sha256_hex
from .sql_leg import build_dsn, _pooler_safe_kwargs

logger = logging.getLogger("api.audit")

_audit_pool: asyncpg.Pool | None = None
# Event loop the cached pool was created on: asyncpg connections are bound to
# their creating loop, so a pool reused across loops fails every acquire.
_audit_pool_loop: asyncio.AbstractEventLoop | None = None
# Per-loop asyncio lock — asyncpg pools are loop-bound so a lock created on
# one loop must not guard builds on another.  Matches the registry pattern.
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


# Standalone string/number literals -> '?' placeholders (audit-safe redaction).
_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\b\d+(?:\.\d+)?\b")


async def get_audit_pool() -> asyncpg.Pool:
    global _audit_pool, _audit_pool_loop
    running = asyncio.get_running_loop()
    # Fast path: cached pool is still valid — no lock contention.
    if (
        _audit_pool is not None
        and not _audit_pool.is_closing()
        and _audit_pool_loop is not None
        and not _audit_pool_loop.is_closed()
        and _audit_pool_loop is running
    ):
        return _audit_pool
    # One async lock per loop so concurrent tasks on the same loop do not
    # create two pools; the lock is created on this loop and only ever
    # awaited from it. Matches the postgres_leads pattern.
    lock = _get_pool_lock(id(running))
    async with lock:
        # Re-check inside the lock (double-checked locking).
        if (
            _audit_pool is not None
            and not _audit_pool.is_closing()
            and _audit_pool_loop is not None
            and not _audit_pool_loop.is_closed()
            and _audit_pool_loop is running
        ):
            return _audit_pool
        # Cached pool is stale (dead or foreign loop — e.g. TestClient spins a
        # new loop per test): drop it best-effort and rebuild on THIS loop.
        stale = _audit_pool
        _audit_pool = None
        _audit_pool_loop = None
        if stale is not None:
            try:
                stale.terminate()
            except Exception:  # noqa: BLE001 — old pool may already be unusable
                logger.warning("stale audit pool terminate failed (ignored)", exc_info=True)
        _audit_pool = await asyncpg.create_pool(build_dsn(), min_size=1, max_size=2, **_pooler_safe_kwargs())
        _audit_pool_loop = running
        return _audit_pool


async def close_audit_pool() -> None:
    global _audit_pool, _audit_pool_loop
    if _audit_pool is not None and not _audit_pool.is_closing():
        await _audit_pool.close()
    _audit_pool = None
    _audit_pool_loop = None


def redact_sql_spec(spec: Any) -> Any:
    """Replace filter literal values with a redacted marker, preserving shape for replay."""
    if not isinstance(spec, dict):
        return spec
    out = dict(spec)
    filters = []
    for f in out.get("filters") or []:
        if not isinstance(f, dict):
            filters.append(f)
            continue
        f2 = dict(f)
        v = f2.get("value")
        f2["value"] = ["❚REDACTED❚"] if isinstance(v, (list, tuple)) else "❚REDACTED❚"
        filters.append(f2)
    out["filters"] = filters
    return out


def redact_sql_query(sql: str | None) -> str | None:
    """Mask numeric/string literals in SQL (e.g. '= 2000000000' -> '= ?')."""
    if not sql:
        return sql
    return _LITERAL_RE.sub("?", sql)


async def write_audit(entry: dict[str, Any]) -> None:
    """Write one audit row; never raise into the pipeline (try/except + log)."""
    try:
        pool = await get_audit_pool()
        sql = """INSERT INTO query_audit (
                   trace_id, session_id, query, rewritten_query, routing, structured_path,
                   sql_spec, sql_query, fact_ids, chunk_ids, rerank_scores,
                   prompt_hash, model, model_version, answer_hash, confidence,
                   guard_verdicts, degraded, latency_ms
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)"""

        answer = entry.get("answer") or ""
        answer_hash = entry.get("answer_hash") or sha256_hex(answer)
        prompt_hash = entry.get("prompt_hash") or None

        sql_spec = redact_sql_spec(entry.get("sql_spec"))
        sql_query = redact_sql_query(entry.get("sql_query"))

        async with pool.acquire() as conn:
            await conn.execute(
                sql,
                entry.get("trace_id"),
                entry.get("session_id"),
                entry.get("query"),
                entry.get("rewritten_query"),
                _jsonb(entry.get("routing") or {}),  # JSONB NOT NULL
                entry.get("structured_path"),
                _jsonb(sql_spec),
                sql_query,
                _int_array(entry.get("fact_ids")),
                _text_array(entry.get("chunk_ids")),
                _jsonb(entry.get("rerank_scores")),
                prompt_hash,
                entry.get("model"),
                entry.get("model_version"),
                answer_hash,
                entry.get("confidence"),
                _jsonb(entry.get("guard_verdicts")),
                _jsonb({"flags": entry.get("degraded", [])}),
                entry.get("latency_ms"),
            )
    except Exception:  # noqa: BLE001 — a failed audit must never crash the pipeline
        logger.exception("audit write failed (ignored)")


# asyncpg parameter encoding helpers.
def _jsonb(v: Any) -> str | None:
    """Encode as an asyncpg JSONB parameter: a valid JSON string or None."""
    if v is None:
        return None
    import json

    return json.dumps(v, ensure_ascii=False, default=str)


def _int_array(v: Any):
    if not v:
        return None
    items = [int(x) for x in v if x is not None]
    return items or None


def _text_array(v: Any):
    if not v:
        return None
    items = [str(x) for x in v if x is not None]
    return items or None
