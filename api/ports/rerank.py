"""Rerank port — scores retrieved chunks against a query."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RerankPort(Protocol):
    """Scores chunks; never raises, returns chunks with scores updated or flagged."""

    async def rerank(self, query: str, chunks: list[dict]) -> list[dict]: ...
