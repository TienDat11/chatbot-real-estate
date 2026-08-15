"""Append-only audit trail (plan §4.8 + db/audit.sql).

Inserts one row per query with redacted literals and hashed prompt/answer
(replay §17.6); the writer never raises, so a failed audit cannot break the pipeline.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

from api import get_cfg, sha256_hex
from api.sql_leg import build_dsn

logger = logging.getLogger("api.audit")

_audit_pool: asyncpg.Pool | None = None

# Standalone string/number literals -> '?' placeholders (audit-safe redaction).
_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\b\d+(?:\.\d+)?\b")


async def get_audit_pool() -> asyncpg.Pool:
    global _audit_pool
    if _audit_pool is None or _audit_pool.is_closing():
        _audit_pool = await asyncpg.create_pool(build_dsn(), min_size=1, max_size=2)
    return _audit_pool


async def close_audit_pool() -> None:
    global _audit_pool
    if _audit_pool is not None and not _audit_pool.is_closing():
        await _audit_pool.close()
    _audit_pool = None


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
