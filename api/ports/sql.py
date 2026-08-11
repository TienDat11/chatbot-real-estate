"""SQL port — read-only structured-facts contract (facts + v_unit_offers)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SqlResult:
    """FACT_EVIDENCE rows plus metadata ({mode, source, sql, row_count})."""

    rows: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    degraded: bool = False


@runtime_checkable
class SqlPort(Protocol):
    """Read-only structured facts; never raises, returns SqlResult."""

    async def rows_from_spec(self, spec: dict | None, as_of: date | None) -> SqlResult: ...

    async def rows_from_nl2sql(self, query: str, as_of: date | None) -> SqlResult: ...
