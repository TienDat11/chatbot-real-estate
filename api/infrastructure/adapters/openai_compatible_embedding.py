"""OpenAI-compatible embedding adapter for need/sale profile vectors.

Talks the /embeddings chat-completions-compatible REST shape so any of the
configured bindings (dashscope, aibox, local) works with one adapter. Vector
dimensionality is validated against ``EMBEDDING_DIM`` on every response: the
project locks 1024 dims, and a provider silently returning a different size
would corrupt cosine comparability — that must fail loudly, not rank garbage.
"""

from __future__ import annotations

import logging

import httpx

from api.application.ports.embedding import NeedProfileEmbeddingNotConfiguredError
from api.infrastructure.config.config import get_settings

logger = logging.getLogger("api.adapters.openai_compatible_embedding")

_EMBEDDING_HTTP_TIMEOUT_SECONDS = 15.0


class OpenAICompatibleNeedProfileEmbedding:
    """NeedProfileEmbeddingPort over an OpenAI-compatible /embeddings endpoint."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        if not settings.embedding_api_key:
            raise NeedProfileEmbeddingNotConfiguredError(
                "EMBEDDING_API_KEY is required for the re-approach matching pipeline"
            )
        self._base_url = settings.embedding_base_url.rstrip("/")
        self._model = settings.embedding_model
        self._expected_dim = int(settings.embedding_dim)
        self._api_key = settings.embedding_api_key
        self._http_client = http_client

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=_EMBEDDING_HTTP_TIMEOUT_SECONDS)
        return self._http_client

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = await self._client()
        response = await client.post(
            f"{self._base_url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model, "input": texts, "dimensions": self._expected_dim},
        )
        response.raise_for_status()
        payload = response.json()
        vectors = [item["embedding"] for item in payload["data"]]
        observed_dims = {len(vector) for vector in vectors}
        if observed_dims != {self._expected_dim}:
            raise ValueError(
                f"embedding dim drift: expected {self._expected_dim}, got {observed_dims}"
            )
        return vectors
