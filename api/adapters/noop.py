"""No-op rerank adapter — used when reranking is disabled by configuration."""

from __future__ import annotations

from api.ports.rerank import RerankPort


class NoopRerank(RerankPort):
    """Pass-through: scores unchanged, but flags chunks so the workflow records the degradation."""

    async def rerank(self, query: str, chunks: list[dict]) -> list[dict]:
        return [{**c, "_rerank_off": True} for c in chunks]
