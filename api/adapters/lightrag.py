"""LightRAG retrieval adapter — implements RagPort over the lightrag-hku singleton.

Thin Ports & Adapters glue: reuses the library-specific helpers from api.rag_leg
(instance init, QueryParam build, chunk normalization) so there is one source of
truth for the LightRAG interaction. Never raises; retrieval failures degrade.
"""

from __future__ import annotations

import asyncio
import logging

from api.ports.rag import RagChunk, RagPort, RagResult
from api.rag_leg import _get_rag, _make_query_param, _normalize_chunks

logger = logging.getLogger("api.adapters.lightrag")


class LightRag(RagPort):
    """RagPort adapter: lazy LightRAG init + hybrid aquery + chunk normalization."""

    async def retrieve(
        self,
        query: str,
        hl_keywords: list[str] | None = None,
        ll_keywords: list[str] | None = None,
    ) -> RagResult:
        try:
            rag = await asyncio.wait_for(_get_rag(), timeout=2.0)
            param = _make_query_param(hl_keywords, ll_keywords)
            result = await asyncio.wait_for(rag.aquery(query, param=param), timeout=6.0)
        except Exception as exc:  # noqa: BLE001 — provider failure degrades
            logger.warning("lightrag retrieve failed: %s", exc)
            return RagResult([], degraded=True, error=str(exc))

        raw_data = getattr(result, "raw_data", None) or {}
        chunks = [
            RagChunk(
                id=c["id"],
                score=c["score"],
                content=c["content"],
                doc_id=c.get("file_path"),
            )
            for c in _normalize_chunks(raw_data.get("chunks"))
            if c.get("id")
        ]
        return RagResult(chunks=chunks, degraded=False)
