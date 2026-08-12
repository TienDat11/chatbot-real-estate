"""Postgres SQL adapter — implements SqlPort over the read-only facts engine.

Thin Ports & Adapters glue: reuses api.sql_leg (R1 spec-builder) and
api.nl2sql_guard (R2 NL2SQL) as the single source of truth for the read-only SQL
logic, mapping their results onto the port contract. Never raises; failures
degrade to an empty SqlResult.
"""

from __future__ import annotations

import logging
from datetime import date

from api.ports.sql import SqlPort, SqlResult
from api.sql_leg import run_sql_leg

logger = logging.getLogger("api.adapters.postgres_sql")


class PostgresSql(SqlPort):
    """SqlPort adapter: R1 spec-builder + R2 NL2SQL, both read-only."""

    async def rows_from_spec(self, spec: dict | None, as_of: date | None) -> SqlResult:
        """Run the deterministic spec-builder path (R1); query is unused for specs."""
        result = await run_sql_leg(spec, as_of, query="")
        return SqlResult(rows=result.rows, meta=result.meta, degraded=result.degraded)

    async def rows_from_nl2sql(self, query: str, as_of: date | None) -> SqlResult:
        """Run the guarded NL2SQL path (R2) for aggregate/compare questions."""
        from api.nl2sql_guard import run_nl2sql  # noqa: PLC0415 — lazy to avoid import cycle

        result = await run_nl2sql(query, as_of)
        return SqlResult(rows=result.rows, meta=result.meta, degraded=result.degraded)
