"""OpenAI-compatible chat adapter (aibox / DashScope / local gateways).

Logic moved from the former api/llm.py. Exceptions live here so callers can
import them from a single concrete place via api.adapters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Sequence

from openai import AsyncOpenAI

from api.domain.value_objects.constants import DEFAULT_LLM_TIMEOUT_S, DEFAULT_MODEL_ANSWER
from api.infrastructure.ports.llm import LLMChatPort

logger = logging.getLogger("api.adapters.openai_compatible_llm")

_APPROVED_MODEL = "gpt-5.6-luna"
# Env-swappable chat model: Gemini [OI]-compat rejects the fixed "xhigh"
# reasoning_effort literal, so only attach it for the legacy approved model.
_APPROVED_REASONING_EFFORT = "xhigh"
_GEMINI_REASONING_EFFORT = "low"


def _normalized_model(model: str | None) -> str:
    """Case/whitespace-insensitive form of a model id for comparisons."""
    return (model or "").strip().lower()


def resolve_reasoning_effort(model: str, configured: str) -> str:
    """Pure clamp: pick the reasoning_effort actually sent for ``model``.

    The approved luna model always keeps its pinned "xhigh" (unchanged legacy
    contract). Gemini 3.x cannot fully disable thinking — Google's floor for
    those models is "low" — so a configured "none" is clamped to "low" for any
    model whose name contains "gemini-3". Every other model passes the
    configured value through verbatim. Model matching is case/whitespace
    insensitive so variants of the approved name cannot bypass the contract.
    """
    normalized = _normalized_model(model)
    if normalized == _APPROVED_MODEL:
        return _APPROVED_REASONING_EFFORT
    if configured == "none" and "gemini-3" in normalized:
        return _GEMINI_REASONING_EFFORT
    return configured

# 400 signatures proving the model/gateway does NOT support structured outputs
# (e.g. Novita via OpenRouter returns INVALID_REQUEST_BODY "does not support
# feature: structured-outputs"). Other 400s are genuine request errors and must
# fail immediately, never be retried without response_format.
_STRUCTURED_OUTPUT_UNSUPPORTED_SIGNATURES = (
    "structured-outputs",
    "structured outputs",
    "response_format",
    "invalid_request_body",
    "invalid request body",
)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
# In-process capability cache: (base_url, model) -> True once the provider has
# rejected response_format, so later json_mode calls skip it entirely.
_no_structured_output: set[tuple[str, str]] = set()


def _extract_first_json_object(text: str) -> str | None:
    """Return the first {...} JSON blob from a model response, else None.

    Handles code fences and surrounding prose that providers emit when
    response_format cannot be enforced.
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lstrip().lower().startswith("json"):
            stripped = stripped.lstrip()[4:]
        stripped = stripped.strip()
    match = _JSON_OBJECT_RE.search(stripped)
    return match.group(0) if match else stripped if stripped.startswith("{") else None


def parse_json_leniently(text: str) -> dict:
    """Parse JSON from a response that could not use response_format.

    Raises LLMError when no JSON object can be recovered — callers keep their
    existing failure handling (fail-open) rather than getting None.
    """
    candidate = _extract_first_json_object(text or "")
    if candidate:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise LLMError("LLM json_mode response is not a parsable JSON object")


class LLMError(Exception):
    """Generic LLM failure surfaced to callers for graceful degradation."""


class LLMConfigError(LLMError):
    """Missing required configuration (api key / base url)."""


class LLMTimeoutError(LLMError):
    """Completion exceeded the configured timeout."""


class OpenAICompatibleLLM(LLMChatPort):
    """AsyncOpenAI wrapper for OpenAI-compatible HTTP gateways."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_model: str = DEFAULT_MODEL_ANSWER,
        *,
        enforce_approved_provider: bool = False,
        reasoning_effort: str = _GEMINI_REASONING_EFFORT,
    ):
        if not api_key or not base_url:
            raise LLMConfigError("LLM_API_KEY / LLM_BASE_URL required in Settings")
        if enforce_approved_provider and (
            _normalized_model(default_model) != _APPROVED_MODEL
            or any(token in base_url.lower() for token in ("sol", "openrouter", "ox-alpha"))
        ):
            raise LLMConfigError("Only provider-anh-vu/gpt-5.6-luna is permitted")
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = default_model
        self.enforce_approved_provider = enforce_approved_provider
        self.reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=DEFAULT_LLM_TIMEOUT_S
        )

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
        if self.enforce_approved_provider and _normalized_model(model) != _APPROVED_MODEL:
            raise LLMConfigError(f"Only {_APPROVED_MODEL} is permitted")
        # Env-driven thinking-token control: luna keeps its pinned "xhigh";
        # Gemini 3.x clamps "none"->"low" (resolve_reasoning_effort is the rule).
        reasoning_effort = resolve_reasoning_effort(model, self.reasoning_effort)
        kwargs: dict = {
            "model": model,
            "messages": list(messages),
            "reasoning_effort": reasoning_effort,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode and (self.base_url, model) not in _no_structured_output:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            try:
                resp = await asyncio.wait_for(
                    self._client.chat.completions.create(**kwargs), timeout=timeout
                )
            except Exception as exc:
                if not (json_mode and self._is_unsupported_structured_output_error(exc)):
                    raise
                # Capability fallback: retry ONCE without response_format. The
                # prompt already carries the JSON instruction, so the model can
                # still emit JSON — we just lose the hard format guarantee.
                capability = (self.base_url, model)
                if capability not in _no_structured_output:
                    _no_structured_output.add(capability)
                    logger.info(
                        "structured outputs unsupported (base=%s model=%s); "
                        "cached capability, retrying without response_format",
                        self.base_url,
                        model,
                    )
                kwargs.pop("response_format", None)
                resp = await asyncio.wait_for(
                    self._client.chat.completions.create(**kwargs), timeout=timeout
                )
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(f"LLM complete timeout ({timeout}s) model={model}") from exc
        except Exception as exc:
            raise LLMError(f"LLM complete failed model={model}: {exc}") from exc
        return resp.choices[0].message.content or ""

    @staticmethod
    def _is_unsupported_structured_output_error(exc: Exception) -> bool:
        """True only for a 400 whose body proves structured outputs are unsupported.

        Status must be exactly 400 and the message must carry the structured-
        output / INVALID_REQUEST_BODY signature; anything else (auth, context
        length, malformed request) must fail immediately without a retry.
        """
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code != 400:
            return False
        message = str(exc).lower()
        return any(sig in message for sig in _STRUCTURED_OUTPUT_UNSUPPORTED_SIGNATURES)

    async def stream(
        self,
        messages: Sequence[dict],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas from a streaming chat completion."""
        model = model or self.default_model
        if self.enforce_approved_provider and _normalized_model(model) != _APPROVED_MODEL:
            raise LLMConfigError(f"Only {_APPROVED_MODEL} is permitted")
        # Env-driven thinking-token control: luna keeps its pinned "xhigh";
        # Gemini 3.x clamps "none"->"low" (resolve_reasoning_effort is the rule).
        reasoning_effort = resolve_reasoning_effort(model, self.reasoning_effort)
        kwargs: dict = {
            "model": model,
            "messages": list(messages),
            "stream": True,
            "reasoning_effort": reasoning_effort,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        except Exception as exc:
            raise LLMError(f"LLM stream failed model={model}: {exc}") from exc
