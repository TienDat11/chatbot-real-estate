"""Back-compat shim — rerank now resolves via api.dependencies.get_reranker().

Kept so legacy `from api.rerank import rerank` call sites keep working.
"""

from __future__ import annotations

from api.adapters.http_rerank import HttpRerank
from api.adapters.noop import NoopRerank
from api.dependencies import get_reranker


async def rerank(query: str, chunks: list[dict]) -> list[dict]:
    """Score chunks through the configured rerank adapter; never raises."""
    return await get_reranker().rerank(query, chunks)


__all__ = ["HttpRerank", "NoopRerank", "get_reranker", "rerank"]
