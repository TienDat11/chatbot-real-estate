"""HTTP rerank adapter — scores chunks via dashscope / aibox / openrouter endpoints.

Logic moved from the former api/rerank.py. Never raises: on any failure it marks
chunks degraded and returns them with their previous scores. Reuses a managed
httpx.AsyncClient with bounded timeouts, retries, SSRF guards, and clean aclose().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from api.domain.value_objects.constants import (
    DEFAULT_RERANK_MODEL,
    DEFAULT_RERANK_TIMEOUT_S,
    RERANK_ENDPOINT_AIBOX,
    RERANK_ENDPOINT_DASHSCOPE,
    RERANK_ENDPOINT_OPENROUTER,
    RERANK_HTTP_TIMEOUT_S,
    RERANK_MODEL_OPENROUTER,
    RERANK_RETRY_ATTEMPTS,
    RETRYABLE_HTTP_STATUS_CODES,
)
from api.infrastructure.adapters.outbound_url import OutboundUrlError, validate_outbound_url
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.rerank import RerankPort

logger = logging.getLogger("api.adapters.http_rerank")


def _is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS_CODES


class HttpRerank(RerankPort):
    """Calls the provider rerank endpoint once per query over the retrieved chunks."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        binding: str,
        model: str = DEFAULT_RERANK_MODEL,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float | None = None,
        retry_attempts: int | None = None,
        allowed_hosts: list[str] | tuple[str, ...] | None = None,
        allow_private: bool | None = None,
        resolve_host: Any | None = None,
    ):
        settings = get_settings()
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.binding = binding.strip().lower()
        self.model = model
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._timeout_s = (
            timeout_s
            if timeout_s is not None
            else (
                RERANK_HTTP_TIMEOUT_S
                if self.binding == "openrouter"
                else DEFAULT_RERANK_TIMEOUT_S
            )
        )
        self._retry_attempts = (
            retry_attempts
            if retry_attempts is not None
            else (RERANK_RETRY_ATTEMPTS if self.binding == "openrouter" else 0)
        )
        self._allowed_hosts = (
            allowed_hosts if allowed_hosts is not None else settings.outbound_allowed_hosts
        )
        self._allow_private = (
            allow_private if allow_private is not None else settings.outbound_allow_private
        )
        self._resolve_host = resolve_host
        self._closed = False

    async def _get_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("HttpRerank adapter already closed")
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_http_client = True
        return self._http_client

    async def aclose(self) -> None:
        """Close managed httpx client on shutdown."""
        if self._http_client is not None and self._owns_http_client and not self._closed:
            self._closed = True
            await self._http_client.aclose()

    async def rerank(self, query: str, chunks: list[dict]) -> list[dict]:
        """Update chunk scores from the rerank model; missing config -> unchanged + flag."""
        if not chunks:
            return chunks
        if not self.api_key or not self.base_url:
            return [_flag_degraded(chunk, "rerank_off") for chunk in chunks]

        if self.binding == "dashscope":
            endpoint = RERANK_ENDPOINT_DASHSCOPE
        elif self.binding == "openrouter":
            endpoint = RERANK_ENDPOINT_OPENROUTER
        else:
            endpoint = RERANK_ENDPOINT_AIBOX

        full_url = self.base_url + endpoint

        # Validate URL before outbound network request
        validate_kwargs: dict[str, Any] = {
            "allowed_hosts": self._allowed_hosts,
            "allow_private": self._allow_private,
        }
        if self._resolve_host is not None:
            validate_kwargs["resolve_host"] = self._resolve_host

        try:
            validate_outbound_url(full_url, **validate_kwargs)
        except OutboundUrlError as exc:
            logger.warning("rerank outbound URL validation failed: %s", exc)
            return [_flag_degraded(chunk, "rerank_degraded") for chunk in chunks]

        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": [chunk.get("content", "") for chunk in chunks],
            "top_n": len(chunks),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data: dict[str, Any] | None = None
        try:
            client = await self._get_client()
            last_exc: Exception | None = None
            for attempt in range(self._retry_attempts + 1):
                if attempt:
                    await asyncio.sleep(0.3 * (2 ** (attempt - 1)))
                try:
                    resp = await client.post(full_url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except httpx.HTTPStatusError as exc:
                    if not _is_retryable_status(exc.response.status_code):
                        raise
                    last_exc = exc
                except httpx.HTTPError as exc:
                    last_exc = exc
            if data is None and last_exc is not None:
                raise last_exc
        except Exception as exc:  # noqa: BLE001 — fail -> keep previous scores + flag
            # Sanitize error in logs: never log full exception if it might leak keys
            logger.warning("rerank failed (%s) — keep previous scores", type(exc).__name__)
            return [_flag_degraded(chunk, "rerank_degraded") for chunk in chunks]

        # Parse response results:
        # 1. OpenRouter/Voyage standard: data.results = [{"index": 0, "relevance_score": 0.95, "document": {"text": ...}}]
        # 2. DashScope / Aibox / Cohere variants: results, data, or output.results
        results = (
            data.get("results")
            or data.get("data")
            or (data.get("output") or {}).get("results")
            or []
        )
        scores: dict[int, float] = {}
        for result_item in results:
            if not isinstance(result_item, dict):
                continue
            index = result_item.get("index")
            score = (
                result_item.get("relevance_score")
                if result_item.get("relevance_score") is not None
                else result_item.get("score")
            )
            if index is None:
                index = result_item.get("id")  # DashScope sometimes keys results by 'id'
            if index is not None and score is not None:
                try:
                    scores[int(index)] = float(score)
                except (TypeError, ValueError):
                    continue

        return [
            {**chunk, "score": scores.get(index, chunk.get("score", 0.0))}
            for index, chunk in enumerate(chunks)
        ]


def _flag_degraded(chunk: dict, flag: str) -> dict:
    """Mark a degradation flag on a chunk (merge strips underscore keys before UI)."""
    return {**chunk, f"_{flag}": True}
