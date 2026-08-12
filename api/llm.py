"""Back-compat shim — factories/adapters now live in api.dependencies / api.adapters.

Kept so legacy `from api.llm import llm, model_for_role, LLMClient` imports work.
"""

from __future__ import annotations

from api.adapters.openai_compatible_llm import (
    LLMConfigError,
    LLMError,
    LLMTimeoutError,
    OpenAICompatibleLLM,
)
from api.constants import MODEL_ROLE_FIELD
from api.dependencies import LazyLLMProxy, get_llm, model_for_role

LLMClient = OpenAICompatibleLLM  # legacy name alias

llm: OpenAICompatibleLLM = LazyLLMProxy()  # type: ignore[assignment]

__all__ = [
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMTimeoutError",
    "LazyLLMProxy",
    "MODEL_ROLE_FIELD",
    "OpenAICompatibleLLM",
    "get_llm",
    "llm",
    "model_for_role",
]
