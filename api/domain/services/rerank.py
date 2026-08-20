"""Back-compat shim — rerank now resolves via api.dependencies.get_reranker().

Kept so legacy `from api.domain.services.rerank import rerank` call sites keep working.
"""

from __future__ import annotations

from api.infrastructure.adapters.http_rerank import HttpRerank
from api.infrastructure.adapters.noop import NoopRerank
from api.infrastructure.dependencies import get_reranker


async def rerank(query: str, chunks: list[dict]) -> list[dict]:
    """Score chunks through the configured rerank adapter; never raises."""
    return await get_reranker().rerank(query, chunks)


__all__ = ["HttpRerank", "NoopRerank", "get_reranker", "rerank"]
