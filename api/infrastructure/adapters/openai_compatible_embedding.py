"""OpenAI-compatible embedding adapter for need/sale profile vectors.

Talks the /embeddings chat-completions-compatible REST shape so any of the
configured bindings (dashscope, aibox, local) works with one adapter, plus the
revision-3 opt-in OpenRouter route (openai/text-embedding-3-small). Vector
dimensionality is validated against the locked 1024 dims on every response: a
provider silently returning a different size would corrupt cosine comparability
— that must fail loudly, not rank garbage.

Reliability: bounded per-request timeout, retries ONLY for transient HTTP
statuses (408/429/5xx) with exponential backoff, sanitized error messages that
never embed the API key, one managed httpx.AsyncClient with an explicit
``aclose`` lifecycle hook for app shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from api.application.ports.embedding import NeedProfileEmbeddingNotConfiguredError
from api.domain.value_objects.constants import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIMENSION_LOCK,
    EMBEDDING_ENCODING_FORMAT,
    EMBEDDING_HTTP_TIMEOUT_S,
    EMBEDDING_RETRY_ATTEMPTS,
    EMBEDDING_RETRY_BACKOFF_SECONDS,
    RETRYABLE_HTTP_STATUS_CODES,
)
from api.infrastructure.adapters.outbound_url import OutboundUrlError, validate_outbound_url
from api.infrastructure.config.config import get_settings

logger = logging.getLogger("api.adapters.openai_compatible_embedding")


class EmbeddingRequestError(RuntimeError):
    """Embedding call failed after exhausting the retry budget (sanitized detail)."""


def _is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS_CODES


class OpenAICompatibleNeedProfileEmbedding:
    """NeedProfileEmbeddingPort over an OpenAI-compatible /embeddings endpoint."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        expected_dim: int | None = None,
        allowed_hosts: list[str] | tuple[str, ...] | None = None,
        allow_private: bool | None = None,
        resolve_host: Any | None = None,
    ) -> None:
        settings = get_settings()
        binding = (settings.embedding_binding or "").strip().lower()
        if binding == "openrouter":
            default_key = settings.openrouter_api_key or settings.embedding_api_key
            default_base_url = settings.openrouter_base_url
            default_model = settings.embedding_openrouter_model or EMBEDDING_MODEL_OPENROUTER
        else:
            default_key = settings.embedding_api_key
            default_base_url = settings.embedding_base_url
            default_model = settings.embedding_model

        resolved_key = api_key if api_key is not None else default_key
        if not resolved_key:
            raise NeedProfileEmbeddingNotConfiguredError(
                "EMBEDDING_API_KEY is required for the re-approach matching pipeline"
            )
        self._base_url = (base_url if base_url is not None else default_base_url)
        self._model = model if model is not None else default_model
        self._expected_dim = int(expected_dim if expected_dim is not None else settings.embedding_dim)
        if self._expected_dim != EMBEDDING_DIMENSION_LOCK:
            raise NeedProfileEmbeddingNotConfiguredError(
                f"embedding dim must stay locked at {EMBEDDING_DIMENSION_LOCK}, "
                f"got {self._expected_dim}"
            )
        self._api_key = resolved_key
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._timeout_seconds = EMBEDDING_HTTP_TIMEOUT_S
        self._retry_attempts = EMBEDDING_RETRY_ATTEMPTS
        self._retry_backoff_seconds = EMBEDDING_RETRY_BACKOFF_SECONDS
        self._closed = False
        self._allowed_hosts = (
            allowed_hosts if allowed_hosts is not None else settings.outbound_allowed_hosts
        )
        self._allow_private = (
            allow_private if allow_private is not None else settings.outbound_allow_private
        )
        self._resolve_host = resolve_host

        # Enforce SSRF outbound guard on base_url
        validate_kwargs: dict[str, Any] = {
            "allowed_hosts": self._allowed_hosts,
            "allow_private": self._allow_private,
        }
        if self._resolve_host is not None:
            validate_kwargs["resolve_host"] = self._resolve_host
        self._validated_url = validate_outbound_url(
            self._base_url,
            **validate_kwargs,
        )

    async def _client(self) -> httpx.AsyncClient:
        if self._closed:
            raise EmbeddingRequestError("embedding adapter already closed")
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_seconds)
            self._owns_http_client = True
        return self._http_client

    async def aclose(self) -> None:
        """Close the managed HTTP client (app lifespan shutdown only)."""
        if self._http_client is not None and self._owns_http_client and not self._closed:
            self._closed = True
            await self._http_client.aclose()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed texts in order; empty input -> empty output, no HTTP call."""
        if not texts:
            return []
        client = await self._client()
        vectors: list[list[float]] = []
        for batch_start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[batch_start : batch_start + EMBEDDING_BATCH_SIZE]
            vectors.extend(await self._embed_batch(client, batch))
        return vectors

    async def _embed_batch(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        last_error: Exception | None = None
        for attempt in range(self._retry_attempts + 1):
            if attempt:
                await asyncio.sleep(self._retry_backoff_seconds * (2 ** (attempt - 1)))
            try:
                return await self._embed_once(client, batch)
            except httpx.HTTPStatusError as exc:
                if not _is_retryable_status(exc.response.status_code):
                    raise EmbeddingRequestError(
                        "embedding request rejected "
                        f"(status {exc.response.status_code})"
                    ) from None
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
        raise EmbeddingRequestError(
            f"embedding request failed after {self._retry_attempts + 1} attempts"
        ) from last_error

    async def _embed_once(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        response = await client.post(
            f"{self._base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "input": batch,
                "dimensions": self._expected_dim,
                "encoding_format": EMBEDDING_ENCODING_FORMAT,
            },
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingRequestError(
                f"embedding response count mismatch: expected {len(batch)} vectors"
            )
        # Stable index ordering: the API guarantees each item echoes its input
        # index; sort by it so response order can never scramble batch order.
        try:
            ordered = sorted(data, key=lambda item: int(item["index"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingRequestError("embedding response missing stable index") from exc
        vectors = [item["embedding"] for item in ordered]
        observed_dims = {len(vector) for vector in vectors}
        if observed_dims != {self._expected_dim}:
            raise EmbeddingRequestError(
                f"embedding dim drift: expected {self._expected_dim}, got {observed_dims}"
            )
        return vectors
