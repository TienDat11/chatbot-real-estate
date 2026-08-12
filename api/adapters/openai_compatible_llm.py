"""OpenAI-compatible chat adapter (aibox / DashScope / local gateways).

Logic moved from the former api/llm.py. Exceptions live here so callers can
import them from a single concrete place via api.adapters.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Sequence

from openai import AsyncOpenAI

from api.constants import DEFAULT_LLM_TIMEOUT_S, DEFAULT_MODEL_ANSWER
from api.ports.llm import LLMChatPort

logger = logging.getLogger("api.adapters.openai_compatible_llm")


class LLMError(Exception):
    """Generic LLM failure surfaced to callers for graceful degradation."""


class LLMConfigError(LLMError):
    """Missing required configuration (api key / base url)."""


class LLMTimeoutError(LLMError):
    """Completion exceeded the configured timeout."""


class OpenAICompatibleLLM(LLMChatPort):
    """AsyncOpenAI wrapper for OpenAI-compatible HTTP gateways."""

    def __init__(self, api_key: str, base_url: str, default_model: str = DEFAULT_MODEL_ANSWER):
        if not api_key or not base_url:
            raise LLMConfigError("LLM_API_KEY / LLM_BASE_URL required in Settings")
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = default_model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=DEFAULT_LLM_TIMEOUT_S)

    async def aclose(self) -> None:
        """Close the underlying HTTP client (called in lifespan shutdown)."""
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001
            logger.warning("llm client close failed", exc_info=True)

    async def complete(
        self,
        messages: Sequence[dict],
        *,
        json_mode: bool = False,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout: float = DEFAULT_LLM_TIMEOUT_S,
    ) -> str:
        """One chat completion call; json_mode requests a JSON object response."""
        model = model or self.default_model
        kwargs: dict = {"model": model, "messages": list(messages)}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await asyncio.wait_for(self._client.chat.completions.create(**kwargs), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(f"LLM complete timeout ({timeout}s) model={model}") from exc
        except Exception as exc:
            raise LLMError(f"LLM complete failed model={model}: {exc}") from exc
        return resp.choices[0].message.content or ""

    async def stream(self, messages: Sequence[dict], *, model: str | None = None) -> AsyncIterator[str]:
        """Yield content deltas from a streaming chat completion."""
        model = model or self.default_model
        try:
            stream = await self._client.chat.completions.create(
                model=model, messages=list(messages), stream=True
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        except Exception as exc:
            raise LLMError(f"LLM stream failed model={model}: {exc}") from exc
