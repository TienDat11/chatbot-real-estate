"""RAG port — graph+vector retrieval contract (LightRAG adapter)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RagChunk:
    """One retrieved chunk with its score and document provenance."""

    id: str
    score: float
    content: str
    doc_id: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class RagResult:
    """Retrieved chunks plus a degraded flag (provider/network failure)."""

    chunks: list[RagChunk] = field(default_factory=list)
    degraded: bool = False
    error: str | None = None


@runtime_checkable
class RagPort(Protocol):
    """Hybrid retrieval over the graph+vector index; never raises."""

    async def retrieve(
        self,
        query: str,
        hl_keywords: list[str] | None = None,
        ll_keywords: list[str] | None = None,
    ) -> RagResult: ...
