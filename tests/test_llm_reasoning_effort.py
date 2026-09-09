"""Reasoning-effort wiring for the [OI]-compatible adapter — no network calls."""

from __future__ import annotations

import pytest

from api.infrastructure.adapters.openai_compatible_llm import (
    LLMConfigError,
    OpenAICompatibleLLM,
    resolve_reasoning_effort,
)


@pytest.mark.parametrize(
    "model, configured, expected",
    [
        # Approved luna model keeps its pinned contract regardless of config.
        ("gpt-5.6-luna", "low", "xhigh"),
        ("gpt-5.6-luna", "none", "xhigh"),
        # Case/whitespace variants of the approved model are the same model:
        # the pinned "xhigh" contract must not be bypassable by casing.
        ("Gpt-5.6-Luna", "none", "xhigh"),
        (" GPT-5.6-LUNA ", "none", "xhigh"),
        # Gemini 3.x cannot fully disable thinking -> "none" clamps to "low".
        ("gemini-3.8-flash", "low", "low"),
        ("gemini-3.8-flash", "none", "low"),
        ("Gemini-3.6-Flash", "none", "low"),
        # Path-style ids still identify a gemini-3 model.
        ("models/gemini-3.8-flash", "none", "low"),
        # Older 2.5 family honors thinking_budget=0 -> "none" passes through.
        ("gemini-2.5-flash", "none", "none"),
        ("gemini-2.5-flash", "low", "low"),
        # Unknown models pass the configured value through.
        ("some-other-model", "low", "low"),
        ("some-other-model", "high", "high"),
    ],
)
def test_resolve_reasoning_effort(model: str, configured: str, expected: str) -> None:
    assert resolve_reasoning_effort(model, configured) == expected


class _FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs

        class _Msg:
            content = "ok"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _FakeCompletions()})()


def _make_adapter(reasoning_effort: str) -> OpenAICompatibleLLM:
    adapter = OpenAICompatibleLLM(api_key="k", base_url="https://example.invalid/v1")
    adapter.reasoning_effort = reasoning_effort
    adapter._client = _FakeClient()
    return adapter


@pytest.mark.asyncio
async def test_complete_sends_env_effort_for_gemini_model() -> None:
    adapter = _make_adapter("none")
    await adapter.complete([{"role": "user", "content": "hi"}], model="gemini-2.5-flash")
    assert adapter._client.chat.completions.kwargs["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_complete_clamps_gemini_3x_none_to_low() -> None:
    adapter = _make_adapter("none")
    await adapter.complete([{"role": "user", "content": "hi"}], model="gemini-3.8-flash")
    assert adapter._client.chat.completions.kwargs["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_complete_keeps_xhigh_for_luna_regardless_of_env() -> None:
    adapter = _make_adapter("none")
    await adapter.complete([{"role": "user", "content": "hi"}], model="gpt-5.6-luna")
    assert adapter._client.chat.completions.kwargs["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_complete_defaults_to_low_without_env_override() -> None:
    adapter = OpenAICompatibleLLM(api_key="k", base_url="https://example.invalid/v1")
    adapter._client = _FakeClient()
    await adapter.complete([{"role": "user", "content": "hi"}], model="gemini-3.8-flash")
    assert adapter._client.chat.completions.kwargs["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_complete_sends_original_model_name_with_normalized_effort() -> None:
    adapter = _make_adapter("none")
    await adapter.complete([{"role": "user", "content": "hi"}], model="Gpt-5.6-Luna")
    assert adapter._client.chat.completions.kwargs["model"] == "Gpt-5.6-Luna"
    assert adapter._client.chat.completions.kwargs["reasoning_effort"] == "xhigh"


def test_enforce_approved_provider_accepts_case_variant_luna() -> None:
    adapter = OpenAICompatibleLLM(
        api_key="k",
        base_url="https://example.invalid/v1",
        default_model="Gpt-5.6-Luna",
        enforce_approved_provider=True,
    )
    assert adapter.default_model == "Gpt-5.6-Luna"


def test_enforce_approved_provider_still_rejects_other_models() -> None:
    with pytest.raises(LLMConfigError):
        OpenAICompatibleLLM(
            api_key="k",
            base_url="https://example.invalid/v1",
            default_model="gpt-5.6-sol",
            enforce_approved_provider=True,
        )
