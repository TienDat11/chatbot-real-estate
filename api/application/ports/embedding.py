"""Need-profile embedding port for the re-approach matching pipeline.

The ReengageMatchWorkflow embeds a customer's need profile and an activated
project's sale profile into the SAME vector space so they can be compared by
cosine similarity. The port is intentionally tiny: one batched call, no
similarity or storage concerns — those live in the workflow steps.

HARD LOCK: vectors are 1024 dims (``EMBEDDING_DIM``), matching the ingest
embedding model. A different dim would make profiles incomparable with the
corpus vectors and is rejected at wiring time rather than silently degraded.
"""

from __future__ import annotations

from typing import Protocol


class NeedProfileEmbeddingNotConfiguredError(Exception):
    """Raised when the embedding binding lacks credentials/configuration."""


class NeedProfileEmbeddingPort(Protocol):
    """Embeds short Vietnamese need/sale profile texts into one vector space."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
